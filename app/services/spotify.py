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


_TRACK_SEARCH_CACHE_MAX = 4096
_TRACK_SEARCH_TTL_HIT = timedelta(hours=24)
_TRACK_SEARCH_TTL_MISS = timedelta(hours=2)


class SpotifyService:
    def __init__(self) -> None:
        self._client_access_token: str | None = None
        self._client_token_expiration: datetime | None = None
        # Cache (artist_lower, title_lower) -> (url_or_None, expires_at).
        # Negative results são cacheados com TTL menor (faixas raras /
        # ambíguas evitam bater na Search API toda execução).
        # value = (record_or_None, expires_at). record = {"url": str, "cover": str|None}
        self._track_search_cache: dict[
            tuple[str, str], tuple[dict[str, str | None] | None, datetime]
        ] = {}

    async def shutdown(self) -> None:
        logger.info("Spotify service stopped.")

    def _client_token_valid(self) -> bool:
        return bool(
            self._client_access_token
            and self._client_token_expiration
            and self._client_token_expiration > datetime.utcnow() + timedelta(seconds=60)
        )

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

    async def exchange_code_for_token(self, code: str, user_id: int) -> bool | None:
        """Troca `code` por tokens e salva. Retorna:

        * `True`  — substituiu um login anterior do mesmo user_id
        * `False` — primeira conexão
        * `None`  — Spotify devolveu resposta inválida (nada foi gravado)
        """
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
            return None

        expiration = datetime.utcnow() + timedelta(seconds=int(expires_in))
        replaced = False
        with SessionLocal() as db:
            existing = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if existing:
                replaced = True
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
        return replaced

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
                mapped = self._map_track(item, source="spotify_current", played_at=None)
                if mapped is not None:
                    # Spotify pode responder 200 com is_playing=false quando o
                    # usuário pausou. Propagamos a flag (default True quando
                    # ausente, preservando o comportamento legado) para que
                    # consumidores como /tnow possam filtrar pausados.
                    mapped["is_playing"] = bool(data.get("is_playing", True))
                return mapped

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

    async def _get_client_credentials_token(self) -> str | None:
        if self._client_token_valid():
            return self._client_access_token
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            logger.error("Spotify client credentials are not configured.")
            return None

        auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {b64_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        if response.status_code != 200:
            logger.error("Spotify client credentials token failed: %s", response.text)
            return None

        data = response.json()
        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not access_token or not expires_in:
            logger.error("Invalid Spotify client credentials response: %s", data)
            return None

        self._client_access_token = str(access_token)
        self._client_token_expiration = datetime.utcnow() + timedelta(seconds=int(expires_in))
        return self._client_access_token

    async def get_track_by_id(self, track_id: str) -> dict[str, Any] | None:
        clean_track_id = (track_id or "").strip()
        if not clean_track_id:
            return None

        access_token = await self._get_client_credentials_token()
        if not access_token:
            return None

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"https://api.spotify.com/v1/tracks/{clean_track_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if response.status_code != 200:
            logger.error("Spotify track lookup failed: status=%s body=%s", response.status_code, response.text)
            return None

        return self._map_track(response.json(), source="spotify_link", played_at=None)

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

    async def search_track(self, artist: str, title: str) -> dict[str, str | None] | None:
        """Resolve artist+title -> {url, cover} via Spotify Search API.

        Usa Client Credentials (app-only auth), portanto NÃO requer que o
        usuário esteja logado no Spotify. Retorna um dict
        `{"url": "https://open.spotify.com/track/{id}", "cover": "...640px..."}`
        ou None quando não há match / API indisponível. URL e capa vêm
        no MESMO payload (sem chamadas extras). Resultados cacheados em
        memória com TTL — o ganho de capa é "de graça" depois do link.
        """
        a = (artist or "").strip()
        t = (title or "").strip()
        if not a or not t:
            return None
        key = (a.lower(), t.lower())
        now = datetime.utcnow()
        cached = self._track_search_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

        token = await self._get_client_credentials_token()
        if not token:
            return None

        # Operadores `track:` e `artist:` com aspas restringem o match aos
        # campos exatos, reduzindo falso-positivos com títulos genéricos.
        query = f'track:"{t}" artist:"{a}"'
        record: dict[str, str | None] | None = None
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    "https://api.spotify.com/v1/search",
                    params={"q": query, "type": "track", "limit": 1, "market": "BR"},
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200:
                items = ((resp.json().get("tracks") or {}).get("items") or [])
                if items:
                    item = items[0]
                    url = (item.get("external_urls") or {}).get("spotify")
                    images = (item.get("album") or {}).get("images") or []
                    # images[0] = maior resolução (640px) por convenção da API.
                    cover = images[0].get("url") if images else None
                    if url:
                        record = {"url": url, "cover": cover}
            else:
                logger.warning(
                    "Spotify search non-200 | status=%s | artist=%s | title=%s",
                    resp.status_code, a, t,
                )
        except Exception:
            logger.exception(
                "Spotify search request failed | artist=%s | title=%s", a, t
            )
            # Não cacheia erros de rede para tentar de novo logo.
            return None

        ttl = _TRACK_SEARCH_TTL_HIT if record else _TRACK_SEARCH_TTL_MISS
        self._track_search_cache[key] = (record, now + ttl)
        # Bound do cache: se exceder o limite, descarta os 25% mais antigos.
        if len(self._track_search_cache) > _TRACK_SEARCH_CACHE_MAX:
            oldest = sorted(self._track_search_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[: len(oldest) // 4]:
                self._track_search_cache.pop(k, None)
        return record

    async def clear_user_session(self, user_id: int) -> bool:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if token:
                db.delete(token)
                db.commit()
        return True


spotify_service = SpotifyService()
