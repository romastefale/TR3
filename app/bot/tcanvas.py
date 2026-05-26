"""/tcanvas — manda o Spotify Canvas (vídeo curto vertical) da música atual.

Mesma legenda e botões do /playing. Se a música não tiver Canvas (ou o
download falhar), cai SILENCIOSAMENTE no fluxo do /playing — o user sempre
recebe alguma coisa útil.

Abordagem: usa o endpoint não-documentado `spclient.wg.spotify.com/canvaz-cache`
com um Bearer token anônimo do web player (`open.spotify.com/get_access_token`).
Não envolve OAuth do usuário. Mesma técnica usada por canvasdownloader.com e
github.com/bartleyg/my-spotify-canvas.
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.bot.telegram import build_playing_payload, _react_to_own_card
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.music import music_service
from app.services.reactions import reactions_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service

logger = logging.getLogger(__name__)
router = Router()

# Cooldown C: bloqueia o MESMO user de disparar /tcanvas em sequência
# rápida. Evita spam acidental (toque duplo) e ataque trivial de DoS de
# 1 user só. Janela curta — 5s é suficiente pra não atrapalhar uso legítimo
# (lookup completo demora ~2-4s) mas frear loops. Dict simples user_id ->
# timestamp do último uso. Bound de memória: se >5000 users, descarta
# o dict inteiro (vai recriar conforme uso).
_TCANVAS_COOLDOWN_SECONDS = 5.0
_TCANVAS_USER_BOUND = 5000
_tcanvas_last_use: dict[int, float] = {}


def _check_cooldown(user_id: int) -> float | None:
    """Retorna segundos restantes se o user está em cooldown, senão None.
    Quando libera, registra o timestamp atual.
    """
    now = time.monotonic()
    last = _tcanvas_last_use.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < _TCANVAS_COOLDOWN_SECONDS:
        return _TCANVAS_COOLDOWN_SECONDS - elapsed
    if len(_tcanvas_last_use) >= _TCANVAS_USER_BOUND:
        _tcanvas_last_use.clear()
    _tcanvas_last_use[user_id] = now
    return None


async def _send_fallback(message: Message, caption: str, cover: str | None, keyboard) -> Message:
    """Fallback silencioso: mesmo resultado do /playing normal."""
    if cover:
        return await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    return await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


async def _register_card(sent: Message, track: dict, track_id: str, owner_user_id: int) -> None:
    """Sprint 8: registra card pra reactions tracking. Mesma lógica do /playing."""
    await reactions_service.register_card(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        track_id=track_id,
        owner_user_id=owner_user_id,
        track_name=str(track.get("track_name") or "").strip() or None,
        artist_name=str(track.get("artist") or "").strip() or None,
    )


@router.message(Command("tcanvas"))
async def tcanvas(message: Message) -> None:
    if not message.from_user:
        return
    if not is_user_connected(message.from_user.id):
        await message.answer(
            connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True
        )
        return

    # Cooldown: 1 /tcanvas por user a cada 5s. Resposta amigável, sem log
    # ruidoso (qualquer pessoa que clica 2x rápido cai aqui — esperado).
    remaining = _check_cooldown(message.from_user.id)
    if remaining is not None:
        await message.answer(
            f"Aguarda {remaining:.0f}s antes de pedir outro Canvas."
        )
        return

    track = await music_service.get_current_or_last_played(message.from_user.id)
    if not track:
        await message.answer(
            "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo."
        )
        return

    payload = await build_playing_payload(message, track)
    if not payload:
        await message.answer("Erro ao identificar a música.")
        return
    track_id, caption, cover, keyboard, card_emoji = payload

    # Canvas precisa de Spotify track_id base62. Quando o music_service
    # devolve Last.fm-first, track_id chega como "lfm:<sha1>" — hash interno
    # que nunca resolve no canvaz-cache. Resolve via Spotify Search API
    # (Client Credentials, sem OAuth do user). Cache em search_track evita
    # round-trip repetido. Mantém o track_id original pra _register_card
    # (likes DB usa "lfm:" como chave histórica — não mexer).
    canvas_track_id = track_id
    if track_id.startswith("lfm:"):
        artist = str(track.get("artist") or "").strip()
        track_name = str(track.get("track_name") or "").strip()
        if artist and track_name:
            try:
                match = await spotify_service.search_track(artist, track_name)
                if match and match.get("id"):
                    canvas_track_id = match["id"]
                    logger.info(
                        "TCANVAS_RESOLVED lfm=%s -> spotify=%s artist=%s track=%s",
                        track_id, canvas_track_id, artist, track_name,
                    )
                else:
                    logger.info(
                        "TCANVAS_RESOLVE_MISS lfm=%s artist=%s track=%s",
                        track_id, artist, track_name,
                    )
            except Exception:
                logger.exception(
                    "TCANVAS_RESOLVE_ERROR lfm=%s artist=%s track=%s",
                    track_id, artist, track_name,
                )

    canvas_url = await spotify_canvas_service.get_canvas_url(canvas_track_id)
    if not canvas_url:
        logger.info("TCANVAS_NO_CANVAS track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover, keyboard)
        await _register_card(sent, track, track_id, message.from_user.id)
        # Sprint 10: bot reage no card (mesma lógica do /playing).
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
        return

    canvas_bytes = await spotify_canvas_service.download_canvas_bytes(canvas_url)
    if not canvas_bytes:
        logger.info("TCANVAS_DOWNLOAD_FAILED track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover, keyboard)
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
        return

    try:
        sent = await message.answer_video(
            video=BufferedInputFile(canvas_bytes, filename=f"canvas-{track_id}.mp4"),
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
    except Exception:
        logger.exception("TCANVAS_SEND_FAILED track_id=%s", track_id)
        sent = await _send_fallback(message, caption, cover, keyboard)
        await _register_card(sent, track, track_id, message.from_user.id)
        await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)
