from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config.settings import (
    SPOTIFY_CANVAS_ENABLED,
    SPOTIFY_CANVAS_SP_DC,
    SPOTIFY_CANVAS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

CANVAS_TOKEN_URL = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
CANVAS_API_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"


@dataclass(slots=True)
class CanvasResult:
    url: str


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


def _find_canvas_url(data: bytes) -> str | None:
    for _, payload in _iter_length_delimited_fields(data):
        if payload.startswith(b"http") and b"canvaz.scdn.co" in payload:
            try:
                return payload.decode()
            except UnicodeDecodeError:
                continue
        nested = _find_canvas_url(payload)
        if nested:
            return nested
    return None


class SpotifyCanvasService:
    async def get_canvas_url(self, track_id: str) -> str | None:
        if not SPOTIFY_CANVAS_ENABLED:
            return None
        if not SPOTIFY_CANVAS_SP_DC:
            return None
        clean_track_id = (track_id or "").strip()
        if not clean_track_id:
            return None

        try:
            access_token = await self._get_access_token()
            if not access_token:
                return None
            return await self._fetch_canvas_url(clean_track_id, access_token)
        except Exception:
            logger.exception("Spotify Canvas lookup failed")
            return None

    async def _get_access_token(self) -> str | None:
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS) as client:
            response = await client.get(
                CANVAS_TOKEN_URL,
                headers={"Cookie": f"sp_dc={SPOTIFY_CANVAS_SP_DC}"},
            )
        if response.status_code != 200:
            logger.warning("Spotify Canvas token failed: status=%s", response.status_code)
            return None
        token = response.json().get("accessToken")
        return str(token) if token else None

    async def _fetch_canvas_url(self, track_id: str, access_token: str) -> str | None:
        payload = _encode_canvas_request(track_id)
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS) as client:
            response = await client.post(
                CANVAS_API_URL,
                content=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/x-protobuf",
                    "Accept": "application/x-protobuf",
                },
            )
        if response.status_code != 200:
            logger.warning("Spotify Canvas API failed: status=%s", response.status_code)
            return None
        return _find_canvas_url(response.content)


spotify_canvas_service = SpotifyCanvasService()
