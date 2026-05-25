from __future__ import annotations

import base64
import html
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "tnow_card.html"


@dataclass(frozen=True)
class TnowEntry:
    user_id: int
    display_name: str
    track_name: str
    artist: str
    cover_bytes: bytes | None
    source: str  # "spotify" | "lastfm"


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


FALLBACK_COVER = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 240'>"
    "<defs><linearGradient id='g' x1='0' x2='1' y1='0' y2='1'>"
    "<stop offset='0%' stop-color='%235B9CFF'/>"
    "<stop offset='55%' stop-color='%23B58CFE'/>"
    "<stop offset='100%' stop-color='%233FE0A6'/>"
    "</linearGradient></defs>"
    "<rect width='240' height='240' fill='url(%23g)'/>"
    "<circle cx='120' cy='120' r='52' fill='rgba(255,255,255,.18)'/>"
    "</svg>"
)


def _cover_data_uri(raw: bytes | None, *, max_dim: int = 480) -> str | None:
    """Encode cover bytes into an inline JPEG data URI, safe for Chromium."""
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            largest = max(img.size)
            if largest > max_dim:
                scale = max_dim / largest
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.LANCZOS,
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("TNOW_COVER_ENCODE_FAILED", exc_info=True)
        return None


def _columns_for(n: int) -> int:
    """Grid inteligente: aproxima ceil(sqrt(n)) p/ manter o mosaico quadrado,
    com casos especiais p/ n pequeno onde uma única fileira lê melhor (3, 5)
    e cap em 6 colunas p/ não espremer demais as capas."""
    import math
    if n <= 1:
        return 1
    if n == 3:
        return 3  # 1 fileira de 3 > 2+1
    if n == 5:
        return 5  # 1 fileira de 5 > 3+2 com capa solitária na 2ª linha
    return min(6, max(2, math.ceil(math.sqrt(n))))


def _tile_html(entry: TnowEntry) -> str:
    cover = _cover_data_uri(entry.cover_bytes) or FALLBACK_COVER
    badge = "spotify" if entry.source == "spotify" else "last.fm"
    return (
        '<div class="tile">'
        '<div class="cover-wrap">'
        f'<img class="cover" src="{_esc(cover)}" alt=""/>'
        f'<span class="badge">{_esc(badge)}</span>'
        '</div>'
        '<div class="info">'
        f'<div class="who">{_esc(entry.display_name)}</div>'
        f'<div class="track">{_esc(entry.track_name)}</div>'
        f'<div class="artist">{_esc(entry.artist)}</div>'
        '</div>'
        '</div>'
    )


def build_tnow_card_html(entries: list[TnowEntry], *, now: datetime | None = None) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    columns = _columns_for(len(entries))
    tiles = "\n".join(_tile_html(e) for e in entries) or "<div></div>"
    # Stamp visual: "23/05 • 18:42 BRT" usando hora local Brasília (UTC-3).
    local = now
    try:
        from datetime import timezone, timedelta
        local = now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=-3)))
    except Exception:
        # Sprint 4 (S4.2): fallback usa `now` cru (UTC) — só perde o ajuste
        # de fuso. Antes era silencioso; agora deixa rastro pra entender se
        # algum input estranho de `datetime` está chegando aqui.
        logger.debug("tnow_card timezone conversion failed", exc_info=True)
    stamp_value = local.strftime("%d/%m • %H:%M")
    stamp_iso = local.strftime("%Y-%m-%d %H:%M BRT")
    values = {
        "count": str(len(entries)),
        "columns": str(columns),
        "tiles": tiles,
        "stamp_value": _esc(stamp_value),
        "stamp_iso": _esc(stamp_iso),
    }
    for key, value in values.items():
        template = template.replace("{{ " + key + " }}", str(value))
    return template


async def render_tnow_card(entries: list[TnowEntry]) -> bytes | None:
    """Render the /tnow mosaic card to JPEG bytes via Playwright/Chromium.

    Returns None when Playwright is unavailable or rendering fails. Callers
    should fall back to a textual response in that case.
    """
    if not entries:
        return None
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except Exception:
        logger.warning("TNOW_CARD_RENDER_UNAVAILABLE | reason=playwright_import_failed", exc_info=True)
        return None

    html_content = build_tnow_card_html(entries)
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            # Altura cresce com o grid; viewport inicial pequena pra deixar
            # o navegador medir, depois full_page=True captura tudo.
            page = await browser.new_page(
                viewport={"width": CARD_WIDTH, "height": 1200},
                device_scale_factor=2,
            )
            await page.set_content(html_content, wait_until="networkidle", timeout=20000)
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                logger.debug("TNOW_CARD_FONTS_READY_FAILED", exc_info=True)
            return await page.screenshot(type="jpeg", quality=90, full_page=True, timeout=20000)
    except Exception:
        logger.exception("TNOW_CARD_RENDER_FAILED | n=%s", len(entries))
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("TNOW_CARD_BROWSER_CLOSE_FAILED", exc_info=True)
