from __future__ import annotations

import base64
import html
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

from app.services.viga_palette import (
    DEFAULT_ACCENT_HEX,
    extract_accent_hex,
    hex_to_rgba,
)

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
CARD_HEIGHT = 2000
DEFAULT_BOT_NAME = "tigraoRADIO"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "monthfm_card.html"

# =========================================================================
# DESIGN TOKENS — fontes do card /monthfm e /weekfm
# -------------------------------------------------------------------------
# Edite estes valores para ajustar tamanhos.
#
# FONT_SCALE: escala tipográfica (Major Third ~1.25). Mude aqui para
#   reescalonar TUDO proporcionalmente.
# CARD_FONTS: mapeia cada elemento visual ao passo da escala. Mude aqui
#   para ajustar um elemento específico (ex: trocar de "subtitle" pra
#   "body-strong").
#
# Os valores são consumidos tanto pelo template HTML (Playwright/Chromium)
# quanto pelo fallback Pillow — fonte única da verdade.
# =========================================================================
FONT_SCALE: dict[str, int] = {
    "micro":       36,   # labels secundárias com tracking
    "eyebrow":     44,   # micro-labels (col titles)
    "body":        52,   # texto secundário, nomes da lista
    "subtitle":    64,   # subtítulo, contagem da lista
    "body-strong": 80,   # números médios, ranks da lista
    "display-sm":  96,   # hero track
    "display-md": 140,   # unidade gigante ("minutos")
    "display-lg": 320,   # display principal (total minutos)
}

# CARD_FONTS — calibrado para o canvas 1080×1900 com coluna útil 952px e
# cada coluna de lista com ~448px. Itens que estouram (label do hero,
# títulos de coluna, nome do item) ficam num passo abaixo dos heróis
# (hero_track, display-lg total) que permanecem dominantes.
CARD_FONTS: dict[str, int] = {
    "brand":            FONT_SCALE["body-strong"] // 2,  # ♫ tigraoRADIO (compacto)
    "period_label":     FONT_SCALE["subtitle"],       # EXTRATO MENSAL/SEMANAL
    "hero_label":       FONT_SCALE["micro"],          # MAIS OUVIDA NO PERÍODO (1 linha)
    "hero_track":       FONT_SCALE["display-sm"],     # nome da música hero
    "hero_artist":      FONT_SCALE["body"],           # artista do hero
    "hero_plays_value": FONT_SCALE["body-strong"],    # número de plays do hero
    "hero_plays_unit":  FONT_SCALE["body"],           # palavra "plays"
    "col_title":        FONT_SCALE["eyebrow"],        # TOP ARTISTAS / TOP MÚSICAS (1 linha)
    "list_rank":        FONT_SCALE["body-strong"],    # 01..05
    "list_item_name":   FONT_SCALE["body"],           # nome do item da lista
    "list_item_sub":    FONT_SCALE["micro"],          # subnome (artista)
    "list_item_count":  FONT_SCALE["subtitle"],       # contagem do item
    "footer_total":     160,                          # total minutos (compacto)
    "footer_unit":      48,                           # "MINUTOS OUVINDO"
    "footer_hint":      FONT_SCALE["eyebrow"],        # NO PERÍODO (legado)
}

# Valor dinâmico do período: encolhe quando texto é longo. Faixa alinhada
# à escala (display-lg ↔ display-sm).
PERIOD_VALUE_STEPS: tuple[tuple[int, int], ...] = (
    (12, 160),   # <= 12 chars → display-lg
    (16, 134),
    (20, 112),
    (26, 92),
    (999, 76),
)

# Nome da música hero — encolhe quando longo para evitar reticências.
# Largura útil ≈ 952px com line-clamp 2.
HERO_TRACK_STEPS: tuple[tuple[int, int], ...] = (
    (14, 102),
    (22, 86),
    (30, 74),
    (40, 61),
    (999, 51),
)

# Nome do item da lista — encolhe por linha conforme o comprimento.
# Largura útil ≈ 290px (col 472 − rank 56 − gap 14 − count 110 − gap 14).
LIST_NAME_STEPS: tuple[tuple[int, int], ...] = (
    (8, 52),
    (12, 44),
    (16, 38),
    (22, 32),
    (999, 28),
)


def _step_size(value: str, steps: tuple[tuple[int, int], ...]) -> int:
    """Devolve o primeiro size cujo limite de chars cobre o texto."""
    length = len(value or "")
    for max_chars, size in steps:
        if length <= max_chars:
            return size
    return steps[-1][1]

ThemeName = Literal["dark"]

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0B0A1A",
        "surface": "#161329",
        "surface_soft": "#1C1A33",
        "text": "#F4F1FF",
        "muted": "#A8A3C6",
        "blue": "#5B9CFF",
        "green": "#3FE0A6",
        "purple": "#B58CFE",
        "line": "rgba(181,140,254,.28)",
    },
}

FALLBACK_HERO_IMAGE = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'>"
    "<defs><linearGradient id='g' x1='0' x2='1' y1='0' y2='1'>"
    "<stop offset='0%' stop-color='%235B9CFF'/>"
    "<stop offset='55%' stop-color='%23B58CFE'/>"
    "<stop offset='100%' stop-color='%233FE0A6'/>"
    "</linearGradient></defs>"
    "<rect width='1024' height='1024' fill='url(%23g)'/>"
    "<circle cx='760' cy='280' r='220' fill='rgba(255,255,255,.16)'/>"
    "<circle cx='260' cy='740' r='260' fill='rgba(0,0,0,.18)'/>"
    "</svg>"
)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]
BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
ITALIC_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf",
]


@dataclass(frozen=True)
class CardArtist:
    name: str
    count: int


@dataclass(frozen=True)
class CardTrack:
    title: str
    artist: str
    plays: int


@dataclass(frozen=True)
class MonthfmCardData:
    title: str
    bot_name: str = DEFAULT_BOT_NAME
    theme: ThemeName = "dark"
    period_label: str = "EXTRATO"
    period_value: str = ""
    hero_image_url: str | None = None
    hero_image_bytes: bytes | None = None
    hero_track: str = ""
    hero_artist: str = ""
    hero_plays: int = 0
    top_artists: tuple[CardArtist, ...] = ()
    top_tracks: tuple[CardTrack, ...] = ()
    # Legacy fields kept for backward compatibility with text builders.
    # They are no longer rendered on the card image.
    album_name: str = ""
    album_artist: str = ""
    album_count: int = 0
    total_scrobbles: int = 0
    minutes: int | None = None
    # Quantidade de itens visíveis em CADA coluna (artistas e músicas).
    # Padrão 5 mantém o card atual dos extratos individuais
    # (/weekfm, /monthfm). O /songcharts (ranking de grupo) usa 10.
    list_size: int = 5


def _card_height_for(list_size: int) -> int:
    """Altura do canvas ajustada à quantidade de linhas das colunas.

    Para até 5 linhas mantém o canvas base (2000px). Acima disso, soma
    70px por linha extra — suficiente pra acomodar passo de 64px no
    fallback Pillow e ajuste de gap do flexbox no HTML.
    """
    extra = max(0, list_size - 5) * 70
    return CARD_HEIGHT + extra


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _format_number(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}".replace(",", ".")


def _row_number(index: int) -> str:
    return str(index)


def _artist_rows(items: tuple[CardArtist, ...], list_size: int = 5) -> str:
    """Tamanho do nome é uniforme (definido no CSS .name) — sem shrink por linha."""
    rows: list[str] = []
    for idx, item in enumerate(items[:list_size], 1):
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            f"<div class=\"name\">{_escape(item.name)}</div>"
            f"<div class=\"count\">{_format_number(item.count)}</div>"
            "</div>"
        )
    while len(rows) < list_size:
        idx = len(rows) + 1
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            "<div class=\"name\">—</div>"
            "<div class=\"count\">0</div>"
            "</div>"
        )
    return "\n".join(rows)


def _track_rows(items: tuple[CardTrack, ...], list_size: int = 5) -> str:
    """Renderiza só nome da música. Tamanho uniforme via CSS .name."""
    rows: list[str] = []
    for idx, item in enumerate(items[:list_size], 1):
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            f"<div class=\"name\">{_escape(item.title)}</div>"
            f"<div class=\"count\">{_format_number(item.plays)}</div>"
            "</div>"
        )
    while len(rows) < list_size:
        idx = len(rows) + 1
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            "<div class=\"name\">—</div>"
            "<div class=\"count\">0</div>"
            "</div>"
        )
    return "\n".join(rows)


def _hero_data_uri(raw: bytes | None, *, max_dim: int = 640) -> str | None:
    """Normalize remote image bytes into a JPEG data URI safe to embed in HTML.

    Caps the largest dimension to keep the page small enough for Chromium to
    rasterize without delay; preserves the best quality the source provides
    up to that bound.
    """
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            largest = max(img.size)
            if largest > max_dim:
                scale = max_dim / largest
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("HERO_DATA_URI_FAILED", exc_info=True)
        return None


def _fit_square(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    if width <= 0 or height <= 0:
        return Image.new("RGB", (size, size), (24, 24, 24))
    scale = max(size / width, size / height)
    new_size = (max(size, int(width * scale)), max(size, int(height * scale)))
    image = image.resize(new_size, Image.LANCZOS)
    left = (image.width - size) // 2
    top = (image.height - size) // 2
    return image.crop((left, top, left + size, top + size))


def _period_font_size(value: str) -> int:
    """Pick a Bebas Neue size that keeps the period inside the 952px column."""
    return _step_size(value, PERIOD_VALUE_STEPS)


def _minutes_unit_scale(minutes_text: str) -> float:
    """Calcula scaleX para que 'minutos ouvindo' fique com a MESMA LARGURA
    visual do número de minutos acima (mantendo font-size atual ~48px).

    Heurística baseada em advance widths típicos do Inter:
    - Inter Black 160px: dígito ≈ 0.555em, '.' ≈ 0.30em
    - Inter ExtraBold 48px uppercase + letter-spacing .04em:
      'MINUTOS OUVINDO' (15 glyphs c/ espaço) ≈ 475px de largura natural.
    """
    number_width = 0.0
    for ch in minutes_text:
        if ch.isdigit():
            number_width += 0.555 * 160.0
        elif ch == ".":
            number_width += 0.30 * 160.0
        else:
            number_width += 0.45 * 160.0
    word_natural = 475.0
    raw = number_width / word_natural if word_natural else 1.0
    # Clamp para evitar deformação extrema em casos limite.
    return round(max(0.45, min(1.6, raw)), 3)


def build_monthfm_card_html(data: MonthfmCardData) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    theme = THEMES.get(data.theme, THEMES["dark"])
    hero_track = data.hero_track or (data.top_tracks[0].title if data.top_tracks else "—")
    hero_artist = data.hero_artist or (data.top_tracks[0].artist if data.top_tracks else "—")
    hero_plays = data.hero_plays or (data.top_tracks[0].plays if data.top_tracks else 0)
    period_value = data.period_value or data.title
    # SECURITY: never let Chromium fetch a remote URL directly. Only inline
    # validated, re-encoded bytes (data URI) or the local SVG placeholder.
    hero_image_src = _hero_data_uri(data.hero_image_bytes) or FALLBACK_HERO_IMAGE
    # Cor dominante extraída da capa do hero (viga_palette). Substitui o
    # verde mint estático em: plays, minutos, hero_label e ícones das colunas.
    accent_hex = extract_accent_hex(data.hero_image_bytes) if data.hero_image_bytes else DEFAULT_ACCENT_HEX
    values = {
        **theme,
        "bot_name": _escape(data.bot_name),
        "period_label": _escape(data.period_label),
        "period_value": _escape(period_value),
        "period_font_size": str(_period_font_size(period_value)),
        "hero_image": _escape(hero_image_src),
        "hero_track": _escape(hero_track),
        "hero_track_font_size": str(_step_size(hero_track, HERO_TRACK_STEPS)),
        "hero_artist": _escape(hero_artist),
        "hero_plays": _format_number(hero_plays),
        "artist_rows": _artist_rows(data.top_artists, data.list_size),
        "track_rows": _track_rows(data.top_tracks, data.list_size),
        "card_height": str(_card_height_for(data.list_size)),
        "minutes": _format_number(data.minutes),
        "minutes_unit_scale": str(_minutes_unit_scale(_format_number(data.minutes))),
        # Accent dinâmico da capa.
        "accent": accent_hex,
        "accent_glow_top": hex_to_rgba(accent_hex, 0.18),
        "accent_glow_mid": hex_to_rgba(accent_hex, 0.10),
        # Design tokens (font sizes) — see CARD_FONTS at top of file.
        **{f"font_{key}": str(size) for key, size in CARD_FONTS.items()},
    }
    for key, value in values.items():
        template = template.replace("{{ " + key + " }}", str(value))
    return template


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _load_font(size: int, *, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = ITALIC_FONT_CANDIDATES if italic else BOLD_FONT_CANDIDATES if bold else FONT_CANDIDATES
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _ellipsize(text: str, max_chars: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "…"


def _render_pillow_card(data: MonthfmCardData) -> bytes | None:
    """Simplified fallback renderer used when Playwright/Chromium is unavailable.

    Matches the dark/blue/green/purple palette of the HTML template but
    without the display font (Bebas Neue) — uses DejaVu Bold instead.
    """
    try:
        theme = THEMES["dark"]
        bg = _hex_to_rgb(theme["bg"])
        surface = _hex_to_rgb(theme["surface"])
        surface_soft = _hex_to_rgb(theme["surface_soft"])
        text_color = _hex_to_rgb(theme["text"])
        muted = _hex_to_rgb(theme["muted"])
        blue = _hex_to_rgb(theme["blue"])
        green = _hex_to_rgb(theme["green"])
        purple = _hex_to_rgb(theme["purple"])

        effective_height = _card_height_for(data.list_size)
        image = Image.new("RGB", (CARD_WIDTH, effective_height), bg)
        draw = ImageDraw.Draw(image)

        # Subtle radial-ish glows using elliptical fills (approximation).
        glow = Image.new("RGB", (CARD_WIDTH, effective_height), bg)
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.ellipse((-220, -260, 560, 480), fill=(blue[0] // 4 + bg[0] // 2, blue[1] // 4 + bg[1] // 2, blue[2] // 4 + bg[2] // 2))
        glow_draw.ellipse((620, -200, 1320, 520), fill=(purple[0] // 4 + bg[0] // 2, purple[1] // 4 + bg[1] // 2, purple[2] // 4 + bg[2] // 2))
        glow_draw.ellipse((180, 1100, 1080, 1700), fill=(green[0] // 5 + bg[0] // 2, green[1] // 5 + bg[1] // 2, green[2] // 5 + bg[2] // 2))
        image = Image.blend(image, glow, alpha=0.55)
        draw = ImageDraw.Draw(image)

        # Fonts — DejaVu Bold é mais largo que Bebas Neue, então aplicamos
        # ~80% nos tamanhos de display pra não estourar o layout do fallback.
        def _disp(token: str) -> int:
            return max(20, int(CARD_FONTS[token] * 0.8))

        brand_font = _load_font(CARD_FONTS["brand"], bold=True)
        period_label_font = _load_font(CARD_FONTS["period_label"], bold=True)
        period_value_font = _load_font(
            max(40, int(_period_font_size(data.period_value or data.title) * 0.7)),
            bold=True,
        )
        hero_label_font = _load_font(CARD_FONTS["hero_label"], bold=True)
        hero_track_font = _load_font(_disp("hero_track"), bold=True)
        hero_artist_font = _load_font(CARD_FONTS["hero_artist"], italic=True)
        hero_plays_font = _load_font(CARD_FONTS["hero_plays_value"], bold=True)
        hero_plays_unit_font = _load_font(CARD_FONTS["hero_plays_unit"], bold=True)
        section_font = _load_font(CARD_FONTS["col_title"], bold=True)
        rank_font = _load_font(_disp("list_rank"), bold=True)
        name_font = _load_font(CARD_FONTS["list_item_name"], bold=True)
        sub_font = _load_font(CARD_FONTS["list_item_sub"], italic=True)
        count_font = _load_font(CARD_FONTS["list_item_count"], bold=True)
        minutes_font = _load_font(_disp("footer_total"), bold=True)
        minutes_word_font = _load_font(_disp("footer_unit"), bold=True)
        minutes_hint_font = _load_font(CARD_FONTS["footer_hint"], bold=True)

        x = 64
        y = 56

        # Header — brand
        draw.text((x, y), f"♫ {data.bot_name}", font=brand_font, fill=blue)
        y += 60

        # Period label + value
        draw.text((x, y), data.period_label.upper(), font=period_label_font, fill=purple)
        y += 40
        draw.text((x, y), (data.period_value or data.title).upper(), font=period_value_font, fill=text_color)
        y += 130

        # Hero card
        hero_top = y
        hero_h = 236
        draw.rounded_rectangle((x - 4, hero_top, CARD_WIDTH - x + 4, hero_top + hero_h), radius=26, fill=surface)
        cover_size = 184
        cover_box = (x + 22, hero_top + 26, x + 22 + cover_size, hero_top + 26 + cover_size)
        pasted_real_cover = False
        if data.hero_image_bytes:
            try:
                with Image.open(io.BytesIO(data.hero_image_bytes)) as raw_cover:
                    cover = _fit_square(raw_cover, cover_size)
                mask = Image.new("L", (cover_size, cover_size), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, cover_size, cover_size), radius=18, fill=255)
                image.paste(cover, (cover_box[0], cover_box[1]), mask)
                draw = ImageDraw.Draw(image)
                pasted_real_cover = True
            except Exception:
                logger.debug("MONTHFM_CARD_PILLOW_COVER_FAILED", exc_info=True)
        if not pasted_real_cover:
            draw.rounded_rectangle(cover_box, radius=18, fill=surface_soft)
            draw.ellipse((cover_box[0] + 40, cover_box[1] + 40, cover_box[2] - 40, cover_box[3] - 40), fill=purple)
            draw.ellipse((cover_box[0] + 70, cover_box[1] + 70, cover_box[2] - 70, cover_box[3] - 70), fill=blue)

        info_x = cover_box[2] + 32
        info_y = hero_top + 30
        draw.text((info_x, info_y), "MAIS OUVIDA NO PERÍODO", font=hero_label_font, fill=muted)
        hero_track = data.hero_track or (data.top_tracks[0].title if data.top_tracks else "—")
        hero_artist = data.hero_artist or (data.top_tracks[0].artist if data.top_tracks else "—")
        hero_plays = data.hero_plays or (data.top_tracks[0].plays if data.top_tracks else 0)
        draw.text((info_x, info_y + 32), _ellipsize(hero_track, 22), font=hero_track_font, fill=text_color)
        draw.text((info_x, info_y + 82), _ellipsize(hero_artist, 26), font=hero_artist_font, fill=green)
        plays_text = _format_number(hero_plays)
        draw.text((info_x, info_y + 122), plays_text, font=hero_plays_font, fill=blue)
        plays_bbox = draw.textbbox((info_x, info_y + 122), plays_text, font=hero_plays_font)
        draw.text((plays_bbox[2] + 8, info_y + 132), "plays", font=hero_plays_unit_font, fill=muted)

        y = hero_top + hero_h + 36

        # Columns
        left_x = x
        right_x = x + 480
        list_width = 416

        draw.text((left_x, y), "✦  TOP ARTISTAS", font=section_font, fill=muted)
        draw.text((right_x, y), "♫  TOP MÚSICAS", font=section_font, fill=muted)

        row_y = y + 50
        for idx in range(data.list_size):
            item = data.top_artists[idx] if idx < len(data.top_artists) else None
            name = item.name if item else "—"
            count = item.count if item else 0
            draw.text((left_x, row_y), f"{idx + 1:02d}", font=rank_font, fill=purple)
            draw.text((left_x + 60, row_y + 4), _ellipsize(name, 20), font=name_font, fill=text_color)
            count_text = _format_number(count)
            bbox = draw.textbbox((0, 0), count_text, font=count_font)
            draw.text((left_x + list_width - (bbox[2] - bbox[0]), row_y + 4), count_text, font=count_font, fill=green)
            row_y += 64

        row_y = y + 50
        for idx in range(data.list_size):
            item = data.top_tracks[idx] if idx < len(data.top_tracks) else None
            title = item.title if item else "—"
            artist = item.artist if item else "—"
            plays = item.plays if item else 0
            draw.text((right_x, row_y), f"{idx + 1:02d}", font=rank_font, fill=purple)
            draw.text((right_x + 60, row_y), _ellipsize(title, 18), font=name_font, fill=text_color)
            draw.text((right_x + 60, row_y + 36), _ellipsize(artist, 22), font=sub_font, fill=muted)
            count_text = _format_number(plays)
            bbox = draw.textbbox((0, 0), count_text, font=count_font)
            draw.text((right_x + list_width - (bbox[2] - bbox[0]), row_y + 8), count_text, font=count_font, fill=green)
            row_y += 64

        # Footer
        footer_top = effective_height - 220
        draw.line((x, footer_top, CARD_WIDTH - x, footer_top), fill=purple, width=2)
        minutes_text = _format_number(data.minutes)
        draw.text((x, footer_top + 24), minutes_text, font=minutes_font, fill=green)
        minutes_bbox = draw.textbbox((x, footer_top + 24), minutes_text, font=minutes_font)
        minutes_right = minutes_bbox[2]
        word_x = max(minutes_right + 30, CARD_WIDTH - x - 240)
        draw.text((word_x, footer_top + 60), "minutos", font=minutes_word_font, fill=text_color)
        draw.text((word_x, footer_top + 120), "NO PERÍODO", font=minutes_hint_font, fill=muted)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()
    except Exception:
        logger.exception("MONTHFM_CARD_PILLOW_RENDER_FAILED | period=%s", data.period_value or data.title)
        return None


async def render_monthfm_card(data: MonthfmCardData) -> bytes | None:
    """Render the monthly/weekly extract card to JPEG bytes.

    The preferred renderer is Playwright/Chromium. If it is unavailable in the
    deploy environment, the function falls back to a pure Pillow card renderer.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except Exception:
        logger.warning("MONTHFM_CARD_RENDER_UNAVAILABLE | reason=playwright_import_failed", exc_info=True)
        return _render_pillow_card(data)

    html_content = build_monthfm_card_html(data)
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            # Render at 2x density (retina). CSS já é grande (1080x1900,
            # fontes 2x maiores via FONT_SCALE). Com DSF=2, físico = 2160x3800,
            # W+H=5960 dentro do limite Telegram (≤10000).
            page = await browser.new_page(
                viewport={"width": CARD_WIDTH, "height": _card_height_for(data.list_size)},
                device_scale_factor=2,
            )
            await page.set_content(html_content, wait_until="networkidle", timeout=20000)
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                logger.debug("MONTHFM_CARD_FONTS_READY_FAILED", exc_info=True)
            return await page.screenshot(type="jpeg", quality=92, full_page=False, timeout=20000)
    except Exception:
        logger.exception(
            "MONTHFM_CARD_RENDER_FAILED | theme=%s | period=%s",
            data.theme,
            data.period_value or data.title,
        )
        return _render_pillow_card(data)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("MONTHFM_CARD_BROWSER_CLOSE_FAILED", exc_info=True)
