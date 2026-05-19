from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class LiveMediaResult:
    content: bytes
    filename: str
    content_type: str


class LiveMediaService:
    def __init__(self) -> None:
        self.enabled = _bool_env("LIVE_MEDIA_ENABLED", False)
        self.url = os.getenv("LIVE_MEDIA_URL", "").strip()
        self.timeout_seconds = float(os.getenv("LIVE_MEDIA_TIMEOUT_SECONDS", "18"))

    async def resolve(self, spotify_url: str) -> LiveMediaResult | None:
        if not self.enabled:
            return None
        if not self.url:
            logger.info("Live media resolver skipped: missing LIVE_MEDIA_URL")
            return None
        clean_url = (spotify_url or "").strip()
        if not clean_url:
            return None

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await client.post(self.url, json={"spotify_url": clean_url})
        except Exception:
            logger.exception("Live media resolver request failed")
            return None

        if response.status_code != 200:
            logger.warning("Live media resolver failed: status=%s body=%s", response.status_code, response.text[:200])
            return None

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "video/mp4":
            logger.warning("Live media resolver returned non-video response: content_type=%s body=%s", content_type, response.text[:200])
            return None

        if not response.content:
            logger.warning("Live media resolver returned empty video")
            return None

        filename = response.headers.get("x-live-media-filename", "canvas.mp4").strip() or "canvas.mp4"
        if not filename.endswith(".mp4"):
            filename = f"{filename}.mp4"
        return LiveMediaResult(content=response.content, filename=filename, content_type="video/mp4")


live_media_service = LiveMediaService()
