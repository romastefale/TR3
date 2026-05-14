from __future__ import annotations

import html
import io
import logging
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
CARD_HEIGHT = 1350
DEFAULT_BOT_NAME = "tigrãoRADIO"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "monthfm_card.html"

ThemeName = Literal["light", "dark"]

THEMES: dict[ThemeName, dict[str, str]] = {
    "dark": {
        "bg": "#0D0B1A",
        "surface": "#151326",
        "surface_soft": "#1C1930",
        "text": "#F4F1FF",
        "muted": "#B9B2D8",
        "blue": "#7AB7FF",
        "purple": "#A78BFA",
        "line": "rgba(167,139,250,.35)",
        "hero_bg_opacity": ".72",
    },
    "light": {
        "bg": "#F5F1FF",
        "surface": "#FFFFFF",
        "surface_soft": "#F0EAFF",
        "text": "#181225",
        "muted": "#655D7C",
        "blue": "#2563EB",
        "purple": "#7C3AED",
        "line": "rgba(124,58,237,.22)",
        "hero_bg_opacity": ".54",
    },
}

FALLBACK_HERO_IMAGE = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'>"
    "<defs><linearGradient id='g' x1='0' x2='1' y1='0' y2='1'>"
    "<stop offset='0%' stop-color='%237AB7FF'/><stop offset='100%' stop-color='%23A78BFA'/>"
    "</linearGradient></defs><rect width='1024' height='1024' fill='url(%23g)'/>"
    "<circle cx='760' cy='280' r='220' fill='rgba(255,255,255,.18)'/>"
    "<circle cx='260' cy='740' r='260' fill='rgba(0,0,0,.16)'/>"
    "</svg>"
)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
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
    hero_image_url: str | None = None
    top_artists: tuple[CardArtist, ...] = ()
    top_tracks: tuple[CardTrack, ...] = ()
    album_name: str = "Sem disco identificado"
    album_artist: str = "Last.fm"
    album_count: int = 0
    total_scrobbles: int = 0
    minutes: int | None = None


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _format_number(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}".replace(",", ".")


def _row_number(index: int) -> str:
    return f"{index:02d}"


def _artist_rows(items: tuple[CardArtist, ...]) -> str:
    rows: list[str] = []
    for idx, item in enumerate(items[:5], 1):
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            f"<div class=\"name\">{_escape(item.name)}</div>"
            f"<div class=\"count\">{_format_number(item.count)}</div>"
            "</div>"
        )
    while len(rows) < 5:
        idx = len(rows) + 1
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            "<div class=\"name\">—</div>"
            "<div class=\"count\">0</div>"
            "</div>"
        )
    return "\n".join(rows)


def _track_rows(items: tuple[CardTrack, ...]) -> str:
    rows: list[str] = []
    for idx, item in enumerate(items[:5], 1):
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            "<div class=\"name\">"
            f"{_escape(item.title)}"
            f"<span class=\"subname\">{_escape(item.artist)}</span>"
            "</div>"
            f"<div class=\"count\">{_format_number(item.plays)}</div>"
            "</div>"
        )
    while len(rows) < 5:
        idx = len(rows) + 1
        rows.append(
            "<div class=\"row\">"
            f"<div class=\"rank\">{_row_number(idx)}</div>"
            "<div class=\"name\">—<span class=\"subname\">—</span></div>"
            "<div class=\"count\">0</div>"
            "</div>"
        )
    return "\n".join(rows)


def build_monthfm_card_html(data: MonthfmCardData) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    theme = THEMES.get(data.theme, THEMES["dark"])
    values = {
        **theme,
        "bot_name": _escape(data.bot_name),
        "title": _escape(data.title),
        "hero_image": _escape(data.hero_image_url or FALLBACK_HERO_IMAGE),
        "artist_rows": _artist_rows(data.top_artists),
        "track_rows": _track_rows(data.top_tracks),
        "album_name": _escape(data.album_name),
        "album_artist": _escape(data.album_artist),
        "album_count": _format_number(data.album_count),
        "total_scrobbles": _format_number(data.total_scrobbles),
        "minutes": _format_number(data.minutes),
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


def _rounded_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _vertical_gradient(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (width, height), top)
    pixels = img.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(top[0] * (1 - ratio) + bottom[0] * ratio)
        g = int(top[1] * (1 - ratio) + bottom[1] * ratio)
        b = int(top[2] * (1 - ratio) + bottom[2] * ratio)
        for x in range(width):
            pixels[x, y] = (r, g, b)
    return img


def _draw_list_item(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    rank: int,
    name: str,
    count: int,
    rank_font: ImageFont.ImageFont,
    name_font: ImageFont.ImageFont,
    count_font: ImageFont.ImageFont,
    rank_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
    count_color: tuple[int, int, int],
    width: int,
) -> None:
    draw.text((x, y), f"{rank:02d}", font=rank_font, fill=rank_color)
    draw.text((x + 60, y - 3), _ellipsize(name, 22), font=name_font, fill=text_color)
    count_text = _format_number(count)
    bbox = draw.textbbox((0, 0), count_text, font=count_font)
    draw.text((x + width - (bbox[2] - bbox[0]), y - 1), count_text, font=count_font, fill=count_color)


def _draw_track_item(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    rank: int,
    title: str,
    artist: str,
    plays: int,
    rank_font: ImageFont.ImageFont,
    title_font: ImageFont.ImageFont,
    artist_font: ImageFont.ImageFont,
    count_font: ImageFont.ImageFont,
    rank_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
    muted_color: tuple[int, int, int],
    count_color: tuple[int, int, int],
    width: int,
) -> None:
    draw.text((x, y), f"{rank:02d}", font=rank_font, fill=rank_color)
    draw.text((x + 60, y - 6), _ellipsize(title, 20), font=title_font, fill=text_color)
    draw.text((x + 60, y + 29), _ellipsize(artist, 21), font=artist_font, fill=muted_color)
    count_text = _format_number(plays)
    bbox = draw.textbbox((0, 0), count_text, font=count_font)
    draw.text((x + width - (bbox[2] - bbox[0]), y + 5), count_text, font=count_font, fill=count_color)


def _render_pillow_card(data: MonthfmCardData) -> bytes | None:
    try:
        theme = THEMES.get(data.theme, THEMES["dark"])
        bg = _hex_to_rgb(theme["bg"])
        surface = _hex_to_rgb(theme["surface"])
        surface_soft = _hex_to_rgb(theme["surface_soft"])
        text_color = _hex_to_rgb(theme["text"])
        muted = _hex_to_rgb(theme["muted"])
        blue = _hex_to_rgb(theme["blue"])
        purple = _hex_to_rgb(theme["purple"])

        image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), bg)
        draw = ImageDraw.Draw(image)

        hero = _vertical_gradient(CARD_WIDTH, 430, blue, purple)
        image.paste(hero, (0, 0))
        draw.rectangle((0, 320, CARD_WIDTH, 430), fill=(20, 16, 38))

        brand_font = _load_font(34, bold=True)
        title_font = _load_font(76, bold=True)
        section_font = _load_font(29, bold=True)
        rank_font = _load_font(27, bold=True)
        item_font = _load_font(30, bold=True)
        item_font_regular = _load_font(28)
        italic_font = _load_font(22, italic=True)
        count_font = _load_font(26, bold=True)
        small_font = _load_font(23, bold=True)
        album_font = _load_font(31, bold=True)
        total_font = _load_font(54, bold=True)
        total_sub_font = _load_font(28, bold=True)

        draw.text((74, 68), f"♫ {data.bot_name}", font=brand_font, fill=(255, 255, 255))
        wrapped_title = textwrap.wrap(data.title, width=18)[:2]
        title_y = 148
        for line in wrapped_title:
            draw.text((74, title_y), line, font=title_font, fill=(255, 255, 255))
            title_y += 82

        # Abstract cover tile. This does not depend on remote image loading.
        _rounded_rect(draw, (738, 88, 1006, 356), 38, (28, 24, 52))
        _rounded_rect(draw, (762, 112, 982, 332), 32, surface_soft)
        draw.ellipse((802, 152, 942, 292), fill=purple)
        draw.ellipse((838, 188, 906, 256), fill=blue)

        draw.rectangle((0, 430, CARD_WIDTH, CARD_HEIGHT), fill=surface)
        draw.rectangle((0, 430, CARD_WIDTH, 436), fill=purple)

        left_x = 72
        right_x = 560
        list_width = 448
        top_y = 500

        draw.text((left_x, top_y), "✦ Top artistas", font=section_font, fill=muted)
        y = top_y + 58
        for idx, item in enumerate(data.top_artists[:5], 1):
            _draw_list_item(
                draw,
                x=left_x,
                y=y,
                rank=idx,
                name=item.name,
                count=item.count,
                rank_font=rank_font,
                name_font=item_font_regular,
                count_font=count_font,
                rank_color=purple,
                text_color=text_color,
                count_color=blue,
                width=list_width,
            )
            y += 58

        draw.text((right_x, top_y), "♫ Top músicas", font=section_font, fill=muted)
        y = top_y + 54
        for idx, item in enumerate(data.top_tracks[:5], 1):
            _draw_track_item(
                draw,
                x=right_x,
                y=y,
                rank=idx,
                title=item.title,
                artist=item.artist,
                plays=item.plays,
                rank_font=rank_font,
                title_font=item_font,
                artist_font=italic_font,
                count_font=count_font,
                rank_color=purple,
                text_color=text_color,
                muted_color=muted,
                count_color=blue,
                width=list_width,
            )
            y += 70

        line_y = 935
        draw.line((72, line_y, 1008, line_y), fill=purple, width=2)

        _rounded_rect(draw, (72, 980, 622, 1218), 32, surface_soft)
        draw.text((102, 1010), "◌ Disco mais ouvido", font=small_font, fill=muted)
        draw.text((102, 1060), _ellipsize(data.album_name, 28), font=album_font, fill=text_color)
        draw.text((102, 1104), f"{_ellipsize(data.album_artist, 24)} · {_format_number(data.album_count)}", font=italic_font, fill=muted)

        _rounded_rect(draw, (668, 980, 1008, 1218), 32, surface_soft)
        draw.text((698, 1010), "⌁ Total", font=small_font, fill=muted)
        draw.text((698, 1062), _format_number(data.total_scrobbles), font=total_font, fill=blue)
        draw.text((698, 1126), f"{_format_number(data.minutes)} minutos", font=total_sub_font, fill=text_color)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()
    except Exception:
        logger.exception("MONTHFM_CARD_PILLOW_RENDER_FAILED | title=%s", data.title)
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
            page = await browser.new_page(
                viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
                device_scale_factor=1,
            )
            await page.set_content(html_content, wait_until="domcontentloaded", timeout=12000)
            return await page.screenshot(type="jpeg", quality=92, full_page=False, timeout=12000)
    except Exception:
        logger.exception("MONTHFM_CARD_RENDER_FAILED | theme=%s | title=%s", data.theme, data.title)
        return _render_pillow_card(data)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("MONTHFM_CARD_BROWSER_CLOSE_FAILED", exc_info=True)
