"""DDX Soft router — espelho do ddx_router.py com prefixo `tigrao:ddx_soft:*`.

Cadastra/remove/lista/desliga as palavras Soft (lei dos 10 minutos).
Mesmo fluxo e UX do DDX hard, mas tabela separada.
"""
from __future__ import annotations

import json
import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.ddx_soft_runtime import cancel_scheduled_delete
from app.moderation_tigrao.keyboards import ddx_soft_keyboard, home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, consume_if_expired, get_session, set_action
from app.moderation_tigrao.storage import (
    get_ddx_soft_filters,
    load_ddx_soft_words,
    log_action,
    set_ddx_soft_filters,
)
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_ddx_soft")


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar o DDX 10min.",
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


def _ddx_soft_list_text() -> str:
    session = get_session()
    if not session.selected_chat_id:
        return _need_group_text()

    row = get_ddx_soft_filters(int(session.selected_chat_id))
    if not row:
        return (
            "Tigrão — filtros DDX 10min\n\n"
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
        "Tigrão — filtros DDX 10min\n\n"
        f"Grupo: {session.selected_chat_id}\n"
        f"Status: {enabled}\n"
        f"Atualizado em: {row.get('updated_at') or '-'}\n\n"
        f"Palavras:\n{words_text}"
    )


@router.callback_query(F.data == "tigrao:ddx_soft:menu")
async def tigrao_ddx_soft_menu(callback: CallbackQuery) -> None:
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
        await callback.message.edit_text(
            "Tigrão — Filtros DDX 10min\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Palavras desta lista são apagadas DEPOIS DE 10 MINUTOS quando "
            "detectadas. Lista independente do DDX (que apaga imediato).",
            reply_markup=ddx_soft_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:ddx_soft:add")
async def tigrao_ddx_soft_add(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("ddx_soft_add", waiting_for="ddx_soft_add_words")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — adicionar filtro DDX 10min\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie as palavras ou frases que devem ser apagadas DEPOIS DE 10 MINUTOS.\n"
            "Pode separar por vírgula, ponto e vírgula ou linha."
        )
    await callback.answer()


@router.message(F.text, lambda message: get_session().waiting_for == "ddx_soft_add_words")
async def tigrao_ddx_soft_receive_add_words(message: Message) -> None:
    if not is_owner_private_message(message):
        return

    if consume_if_expired():
        await message.answer(
            error_text(
                "Sessão expirada",
                "O fluxo de adicionar filtro DDX 10min expirou (15 min).",
                "Use /tigrao para recomeçar.",
            )
        )
        return

    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    incoming = _parse_words(message.text or "")
    if not incoming:
        await message.answer(
            error_text(
                "Nenhum filtro válido",
                "Não encontrei palavra ou frase para salvar.",
                "Envie ao menos uma palavra ou frase.",
            ),
            reply_markup=ddx_soft_keyboard(),
        )
        return

    chat_id = int(session.selected_chat_id)
    current = load_ddx_soft_words(chat_id)
    final_words = list(dict.fromkeys(current + incoming))
    set_ddx_soft_filters(chat_id, final_words, enabled=True)
    log_action(chat_id=chat_id, action="ddx_soft_add", status="success")
    clear_action()

    await message.answer(
        success_text(
            "Filtro DDX 10min atualizado",
            f"Grupo: {chat_id}\nAdicionados: {len(incoming)}\nTotal de filtros: {len(final_words)}",
        ),
        reply_markup=ddx_soft_keyboard(),
    )


@router.callback_query(F.data == "tigrao:ddx_soft:remove")
async def tigrao_ddx_soft_remove(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("ddx_soft_remove", waiting_for="ddx_soft_remove_words")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — remover filtro DDX 10min\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie as palavras ou frases que devem ser removidas.\n"
            "Pode separar por vírgula, ponto e vírgula ou linha."
        )
    await callback.answer()


@router.message(F.text, lambda message: get_session().waiting_for == "ddx_soft_remove_words")
async def tigrao_ddx_soft_receive_remove_words(message: Message) -> None:
    if not is_owner_private_message(message):
        return

    if consume_if_expired():
        await message.answer(
            error_text(
                "Sessão expirada",
                "O fluxo de remover filtro DDX 10min expirou (15 min).",
                "Use /tigrao para recomeçar.",
            )
        )
        return

    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    remove_words = set(_parse_words(message.text or ""))
    if not remove_words:
        await message.answer(
            error_text(
                "Nenhum filtro válido",
                "Não encontrei palavra ou frase para remover.",
                "Envie ao menos uma palavra ou frase.",
            ),
            reply_markup=ddx_soft_keyboard(),
        )
        return

    chat_id = int(session.selected_chat_id)
    current = load_ddx_soft_words(chat_id)
    final_words = [word for word in current if word not in remove_words]
    removed_count = len(current) - len(final_words)
    set_ddx_soft_filters(chat_id, final_words, enabled=True)
    log_action(chat_id=chat_id, action="ddx_soft_remove", status="success")
    clear_action()

    await message.answer(
        success_text(
            "Filtro DDX 10min atualizado",
            f"Grupo: {chat_id}\nRemovidos: {removed_count}\nTotal de filtros: {len(final_words)}",
        ),
        reply_markup=ddx_soft_keyboard(),
    )


@router.callback_query(F.data == "tigrao:ddx_soft:off")
async def tigrao_ddx_soft_off(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    chat_id = int(session.selected_chat_id)
    current = load_ddx_soft_words(chat_id)
    set_ddx_soft_filters(chat_id, current, enabled=False)
    log_action(chat_id=chat_id, action="ddx_soft_off", status="success")

    if callback.message:
        await callback.message.edit_text(
            success_text(
                "DDX 10min desligado",
                f"Grupo: {chat_id}\nFiltros preservados: {len(current)}",
            ),
            reply_markup=ddx_soft_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:ddx_soft:cancel:"))
async def tigrao_ddx_soft_cancel(callback: CallbackQuery) -> None:
    """Handler do botão 'Cancelar' anexado à DM 'agendou apagamento'.
    Owner-only. Cancela a delete-task se ainda estiver pendente.
    Sessão única: remove o teclado após o click pra impedir reuso."""
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    # Esperado: tigrao:ddx_soft:cancel:<chat_id>:<message_id>
    if len(parts) != 5:
        await callback.answer("Dados inválidos.", show_alert=True)
        return
    try:
        chat_id = int(parts[3])
        message_id = int(parts[4])
    except ValueError:
        await callback.answer("Dados inválidos.", show_alert=True)
        return

    cancelled = cancel_scheduled_delete(chat_id, message_id)

    log_action(
        chat_id=chat_id,
        action="ddx_soft_cancel",
        target_user_id=callback.from_user.id if callback.from_user else None,
        status="success" if cancelled else "noop",
    )

    # Sessão única: remove o teclado sempre (mesmo no noop) pra evitar
    # cliques repetidos numa DM cuja ação já não vai surtir efeito.
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            # Telegram pode rejeitar edit em msg muito antiga; tudo
            # bem, o callback.answer abaixo ainda dá feedback.
            pass

    if cancelled:
        await callback.answer(
            "Cancelado. A mensagem não será apagada.", show_alert=True
        )
    else:
        await callback.answer(
            "Já não estava agendado (apagou, falhou ou já foi cancelado).",
            show_alert=True,
        )


@router.callback_query(F.data == "tigrao:ddx_soft:list")
async def tigrao_ddx_soft_list(callback: CallbackQuery) -> None:
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
        await callback.message.edit_text(_ddx_soft_list_text(), reply_markup=ddx_soft_keyboard())
    await callback.answer()
