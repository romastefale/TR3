"""/SAT — gera card com Top 10 faixas de uma playlist do Spotify.

Fluxos suportados:
- Argumento:   /sat https://open.spotify.com/playlist/...
- Reply:       responde uma mensagem contendo o link, mandando só "/sat"
- Interativo:  /sat sozinho → bot pede o link e aguarda a próxima mensagem
               do mesmo usuário no mesmo chat (FSM).

Funciona em privado e em grupos. Sem botão de editar, sem inline keyboard.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message

from app.services.sat_card import (
    extract_playlist_id_async,
    fetch_playlist,
    render_sat_card,
)

logger = logging.getLogger(__name__)
router = Router(name="sat")


class SatStates(StatesGroup):
    waiting_link = State()


def _caption(display_name: str, user_id: int) -> str:
    safe_name = html.escape(display_name or "Usuário")
    return (
        "📊 <b>Seu Ranking Vitalício (All-Time Top Songs)</b>\n"
        f'👤 <b>Usuário:</b> <a href="tg://user?id={user_id}">{safe_name}</a>\n'
        "🎵 <i>Top 10 faixas da playlist extraída.</i>"
    )


def _display_name(message: Message) -> str:
    u = message.from_user
    if not u:
        return "Usuário"
    return (u.full_name or u.username or "Usuário").strip() or "Usuário"


async def _generate_and_send(message: Message, playlist_id: str) -> None:
    status: Message | None = None
    try:
        status = await message.answer("Buscando a playlist…")
        user_id = message.from_user.id if message.from_user else None
        playlist = await fetch_playlist(playlist_id, user_id=user_id)
        if not playlist:
            await status.edit_text(
                "Não consegui acessar essa playlist. Se ela é "
                "<b>personalizada</b> (Discover Weekly, Daily Mix, On Repeat, "
                "Release Radar…) ou <b>privada</b>, faça /login no Spotify "
                "primeiro — só o dono consegue abrir esse tipo de playlist. "
                "Se já fez login, confira se o link está correto.",
                parse_mode="HTML",
            )
            return

        await status.edit_text("Renderizando o card…")
        card_bytes = await render_sat_card(playlist)
        if not card_bytes:
            await status.edit_text(
                "Não consegui gerar o card agora. Tente novamente em alguns instantes."
            )
            return

        try:
            await status.delete()
        except Exception:
            logger.debug("SAT status delete failed", exc_info=True)

        await message.answer_photo(
            photo=BufferedInputFile(card_bytes, filename="sat-card.jpg"),
            caption=_caption(_display_name(message), message.from_user.id),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("SAT generation failed | playlist_id=%s", playlist_id)
        try:
            if status is not None:
                await status.edit_text(
                    "Algo deu errado ao gerar o card. Tente novamente."
                )
            else:
                await message.answer(
                    "Algo deu errado ao gerar o card. Tente novamente."
                )
        except Exception:
            logger.debug("SAT failure message failed", exc_info=True)


@router.message(Command("sat", "SAT", ignore_case=True))
async def sat_command(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    # 1) Argumento direto: /sat <link>
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        pid = await extract_playlist_id_async(parts[1])
        if pid:
            await state.clear()
            await _generate_and_send(message, pid)
            return
        await message.answer(
            "Link do Spotify inválido. Envie uma URL como "
            "<code>https://open.spotify.com/playlist/...</code>.",
            parse_mode="HTML",
        )
        return

    # 2) Reply para mensagem com link do Spotify
    reply = message.reply_to_message
    if reply is not None:
        candidate = reply.text or reply.caption or ""
        pid = await extract_playlist_id_async(candidate)
        if pid:
            await state.clear()
            await _generate_and_send(message, pid)
            return

    # 3) Fluxo interativo: pede o link e fica esperando
    await state.set_state(SatStates.waiting_link)
    await message.answer(
        "Envie o link da playlist copiado do Spotify para gerar o seu card."
    )


@router.message(SatStates.waiting_link, Command("cancel", ignore_case=True))
async def sat_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Beleza, cancelado.")


@router.message(SatStates.waiting_link, F.text)
async def sat_receive_link(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    pid = await extract_playlist_id_async(message.text or "")
    if not pid:
        await message.answer(
            "Não reconheci esse link. Envie uma URL de playlist do Spotify "
            "(ex.: <code>https://open.spotify.com/playlist/...</code>) ou "
            "/cancel para sair.",
            parse_mode="HTML",
        )
        return
    await state.clear()
    await _generate_and_send(message, pid)
