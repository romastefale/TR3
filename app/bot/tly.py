"""/tly — igual ao /tcanvas (Spotify Canvas em vídeo), mas com legenda enxuta:

    {nome em negrito unicode} · ♫ N · faixa — artista
    [quote expansível com o refrão da letra]

A letra vem do lyrics.ovh (sem chave). O trecho é o refrão (parte que mais se
repete); sem refrão detectável, cai nas primeiras linhas. Sem letra, sai só o
cabeçalho. Mesmo fallback silencioso do /tcanvas (vídeo → foto → texto).
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.bot.telegram import build_tly_payload, _react_to_own_card
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.lyrics import lyrics_service
from app.services.music import music_service
from app.services.reactions import reactions_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service

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


async def _send_fallback(message: Message, caption: str, cover: str | None) -> Message:
    """Fallback silencioso: foto (capa) ou texto, sem botões."""
    if cover:
        return await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML")
    return await message.answer(caption, parse_mode="HTML")


async def _register_card(sent: Message, track: dict, track_id: str, owner_user_id: int) -> None:
    await reactions_service.register_card(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        track_id=track_id,
        owner_user_id=owner_user_id,
        track_name=str(track.get("track_name") or "").strip() or None,
        artist_name=str(track.get("artist") or "").strip() or None,
    )


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

    # Canvas precisa do Spotify track_id base62. Last.fm-first chega como
    # "lfm:<sha1>" — resolve via Spotify Search (igual ao /tcanvas). Mantém o
    # track_id original pro _register_card (chave histórica dos likes).
    canvas_track_id = track_id
    if track_id.startswith("lfm:"):
        if artist_raw and track_name_raw:
            try:
                match = await spotify_service.search_track(artist_raw, track_name_raw)
                if match and match.get("id"):
                    canvas_track_id = match["id"]
                    logger.info(
                        "TLY_RESOLVED lfm=%s -> spotify=%s artist=%s track=%s",
                        track_id, canvas_track_id, artist_raw, track_name_raw,
                    )
                else:
                    logger.info(
                        "TLY_RESOLVE_MISS lfm=%s artist=%s track=%s",
                        track_id, artist_raw, track_name_raw,
                    )
            except Exception:
                logger.exception(
                    "TLY_RESOLVE_ERROR lfm=%s artist=%s track=%s",
                    track_id, artist_raw, track_name_raw,
                )

    canvas_url = await spotify_canvas_service.get_canvas_url(canvas_track_id)
    if not canvas_url:
        logger.info("TLY_NO_CANVAS track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover)
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
        return

    canvas_bytes = await spotify_canvas_service.download_canvas_bytes(canvas_url)
    if not canvas_bytes:
        logger.info("TLY_DOWNLOAD_FAILED track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover)
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
        return

    try:
        sent = await message.answer_video(
            video=BufferedInputFile(canvas_bytes, filename=f"canvas-{track_id}.mp4"),
            caption=caption,
            parse_mode="HTML",
        )
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
    except Exception:
        logger.exception("TLY_SEND_FAILED track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover)
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
