from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.services.lastfm import lastfm_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)
_installed = False
_original_get_current_or_last_played: Callable[[int], Awaitable[dict[str, Any] | None]] | None = None


def install_music_proxy() -> None:
    global _installed, _original_get_current_or_last_played
    if _installed:
        return

    _original_get_current_or_last_played = spotify_service.get_current_or_last_played

    async def get_current_or_last_played(user_id: int) -> dict[str, Any] | None:
        try:
            lastfm_track = await lastfm_service.get_current_or_last_played(user_id)
            if lastfm_track and lastfm_track.get("track_id"):
                return lastfm_track
        except Exception:
            logger.exception("Last.fm proxy failed | user_id=%s", user_id)

        if _original_get_current_or_last_played is None:
            return None
        return await _original_get_current_or_last_played(user_id)

    spotify_service.get_current_or_last_played = get_current_or_last_played  # type: ignore[method-assign]
    _installed = True
