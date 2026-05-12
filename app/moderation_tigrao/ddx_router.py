from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.moderation_tigrao.keyboards import ddx_keyboard, home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback
from app.moderation_tigrao.state import get_session
from app.moderation_tigrao.storage import get_ddx_filters
from app.moderation_tigrao.texts import error_text

router = Router(name="moderation_tigrao_ddx")


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar o DDX.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _ddx_list_text() -> str:
    session = get_session()
    if not session.selected_chat_id:
        return _need_group_text()

    row = get_ddx_filters(int(session.selected_chat_id))
    if not row:
        return (
            "Tigrão — filtros DDX\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Nenhum filtro cadastrado."
        )

    try:
        words = json.loads(str(row.get("words") or "[]"))
    except Exception:
        words = []

    if not isinstance(words, list):
        words = []

    enabled = "ativo" if row.get("enabled") else "inativo"
    words_text = "\n".join(f"- {word}" for word in words) if words else "nenhum"

    return (
        "Tigrão — filtros DDX\n\n"
        f"Grupo: {session.selected_chat_id}\n"
        f"Status: {enabled}\n"
        f"Atualizado em: {row.get('updated_at') or '-'}\n\n"
        f"Palavras:\n{words_text}"
    )


@router.callback_query(F.data == "tigrao:ddx:list")
async def tigrao_ddx_list(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    if callback.message:
        await callback.message.edit_text(_ddx_list_text(), reply_markup=ddx_keyboard())
    await callback.answer()
