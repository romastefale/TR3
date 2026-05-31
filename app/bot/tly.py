"""/tly — igual ao /tcanvas (Spotify Canvas em vídeo), mas com legenda enxuta:

    {nome em negrito unicode} · ♫ N · faixa — artista
    [quote expansível com o refrão da letra]

A letra vem do lyrics.ovh (sem chave). O trecho é o refrão (parte que mais se
repete); sem refrão detectável, cai nas primeiras linhas. Sem letra, sai só o
cabeçalho. Mesmo fallback silencioso do /tcanvas (vídeo → foto → texto).

O envio/cache do vídeo (reuso de file_id, canal de arquivo, fallback) fica no
helper compartilhado `deliver_canvas` (mesma lógica do /tcanvas, sem botões).
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.canvas_delivery import deliver_canvas
from app.bot.telegram import build_tly_payload
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.lyrics import lyrics_service
from app.services.music import music_service

logger = logging.getLogger(__name__)
router = Router()

# Cooldown por user (mesma lógica/janela do /tcanvas): 1 /tly a cada 5s.
_TLY_COOLDOWN_SECONDS = 5.0
_TLY_USER_BOUND = 5000
_tly_last_use: dict[int, float] = {}


def _check_cooldown(user_id: int) -> float | None:
    now = time.monotonic()
    last = _tly_last_use.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < _TLY_COOLDOWN_SECONDS:
        return _TLY_COOLDOWN_SECONDS - elapsed
    if len(_tly_last_use) >= _TLY_USER_BOUND:
        _tly_last_use.clear()
    _tly_last_use[user_id] = now
    return None


@router.message(Command("tly"))
async def tly(message: Message) -> None:
    if not message.from_user:
        return
    if not is_user_connected(message.from_user.id):
        await message.answer(
            connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True
        )
        return

    remaining = _check_cooldown(message.from_user.id)
    if remaining is not None:
        await message.answer(f"Aguarda {remaining:.0f}s antes de pedir outro.")
        return

    track = await music_service.get_current_or_last_played(message.from_user.id)
    if not track:
        await message.answer(
            "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo."
        )
        return

    # Letra: best-effort. Qualquer falha vira None (sai só o cabeçalho).
    artist_raw = str(track.get("artist") or "").strip()
    track_name_raw = str(track.get("track_name") or "").strip()
    lyric_snippet: str | None = None
    if artist_raw and track_name_raw:
        try:
            lyric_snippet = await lyrics_service.get_snippet(artist_raw, track_name_raw)
        except Exception:
            logger.exception("TLY_LYRICS_FAILED artist=%s track=%s", artist_raw, track_name_raw)

    payload = await build_tly_payload(message, track, lyric_snippet)
    if not payload:
        await message.answer("Erro ao identificar a música.")
        return
    track_id, caption, cover, card_emoji = payload

    await deliver_canvas(
        message,
        track=track,
        track_id=track_id,
        caption=caption,
        cover=cover,
        card_emoji=card_emoji,
        keyboard=None,
        log_prefix="TLY",
    )
