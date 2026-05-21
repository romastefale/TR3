"""SAT card: Top 10 tracks of a Spotify playlist, rendered as a square card.

Reuses the Spotify Client Credentials path (no user OAuth required) via
``spotify_service.get_playlist_top_tracks`` and renders via Playwright /
Chromium, the same engine used by ``monthfm_card``.
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.config.settings import HTTP_TIMEOUT_SECONDS
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
CARD_HEIGHT = 1500
ACCENT = "#1ed760"        # Spotify green
ACCENT_SOFT = "rgba(30, 215, 96, .14)"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "sat_card.html"

# open.spotify.com/playlist/<22-char-id>, open.spotify.com/intl-pt/playlist/<id>,
# open.spotify.com/intl-pt-BR/playlist/<id>, spotify:playlist:<id>.
# Spotify playlist IDs são sempre 22 chars base62.
_PLAYLIST_RE = re.compile(
    r"(?:spotify:playlist:|open\.spotify\.com/(?:intl-[a-z]{2,3}(?:-[A-Za-z]{2})?/)?playlist/)"
    r"([A-Za-z0-9]{22})",
    re.IGNORECASE,
)

# Links curtos compartilhados pelo app mobile (spotify.link/xyz) requerem
# resolução de redirect HTTP até chegar em open.spotify.com.
_SHORT_LINK_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi)/[A-Za-z0-9]+", re.IGNORECASE)


def extract_playlist_id(text: str | None) -> str | None:
    """Extract a Spotify playlist id from arbitrary text (URL, URI, plain id).

    Sync version: handles open.spotify.com URLs, spotify: URIs, and bare 22-char IDs.
    For shared mobile short links (spotify.link/...), use ``extract_playlist_id_async``.
    """
    if not text:
        return None
    s = text.strip()
    m = _PLAYLIST_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", s):
        return s
    return None


async def _resolve_short_link(url: str) -> str | None:
    """Follow redirects on a spotify.link/spoti.fi short URL to get the final URL."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 tigraoRADIO/SAT"},
        ) as client:
            resp = await client.head(url)
            return str(resp.url)
    except Exception:
        logger.debug("SAT short-link resolution failed | url=%s", url, exc_info=True)
        return None


async def extract_playlist_id_async(text: str | None) -> str | None:
    """Async version: also resolves spotify.link/spoti.fi short URLs."""
    pid = extract_playlist_id(text)
    if pid:
        return pid
    if not text:
        return None
    short_match = _SHORT_LINK_RE.search(text)
    if not short_match:
        return None
    resolved = await _resolve_short_link(short_match.group(0))
    if not resolved:
        return None
    return extract_playlist_id(resolved)


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _build_rows_html(tracks: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for idx, t in enumerate(tracks, start=1):
        title = html.escape(_truncate(str(t.get("title") or "—"), 34))
        artist = html.escape(_truncate(str(t.get("artist") or "—"), 40))
        rows.append(
            f'<div class="row">'
            f'<div class="rank">{idx:02d}</div>'
            f'<div class="info"><div class="title">{title}</div>'
            f'<div class="artist">{artist}</div></div>'
            f'</div>'
        )
    return "\n".join(rows)


def _build_html(playlist: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    tracks: list[dict[str, Any]] = playlist.get("tracks") or []
    hero_cover = (
        playlist.get("image")
        or (tracks[0].get("cover") if tracks else None)
        or ""
    )
    name = html.escape(_truncate(str(playlist.get("name") or "Playlist"), 60))
    owner_raw = playlist.get("owner")
    owner_block = (
        f'<div class="owner">por {html.escape(_truncate(str(owner_raw), 36))}</div>'
        if owner_raw
        else ""
    )
    rendered = (
        template
        .replace("{{CARD_WIDTH}}", str(CARD_WIDTH))
        .replace("{{CARD_HEIGHT}}", str(CARD_HEIGHT))
        .replace("{{ACCENT}}", ACCENT)
        .replace("{{ACCENT_SOFT}}", ACCENT_SOFT)
        .replace("{{HERO_COVER}}", html.escape(hero_cover, quote=True))
        .replace("{{PLAYLIST_NAME}}", name)
        .replace("{{OWNER_BLOCK}}", owner_block)
        .replace("{{ROWS}}", _build_rows_html(tracks))
    )
    return rendered


async def fetch_playlist(playlist_id: str) -> dict[str, Any] | None:
    return await spotify_service.get_playlist_top_tracks(playlist_id, limit=10)


async def render_sat_card(playlist: dict[str, Any]) -> bytes | None:
    """Render the SAT card to JPEG bytes via Playwright/Chromium."""
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except Exception:
        logger.warning("SAT_CARD_RENDER_UNAVAILABLE | reason=playwright_import_failed", exc_info=True)
        return None

    html_content = _build_html(playlist)
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(
                viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
                device_scale_factor=2,
            )
            await page.set_content(html_content, wait_until="networkidle", timeout=20000)
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                logger.debug("SAT_CARD_FONTS_READY_FAILED", exc_info=True)
            return await page.screenshot(type="jpeg", quality=92, full_page=False, timeout=20000)
    except Exception:
        logger.exception(
            "SAT_CARD_RENDER_FAILED | playlist=%s",
            (playlist or {}).get("name"),
        )
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("SAT_CARD_BROWSER_CLOSE_FAILED", exc_info=True)
