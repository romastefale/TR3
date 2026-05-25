from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.monthfm import _finish_monthfm
from app.bot.weekfm import _finish_weekfm

logger = logging.getLogger(__name__)
router = Router(name="myself")


def _menu_keyboard(requester_id: int) -> InlineKeyboardMarkup:
    # Bot API 9.4 (fev/2026): InlineKeyboardButton ganhou o campo `style`
    # com valores "success" (verde), "danger" (vermelho) e "primary" (azul).
    # aiogram 3.27 expõe direto. Sem emoji, só cor de fundo do botão.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Semanal",
                    callback_data=f"myself:w:{requester_id}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Mensal",
                    callback_data=f"myself:m:{requester_id}",
                    style="danger",
                ),
            ]
        ]
    )


@router.message(Command("myself"))
async def myself(message: Message) -> None:
    """Porta de entrada para /weekfm e /monthfm via botões.

    Liberado pra todos os membros (é o extrato individual do próprio
    usuário). Em chat de grupo, apenas o user que rodou o comando pode
    clicar nos botões — `callback_data` carrega o `requester_id`.
    """
    if not message.from_user:
        return
    requester = message.from_user
    from app.services.connection_check import connect_hint_for, is_user_connected

    if not is_user_connected(requester.id):
        await message.answer(
            connect_hint_for(message.chat.type),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Sprint 11: effect PARTY em DM (toda vez user abre o extrato), graceful em grupo.
    from app.bot.telegram import _answer_with_effect, _EFFECT_PARTY
    await _answer_with_effect(
        message,
        "♫ Qual extrato você quer?\n"
        "Escolha o período do seu Last.fm:",
        _EFFECT_PARTY,
        reply_markup=_menu_keyboard(requester.id),
    )


@router.callback_query(F.data.startswith("myself:"))
async def myself_callback(query: CallbackQuery) -> None:
    if not query.from_user or not query.data or not query.message:
        await query.answer()
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, period, raw_requester = parts
    try:
        requester_id = int(raw_requester)
    except ValueError:
        await query.answer()
        return
    if query.from_user.id != requester_id:
        await query.answer(
            "Esse menu é do outro usuário. Rode /myself você mesmo.",
            show_alert=True,
        )
        return
    if period not in {"w", "m"}:
        await query.answer()
        return

    await query.answer()
    display_name = query.from_user.full_name or "Usuário"
    label = "semana" if period == "w" else "mês"
    try:
        await query.message.edit_text(f"Gerando extrato do {label} no Last.fm...")
        status = query.message
    except Exception:
        # Mensagem não pôde ser editada (idade, permissões) — manda nova.
        logger.warning("MYSELF_EDIT_FAILED", exc_info=True)
        status = await query.message.answer(f"Gerando extrato do {label} no Last.fm...")

    if period == "w":
        asyncio.create_task(
            _finish_weekfm(
                status,
                user_id=requester_id,
                display_name=display_name,
                raw_week=None,
            )
        )
    else:
        asyncio.create_task(
            _finish_monthfm(
                status,
                user_id=requester_id,
                display_name=display_name,
                raw_month=None,
            )
        )
