from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.keyboards import (
    ddx_keyboard,
    groups_keyboard,
    home_keyboard,
    links_keyboard,
    logs_keyboard,
    messages_keyboard,
    user_actions_keyboard,
)
from app.moderation_tigrao.parsers import parse_chat_id, parse_user_id
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import get_session, set_action, set_selected_group
from app.moderation_tigrao.storage import list_groups, remember_group
from app.moderation_tigrao.texts import error_text, home_text, success_text

router = Router(name="moderation_tigrao")

ACTION_LABELS = {
    "ban": "Banir usuário",
    "unban": "Desbanir usuário",
    "mute": "Mutar usuário",
    "unmute": "Desmutar usuário",
    "approve": "Aprovar entrada",
    "reset": "Resetar entrada",
}


def _section_text(title: str, detail: str) -> str:
    session = get_session()
    selected = ""
    if session.selected_chat_id:
        selected = f"\n\nGrupo selecionado: {session.selected_group_title or session.selected_chat_id} ({session.selected_chat_id})"
    return f"Tigrão — {title}\n\n{detail}{selected}\n\nEscolha uma opção pelos botões abaixo."


async def _edit_private_panel(callback: CallbackQuery, text: str, reply_markup) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar esta ação.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


@router.message(Command("tigrao"))
async def tigrao_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    await message.answer(home_text(), reply_markup=home_keyboard())


@router.message(F.text)
async def tigrao_private_text(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    session = get_session()

    if session.waiting_for == "chat_id":
        try:
            chat_id = parse_chat_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Chat ID inválido", str(exc), "Envie apenas o chat_id numérico, com ou sem hífen."))
            return
        remember_group(chat_id, str(chat_id))
        set_selected_group(chat_id, str(chat_id))
        await message.answer(success_text("Grupo selecionado", f"Grupo: {chat_id}"), reply_markup=home_keyboard())
        return

    if session.waiting_for == "user_id":
        try:
            user_id = parse_user_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("User ID inválido", str(exc), "Envie apenas o user_id numérico, sem hífen."))
            return
        action_label = ACTION_LABELS.get(session.selected_action or "", session.selected_action or "ação")
        session.payload["target_user_id"] = user_id
        session.waiting_for = None
        await message.answer(
            success_text(
                "Dados recebidos",
                f"Grupo: {session.selected_chat_id}\nAção: {action_label}\nUsuário: {user_id}\n\nNesta etapa a ação ainda não foi executada.",
            ),
            reply_markup=user_actions_keyboard(),
        )
        return


@router.callback_query(F.data == "tigrao:home")
async def tigrao_home_callback(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, home_text(), home_keyboard())


@router.callback_query(F.data == "tigrao:groups")
async def tigrao_groups(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "escolher grupo",
            "Selecione um grupo já conhecido ou use a opção para digitar o chat_id.",
        ),
        groups_keyboard(list_groups()),
    )


@router.callback_query(F.data == "tigrao:group:manual")
async def tigrao_group_manual(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    set_action("select_group", waiting_for="chat_id")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — escolher grupo\n\n"
            "Envie agora o chat_id numérico do grupo.\n"
            "Pode ser com ou sem hífen.\n\n"
            "Exemplo:\n"
            "-1001234567890"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:group:"))
async def tigrao_group_select(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.data == "tigrao:group:manual":
        return
    try:
        chat_id = parse_chat_id(callback.data.rsplit(":", 1)[-1])
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    set_selected_group(chat_id, str(chat_id))
    if callback.message:
        await callback.message.edit_text(
            success_text("Grupo selecionado", f"Grupo: {chat_id}"),
            reply_markup=home_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:user_actions")
async def tigrao_user_actions(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text("ações de usuário", "Ações que exigem grupo selecionado e, em geral, apenas o user_id do alvo."),
        user_actions_keyboard(),
    )


@router.callback_query(F.data.startswith("tigrao:action:"))
async def tigrao_prepare_user_action(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    action = (callback.data or "").rsplit(":", 1)[-1]
    if action not in ACTION_LABELS:
        await callback.answer("Ação inválida.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action(action, waiting_for="user_id")
    if callback.message:
        await callback.message.edit_text(
            f"Tigrão — {ACTION_LABELS[action]}\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora apenas o user_id do alvo.\n"
            "Nesta etapa a ação ainda não será executada."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:links")
async def tigrao_links(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("links", "Geração de links de entrada para o grupo selecionado."), links_keyboard())


@router.callback_query(F.data == "tigrao:messages")
async def tigrao_messages(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("mensagens", "Envio, fixação ou remoção de mensagens usando apenas o privado do dono."), messages_keyboard())


@router.callback_query(F.data == "tigrao:ddx")
async def tigrao_ddx(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("filtros DDX", "Configuração futura dos filtros de remoção automática por texto."), ddx_keyboard())


@router.callback_query(F.data == "tigrao:logs")
async def tigrao_logs(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("logs", "Consulta futura dos registros internos de ações executadas pelo painel."), logs_keyboard())


@router.callback_query(F.data == "tigrao:close")
async def tigrao_close(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão — painel fechado.")
    await callback.answer()
