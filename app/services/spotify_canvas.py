from __future__ import annotations

import logging
import re

import httpx

from app.config.settings import (
    SPOTIFY_CANVAS_ENABLED,
    SPOTIFY_CANVAS_SP_DC,
    SPOTIFY_CANVAS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

CANVAS_TOKEN_URL = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
CANVAS_API_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
CANVAS_URL_RE = re.compile(rb"https://canvaz\.scdn\.co/[^\x00\s\"'<>]+")
WEB_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
    "App-Platform": "WebPlayer",
    "Origin": "https://open.spotify.com",
    "Referer": "https://open.spotify.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
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
    async def get_canvas_url(self, track_id: str) -> str | None:
        clean_track_id = (track_id or "").strip()
        if not SPOTIFY_CANVAS_ENABLED:
            logger.info("Spotify Canvas skipped: disabled")
            return None
        if not SPOTIFY_CANVAS_SP_DC:
            logger.info("Spotify Canvas skipped: missing SPOTIFY_CANVAS_SP_DC")
            return None
        if not clean_track_id:
            logger.info("Spotify Canvas skipped: empty track_id")
            return None

        try:
            access_token = await self._get_access_token()
            if not access_token:
                logger.warning("Spotify Canvas skipped: token unavailable")
                return None
            canvas_url = await self._fetch_canvas_url(clean_track_id, access_token)
            if canvas_url:
                logger.info("Spotify Canvas found: track_id=%s", clean_track_id)
            else:
                logger.info("Spotify Canvas not found: track_id=%s", clean_track_id)
            return canvas_url
        except Exception:
            logger.exception("Spotify Canvas lookup failed: track_id=%s", clean_track_id)
            return None

    async def _get_access_token(self) -> str | None:
        headers = dict(WEB_HEADERS)
        headers["Cookie"] = f"sp_dc={SPOTIFY_CANVAS_SP_DC}"
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(CANVAS_TOKEN_URL, headers=headers)
        if response.status_code != 200:
            logger.warning("Spotify Canvas token failed: status=%s body=%s", response.status_code, response.text[:200])
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("Spotify Canvas token failed: non-json response status=%s body=%s", response.status_code, response.text[:200])
            return None
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            logger.warning("Spotify Canvas token failed: token key missing keys=%s", sorted(data.keys()))
            return None
        return str(token)

    async def _fetch_canvas_url(self, track_id: str, access_token: str) -> str | None:
        payload = _encode_canvas_request(track_id)
        headers = dict(WEB_HEADERS)
        headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/x-protobuf",
                "Accept": "application/x-protobuf",
            }
        )
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
