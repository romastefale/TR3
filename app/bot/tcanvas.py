"""/tcanvas — manda o Spotify Canvas (vídeo curto vertical) da música atual.

Mesma legenda e botões do /playing. Se a música não tiver Canvas (ou o
download falhar), cai SILENCIOSAMENTE no fluxo do /playing — o user sempre
recebe alguma coisa útil.

Abordagem: usa o endpoint não-documentado `spclient.wg.spotify.com/canvaz-cache`
com um Bearer token anônimo do web player (`open.spotify.com/get_access_token`).
Não envolve OAuth do usuário. Mesma técnica usada por canvasdownloader.com e
github.com/bartleyg/my-spotify-canvas.

O envio/cache do vídeo (reuso de file_id, canal de arquivo, fallback) fica no
helper compartilhado `deliver_canvas` (mesma lógica do /tly).
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.canvas_delivery import deliver_canvas
from app.bot.telegram import build_playing_payload
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.music import music_service

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

    await deliver_canvas(
        message,
        track=track,
        track_id=track_id,
        caption=caption,
        cover=cover,
        card_emoji=card_emoji,
        keyboard=keyboard,
        log_prefix="TCANVAS",
    )
