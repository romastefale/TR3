from __future__ import annotations

import json
import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.keyboards import ddx_keyboard, home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, get_session, set_action
from app.moderation_tigrao.storage import get_ddx_filters, load_ddx_words, log_action, set_ddx_filters
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_ddx")


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar o DDX.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _parse_words(raw: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\n]", raw):
        word = re.sub(r"\s+", " ", item.strip().lower())
        if word and word not in seen:
            seen.add(word)
            words.append(word)
    return words


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


@router.callback_query(F.data == "tigrao:ddx:add")
async def tigrao_ddx_add(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("ddx_add", waiting_for="ddx_add_words")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — adicionar filtro DDX\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie as palavras ou frases que devem ser filtradas.\n"
            "Pode separar por vírgula, ponto e vírgula ou linha."
        )
    await callback.answer()


@router.message(F.text, lambda message: get_session().waiting_for == "ddx_add_words")
async def tigrao_ddx_receive_add_words(message: Message) -> None:
    if not is_owner_private_message(message):
        return

    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    incoming = _parse_words(message.text or "")
    if not incoming:
        await message.answer(
            error_text("Nenhum filtro válido", "Não encontrei palavra ou frase para salvar.", "Envie ao menos uma palavra ou frase."),
            reply_markup=ddx_keyboard(),
        )
        return

    chat_id = int(session.selected_chat_id)
    current = load_ddx_words(chat_id)
    final_words = list(dict.fromkeys(current + incoming))
    set_ddx_filters(chat_id, final_words, enabled=True)
    log_action(chat_id=chat_id, action="ddx_add", status="success")
    clear_action()

    await message.answer(
        success_text(
            "Filtro DDX atualizado",
            f"Grupo: {chat_id}\nAdicionados: {len(incoming)}\nTotal de filtros: {len(final_words)}",
        ),
        reply_markup=ddx_keyboard(),
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
