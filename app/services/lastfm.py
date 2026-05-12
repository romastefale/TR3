from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.config.settings import HTTP_TIMEOUT_SECONDS, LASTFM_API_BASE_URL, LASTFM_API_KEY
from app.db.database import SessionLocal
from app.models.lastfm_profile import LastfmProfile

logger = logging.getLogger(__name__)


def _clean_username(username: str) -> str:
    value = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", value):
        raise ValueError("username Last.fm inválido")
    return value


def _stable_track_id(artist: str, track: str) -> str:
    raw = f"{artist}:{track}".lower().strip()
    raw = re.sub(r"\s+", " ", raw)
    safe = re.sub(r"[^a-z0-9]+", ":", raw).strip(":")
    return f"lastfm:{safe or 'unknown'}"


class LastfmService:
    async def set_username(self, user_id: int, username: str) -> str:
        clean = _clean_username(username)
        now = datetime.utcnow()
        with SessionLocal() as db:
            existing = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            if existing:
                existing.username = clean
                existing.updated_at = now
            else:
                db.add(LastfmProfile(user_id=user_id, username=clean, created_at=now, updated_at=now))
            db.commit()
        return clean

    async def clear_username(self, user_id: int) -> bool:
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            if profile:
                db.delete(profile)
                db.commit()
                return True
        return False

    async def get_username(self, user_id: int) -> str | None:
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            return profile.username if profile else None

    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        username = await self.get_username(user_id)
        if not username or not LASTFM_API_KEY:
            return None

        params = {
            "method": "user.getrecenttracks",
            "user": username,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "limit": "1",
            "extended": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.get(LASTFM_API_BASE_URL, params=params)
        except Exception:
            logger.exception("Last.fm request failed | user_id=%s | username=%s", user_id, username)
            return None

        if response.status_code != 200:
            logger.error("Last.fm error %s: %s", response.status_code, response.text)
            return None

        data = response.json()
        recent = (data.get("recenttracks") or {}).get("track") or []
        if isinstance(recent, dict):
            recent = [recent]
        if not recent:
            return None

        item = recent[0]
        return self._map_track(username, item)

    def _text(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("#text") or value.get("name") or "").strip()
        return str(value or "").strip()

    def _map_track(self, username: str, item: dict[str, Any]) -> dict[str, Any] | None:
        track_name = self._text(item.get("name"))
        artist = self._text(item.get("artist"))
        album = self._text(item.get("album"))
        if not track_name or not artist:
            return None

        attr = item.get("@attr") or {}
        nowplaying = str(attr.get("nowplaying") or "").lower() == "true"
        date_data = item.get("date") or {}
        played_at = date_data.get("uts") if isinstance(date_data, dict) else None

        images = item.get("image") or []
        cover = None
        if isinstance(images, list):
            for image in reversed(images):
                if isinstance(image, dict) and image.get("#text"):
                    cover = image.get("#text")
                    break

        track_url = item.get("url") or f"https://www.last.fm/user/{quote(username)}/library"
        album_url = f"https://www.last.fm/music/{quote(artist)}/{quote(album)}" if album else track_url

        return {
            "source": "lastfm_current" if nowplaying else "lastfm_last",
            "played_at": played_at,
            "track_name": track_name,
            "artist": artist,
            "album": album,
            "album_name": album,
            "track_id": _stable_track_id(artist, track_name),
            "spotify_url": track_url,
            "album_url": album_url,
            "album_image_url": cover,
        }


lastfm_service = LastfmService()
