"""Camada de abstração music_service.

Sprint 3.5 (a8907ac+): este módulo era um pass-through fino pro
`spotify_service`. O comportamento Last.fm-first era injetado em runtime
por `music_proxy.install_music_proxy()` (monkey-patch em
`spotify_service.get_current_or_last_played`). Isso confundia leitura do
código — o método dizia "spotify" mas devolvia Last.fm-first.

Agora a preferência Last.fm é explícita aqui. Decisão de produto: Last.fm
é a fonte primária — só ~4 users usam OAuth Spotify, o restante usa
Last.fm. Spotify entra como fallback quando Last.fm não tem dado.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.lastfm import lastfm_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)


class MusicService:
    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        """Last.fm-first; Spotify como fallback.

        Mesma lógica do antigo `music_proxy`: tenta Last.fm; se devolver
        algo com `track_id`, retorna. Senão (ou erro), cai pro Spotify.
        Exceções de Last.fm são logadas mas não propagadas — a chamada
        Spotify ainda precisa rodar pra não derrubar o handler.
        """
        try:
            lastfm_track = await lastfm_service.get_current_or_last_played(user_id)
            if lastfm_track and lastfm_track.get("track_id"):
                return lastfm_track
        except Exception:
            logger.exception("Last.fm lookup failed | user_id=%s", user_id)

        return await spotify_service.get_current_or_last_played(user_id)


music_service = MusicService()
