from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("live_media_worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

APP_URL = "https://www.canvasdownloader.com/canvas?link={link}"
MAX_BYTES = int(os.getenv("LIVE_MEDIA_MAX_BYTES", str(12 * 1024 * 1024)))
PAGE_TIMEOUT_MS = int(os.getenv("LIVE_MEDIA_PAGE_TIMEOUT_MS", "25000"))
SELECTOR_TIMEOUT_MS = int(os.getenv("LIVE_MEDIA_SELECTOR_TIMEOUT_MS", "18000"))

app = FastAPI(title="Live Media Worker")


class ResolveRequest(BaseModel):
    spotify_url: str


def _safe_filename(name: str | None) -> str:
    raw = (name or "canvas").strip()
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    raw = raw.strip("._") or "canvas"
    if not raw.endswith(".mp4"):
        raw = f"{raw}.mp4"
    return raw


def _looks_like_spotify_track(url: str) -> bool:
    return bool(re.search(r"https://open\.spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]{22}", url))


async def _fetch_response_bytes(response) -> bytes | None:
    try:
        headers = {k.lower(): v for k, v in response.headers.items()}
        content_type = headers.get("content-type", "").lower()
        url = response.url
        if "video/mp4" not in content_type and ".mp4" not in url and "canvaz.scdn.co" not in url:
            return None
        body = await response.body()
        if not body or len(body) > MAX_BYTES:
            return None
        return body
    except Exception:
        logger.exception("Failed reading candidate response")
        return None


async def _resolve_with_browser(spotify_url: str) -> tuple[bytes, str]:
    target_url = APP_URL.format(link=quote(spotify_url, safe=""))
    video_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )

        async def on_response(response) -> None:
            if video_future.done():
                return
            data = await _fetch_response_bytes(response)
            if data:
                video_future.set_result(data)

        page.on("response", on_response)

        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            try:
                data = await asyncio.wait_for(asyncio.shield(video_future), timeout=4)
                return data, _safe_filename(None)
            except asyncio.TimeoutError:
                pass

            deadline = time.monotonic() + (SELECTOR_TIMEOUT_MS / 1000)
            while time.monotonic() < deadline:
                blob_result = await page.evaluate(
                    """
                    async () => {
                      const candidates = Array.from(document.querySelectorAll('a,button'));
                      const link = candidates.find((el) => {
                        const href = el.href || '';
                        const text = (el.innerText || el.textContent || '').toLowerCase();
                        return href.startsWith('blob:') || text.includes('download') || text.includes('baixar');
                      });
                      if (!link) return null;

                      const href = link.href || '';
                      const filename = link.getAttribute('download') || link.download || document.title || 'canvas.mp4';
                      if (href.startsWith('blob:')) {
                        const res = await fetch(href);
                        const buffer = await res.arrayBuffer();
                        return { filename, bytes: Array.from(new Uint8Array(buffer)) };
                      }
                      return null;
                    }
                    """
                )
                if blob_result and blob_result.get("bytes"):
                    data = bytes(blob_result["bytes"])
                    if data and len(data) <= MAX_BYTES:
                        return data, _safe_filename(blob_result.get("filename"))
                await page.wait_for_timeout(500)

            try:
                data = await asyncio.wait_for(asyncio.shield(video_future), timeout=2)
                return data, _safe_filename(None)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=404, detail="canvas_not_found")
        finally:
            await browser.close()


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/resolve")
async def resolve(req: ResolveRequest) -> Response:
    spotify_url = req.spotify_url.strip()
    if not _looks_like_spotify_track(spotify_url):
        raise HTTPException(status_code=400, detail="invalid_spotify_track_url")

    data, filename = await _resolve_with_browser(spotify_url)
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "x-live-media-filename": filename,
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )
