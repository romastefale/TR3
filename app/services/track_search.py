"""Busca de faixas por termo livre (multi-resultado) via API pública do Deezer.

Serviço reutilizável: usado pelo comando `/radiofm` (lista de candidatos em
botões) e pelo inline público (resultados com miniatura). O Deezer não exige
auth e já é usado no projeto para capas (`app/services/lastfm.py`).

Sem contadores de play/like e sem registro de reações — só metadados da faixa
(título, artista, capa grande/miniatura e link).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEEZER_SEARCH_URL = "https://api.deezer.com/search"
_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class TrackHit:
    track_id: str
    title: str
    artist: str
    cover_big: str | None
    cover_thumb: str | None
    url: str | None


async def search_tracks(term: str, *, limit: int = 8) -> list[TrackHit]:
    """Retorna até `limit` faixas pro termo. Nunca levanta: erro/sem match -> []."""
    q = (term or "").strip()
    if not q:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(_DEEZER_SEARCH_URL, params={"q": q, "limit": str(limit)})
            if resp.status_code != 200:
                logger.info("TRACK_SEARCH_NON200 status=%s term=%s", resp.status_code, q)
                return []
            data = resp.json()
    except Exception:
        logger.info("TRACK_SEARCH_FAILED term=%s", q, exc_info=True)
        return []

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    hits: list[TrackHit] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        artist = str((item.get("artist") or {}).get("name") or "").strip()
        if not title or not artist:
            continue
        dedup = f"{title.lower()}|{artist.lower()}"
        if dedup in seen:
            continue
        seen.add(dedup)
        album = item.get("album") or {}
        cover_big = album.get("cover_big") or album.get("cover_medium")
        cover_thumb = album.get("cover_small") or album.get("cover_medium")
        url = item.get("link")
        hits.append(
            TrackHit(
                track_id=str(item.get("id") or "").strip(),
                title=title,
                artist=artist,
                cover_big=str(cover_big) if cover_big else None,
                cover_thumb=str(cover_thumb) if cover_thumb else None,
                url=str(url) if url else None,
            )
        )
        if len(hits) >= limit:
            break
    return hits
