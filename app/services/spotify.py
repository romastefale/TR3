from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.config.settings import (
    HTTP_TIMEOUT_SECONDS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES,
)
from app.db.database import SessionLocal
from app.models.spotify_token import SpotifyToken

logger = logging.getLogger(__name__)


class SpotifyService:
    async def shutdown(self) -> None:
        logger.info("Spotify service stopped.")

    def build_auth_url(self, user_id: int) -> str:
        return (
            "https://accounts.spotify.com/authorize"
            f"?client_id={SPOTIFY_CLIENT_ID}"
            "&response_type=code"
            f"&redirect_uri={SPOTIFY_REDIRECT_URI}"
            f"&scope={quote(SPOTIFY_SCOPES)}"
            f"&state={user_id}"
        )

    def resolve_user_id_from_state(self, state: str) -> int | None:
        try:
            return int(state)
        except ValueError:
            return None

    async def exchange_code_for_token(self, code: str, user_id: int) -> None:
        auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": SPOTIFY_REDIRECT_URI,
                },
                headers={
                    "Authorization": f"Basic {b64_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        data = response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")

        if not access_token or not expires_in:
            logger.error("Invalid Spotify token response: %s", data)
            return

        expiration = datetime.utcnow() + timedelta(seconds=int(expires_in))
        with SessionLocal() as db:
            existing = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if existing:
                existing.access_token = access_token
                existing.expiration = expiration
                if refresh_token:
                    existing.refresh_token = refresh_token
            else:
                db.add(
                    SpotifyToken(
                        user_id=user_id,
                        access_token=access_token,
                        refresh_token=refresh_token or "",
                        expiration=expiration,
                    )
                )
            db.commit()

    async def _refresh_token(self, user_id: int) -> SpotifyToken | None:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if not token or not token.refresh_token:
                return None

            auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    "https://accounts.spotify.com/api/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": token.refresh_token,
                    },
                    headers={
                        "Authorization": f"Basic {b64_auth}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )

            data = response.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in")
            if not access_token or not expires_in:
                logger.error("Spotify refresh failed: %s", data)
                return None

            token.access_token = access_token
            token.expiration = datetime.utcnow() + timedelta(seconds=int(expires_in))
            db.commit()
            db.refresh(token)
            return token

    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()

        if not token:
            return None

        async def fetch_current(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                return await client.get(
                    "https://api.spotify.com/v1/me/player/currently-playing",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

        async def fetch_recent(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                return await client.get(
                    "https://api.spotify.com/v1/me/player/recently-played?limit=1",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

        response = await fetch_current(token.access_token)
        if response.status_code == 401:
            refreshed = await self._refresh_token(user_id)
            if refreshed:
                response = await fetch_current(refreshed.access_token)

        if response.status_code == 200:
            data = response.json()
            item = data.get("item")
            if item:
                return self._map_track(item, source="spotify_current", played_at=None)

        recent = await fetch_recent(token.access_token)
        if recent.status_code == 401:
            refreshed = await self._refresh_token(user_id)
            if refreshed:
                recent = await fetch_recent(refreshed.access_token)

        if recent.status_code != 200:
            logger.error("Spotify recent error: %s", recent.text)
            return None

        items = recent.json().get("items") or []
        if not items:
            return None

        return self._map_track(
            items[0].get("track") or {},
            source="spotify_last",
            played_at=items[0].get("played_at"),
        )

    def _map_track(self, item: dict[str, Any], source: str, played_at: str | None) -> dict[str, Any] | None:
        if not item:
            return None
        album = item.get("album") or {}
        artists = item.get("artists") or []
        artist = artists[0].get("name") if artists else ""
        images = album.get("images") or []
        return {
            "source": source,
            "played_at": played_at,
            "track_name": item.get("name") or "",
            "artist": artist,
            "album": album.get("name") or "",
            "album_name": album.get("name") or "",
            "track_id": item.get("id"),
            "spotify_url": (item.get("external_urls") or {}).get("spotify"),
            "album_url": (album.get("external_urls") or {}).get("spotify"),
            "album_image_url": images[0].get("url") if images else None,
        }

    async def clear_user_session(self, user_id: int) -> bool:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if token:
                db.delete(token)
                db.commit()
        return True


spotify_service = SpotifyService()
