from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import Dispatcher

from app.services.lastfm import lastfm_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)
_installed = False
_original_get_current_or_last_played: Callable[[int], Awaitable[dict[str, Any] | None]] | None = None
_original_register_handlers: Callable[[Dispatcher], None] | None = None


def _install_extra_music_handlers() -> None:
    global _original_register_handlers
    try:
        from app.bot import telegram as telegram_module
        from app.bot.music_extras import register_music_extra_handlers
    except Exception:
        logger.exception("Could not import extra music handlers")
        return

    if _original_register_handlers is not None:
        return

    _original_register_handlers = telegram_module._register_handlers

    def wrapped_register_handlers(dispatcher: Dispatcher) -> None:
        assert _original_register_handlers is not None
        _original_register_handlers(dispatcher)
        register_music_extra_handlers(dispatcher)

    telegram_module._register_handlers = wrapped_register_handlers


def install_music_proxy() -> None:
    global _installed, _original_get_current_or_last_played
    if _installed:
        return

    _install_extra_music_handlers()
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
