from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


async def render_monthfm_card(data: MonthfmCardData) -> bytes | None:
    """Render the monthly/weekly extract card to JPEG bytes.

    This function is intentionally safe: if Playwright or Chromium is missing,
    it returns None so callers can fall back to the text extract.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except Exception:
        logger.warning("MONTHFM_CARD_RENDER_UNAVAILABLE | reason=playwright_import_failed", exc_info=True)
        return None

    html_content = build_monthfm_card_html(data)
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(
                viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
                device_scale_factor=1,
            )
            await page.set_content(html_content, wait_until="networkidle")
            return await page.screenshot(type="jpeg", quality=92, full_page=False)
    except Exception:
        logger.exception("MONTHFM_CARD_RENDER_FAILED | theme=%s | title=%s", data.theme, data.title)
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("MONTHFM_CARD_BROWSER_CLOSE_FAILED", exc_info=True)
