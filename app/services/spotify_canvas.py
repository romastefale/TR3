from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from app.config.settings import (
    SPOTIFY_CANVAS_ENABLED,
    SPOTIFY_CANVAS_SP_DC,
    SPOTIFY_CANVAS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# Token anônimo do web player dura ~1h; usamos 50min p/ margem de segurança.
CANVAS_TOKEN_TTL_SECONDS = 50 * 60
# Canvas URL pra um track muda raramente; 24h de cache reduz drasticamente
# o tráfego pro canvaz-cache mas ainda permite refresh diário.
CANVAS_URL_CACHE_TTL_SECONDS = 24 * 3600
# Canvas é vertical 720x1280 H.264, raramente passa de 2MB. 8MB já é teto
# bem folgado — qualquer coisa maior é provavelmente bug e a gente aborta.
CANVAS_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
CANVAS_DOWNLOAD_TIMEOUT_SECONDS = 10.0

# Endpoint anônimo do web player. Funciona de IPs residenciais; IPs de
# datacenter (Railway, AWS etc) tipicamente recebem `403 URL Blocked` da
# camada upstream (Cloudflare/Akamai) — daí a necessidade do sp_dc cookie.
CANVAS_TOKEN_URL_ANON = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
# Endpoint autenticado por cookie de sessão (sp_dc) — trata como browser
# logado, atravessa o bloqueio de datacenter. Cookie dura ~1 ano; o token
# derivado dura ~1h (já cacheado em memória).
CANVAS_TOKEN_URL_COOKIE = "https://open.spotify.com/api/token?reason=transport&productType=web_player"
CANVAS_API_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
CANVAS_URL_RE = re.compile(rb"https://canvaz\.scdn\.co/[^\x00\s\"'<>]+")
TOKEN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
CANVAS_HEADERS = {
    "Accept": "application/protobuf",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Language": "en",
    "User-Agent": "Spotify/8.5.49 iOS/Version 13.3.1 (Build 17D50)",
    "Accept-Encoding": "gzip, deflate, br",
}


def _encode_varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _encode_length_delimited(field_number: int, payload: bytes) -> bytes:
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_canvas_request(track_id: str) -> bytes:
    track_uri = f"spotify:track:{track_id}".encode()
    inner = _encode_length_delimited(1, track_uri)
    outer = _encode_length_delimited(1, inner)
    return outer


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("Incomplete varint")


def _iter_length_delimited_fields(data: bytes):
    offset = 0
    while offset < len(data):
        try:
            tag, offset = _decode_varint(data, offset)
        except ValueError:
            return
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 2:
            try:
                length, offset = _decode_varint(data, offset)
            except ValueError:
                return
            payload = data[offset : offset + length]
            offset += length
            yield field_number, payload
        elif wire_type == 0:
            try:
                _, offset = _decode_varint(data, offset)
            except ValueError:
                return
        elif wire_type == 1:
            offset += 8
        elif wire_type == 5:
            offset += 4
        else:
            return


def _find_canvas_url_from_protobuf(data: bytes) -> str | None:
    for _, payload in _iter_length_delimited_fields(data):
        if payload.startswith(b"http") and b"canvaz.scdn.co" in payload:
            try:
                return payload.decode()
            except UnicodeDecodeError:
                continue
        nested = _find_canvas_url_from_protobuf(payload)
        if nested:
            return nested
    return None


def _find_canvas_url(data: bytes) -> str | None:
    protobuf_url = _find_canvas_url_from_protobuf(data)
    if protobuf_url:
        return protobuf_url
    match = CANVAS_URL_RE.search(data)
    if not match:
        return None
    try:
        return match.group(0).decode()
    except UnicodeDecodeError:
        return None


class SpotifyCanvasService:
    def __init__(self) -> None:
        # Cache do token anônimo (compartilhado entre requests).
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # Cache de URL por track_id; armazena `None` como cache negativo pra
        # não martelar o canvaz-cache em músicas que sabidamente não têm Canvas.
        self._url_cache: dict[str, tuple[str | None, float]] = {}
        self._url_lock = asyncio.Lock()

    async def get_canvas_url(self, track_id: str) -> str | None:
        clean_track_id = (track_id or "").strip()
        if not SPOTIFY_CANVAS_ENABLED:
            logger.info("Spotify Canvas skipped: disabled")
            return None
        if not clean_track_id:
            logger.info("Spotify Canvas skipped: empty track_id")
            return None

        # Fast path: cache hit sem lock.
        now = time.time()
        cached = self._url_cache.get(clean_track_id)
        if cached is not None and now < cached[1]:
            return cached[0]

        # Slow path: re-check sob lock pra não duplicar fetch.
        async with self._url_lock:
            now = time.time()
            cached = self._url_cache.get(clean_track_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            try:
                access_token = await self._get_access_token()
                if not access_token:
                    logger.warning("Spotify Canvas skipped: token unavailable")
                    # NÃO cacheia falha de token (problema transitório).
                    return None
                canvas_url = await self._fetch_canvas_url(clean_track_id, access_token)
                if canvas_url:
                    logger.info("Spotify Canvas found: track_id=%s", clean_track_id)
                else:
                    logger.info("Spotify Canvas not found: track_id=%s", clean_track_id)
                # Cacheia resultado (positivo OU negativo) com TTL.
                self._url_cache[clean_track_id] = (canvas_url, now + CANVAS_URL_CACHE_TTL_SECONDS)
                return canvas_url
            except Exception:
                logger.exception("Spotify Canvas lookup failed: track_id=%s", clean_track_id)
                return None

    async def download_canvas_bytes(self, url: str) -> bytes | None:
        """Baixa o vídeo Canvas pra memória, com teto de tamanho.

        Só aceita URLs do domínio oficial `canvaz.scdn.co` (SSRF guard).
        Retorna `None` em qualquer falha — chamador deve cair pro fallback.
        """
        if not url or not url.startswith("https://canvaz.scdn.co/"):
            logger.warning("Canvas download rejected: bad url=%s", url[:120] if url else None)
            return None
        try:
            async with httpx.AsyncClient(
                timeout=CANVAS_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Canvas download failed: status=%s url=%s", response.status_code, url
                        )
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > CANVAS_DOWNLOAD_MAX_BYTES:
                            logger.warning(
                                "Canvas download aborted: oversize (>%s bytes) url=%s",
                                CANVAS_DOWNLOAD_MAX_BYTES,
                                url,
                            )
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception:
            logger.exception("Canvas download error url=%s", url)
            return None

    async def _get_access_token(self) -> str | None:
        # Fast path: token em cache e ainda válido.
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            # Re-check sob lock.
            now = time.time()
            if self._token and now < self._token_expires_at:
                return self._token
            token = await self._fetch_access_token()
            if token:
                self._token = token
                self._token_expires_at = now + CANVAS_TOKEN_TTL_SECONDS
            return token

    async def _fetch_access_token(self) -> str | None:
        """Tenta cookie sp_dc primeiro (se configurado), cai pra anônimo.

        Em IPs de datacenter (Railway), o caminho anônimo retorna
        `403 URL Blocked` da camada upstream — daí a prioridade do cookie.
        """
        if SPOTIFY_CANVAS_SP_DC:
            token = await self._fetch_token_with_cookie()
            if token:
                return token
            logger.warning(
                "Spotify Canvas: sp_dc fallback acionado — cookie pode estar expirado/inválido"
            )
        return await self._fetch_token_anonymous()

    async def _fetch_token_with_cookie(self) -> str | None:
        headers = dict(TOKEN_HEADERS)
        headers["Cookie"] = f"sp_dc={SPOTIFY_CANVAS_SP_DC}"
        try:
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(CANVAS_TOKEN_URL_COOKIE, headers=headers)
        except Exception:
            logger.exception("Spotify Canvas cookie token request error")
            return None
        return self._extract_token(response, source="cookie")

    async def _fetch_token_anonymous(self) -> str | None:
        try:
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(CANVAS_TOKEN_URL_ANON, headers=TOKEN_HEADERS)
        except Exception:
            logger.exception("Spotify Canvas anonymous token request error")
            return None
        return self._extract_token(response, source="anon")

    def _extract_token(self, response: httpx.Response, source: str) -> str | None:
        if response.status_code == 403 and "URL Blocked" in response.text:
            # Caso clássico de IP de datacenter bloqueado pelo upstream do Spotify.
            logger.warning(
                "Spotify Canvas token BLOCKED_BY_UPSTREAM (datacenter IP block) source=%s — "
                "configure SPOTIFY_CANVAS_SP_DC com o cookie sp_dc de uma conta logada",
                source,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "Spotify Canvas token failed: source=%s status=%s body=%s",
                source,
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning(
                "Spotify Canvas token failed: non-json response source=%s body=%s",
                source,
                response.text[:200],
            )
            return None
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            logger.warning(
                "Spotify Canvas token failed: token key missing source=%s keys=%s",
                source,
                sorted(data.keys()),
            )
            return None
        is_anonymous = bool(data.get("isAnonymous"))
        logger.info("Spotify Canvas token OK source=%s isAnonymous=%s", source, is_anonymous)
        return str(token)

    async def _fetch_canvas_url(self, track_id: str, access_token: str) -> str | None:
        payload = _encode_canvas_request(track_id)
        headers = dict(CANVAS_HEADERS)
        headers["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.post(CANVAS_API_URL, content=payload, headers=headers)
        if response.status_code != 200:
            logger.warning("Spotify Canvas API failed: track_id=%s status=%s body=%s", track_id, response.status_code, response.text[:200])
            return None
        canvas_url = _find_canvas_url(response.content)
        if not canvas_url:
            logger.info("Spotify Canvas API response parsed without URL: track_id=%s bytes=%s", track_id, len(response.content))
        return canvas_url


spotify_canvas_service = SpotifyCanvasService()
