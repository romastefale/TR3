from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.keyboards import (
    ddx_keyboard,
    home_keyboard,
    links_keyboard,
    logs_keyboard,
    messages_keyboard,
    user_actions_keyboard,
)
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.texts import home_text

router = Router(name="moderation_tigrao")


def _section_text(title: str, detail: str) -> str:
    return f"Tigrão — {title}\n\n{detail}\n\nEscolha uma opção pelos botões abaixo."


async def _edit_private_panel(callback: CallbackQuery, text: str, reply_markup) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()


@router.message(Command("tigrao"))
async def tigrao_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    await message.answer(home_text(), reply_markup=home_keyboard())


@router.callback_query(F.data == "tigrao:home")
async def tigrao_home_callback(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, home_text(), home_keyboard())


@router.callback_query(F.data == "tigrao:user_actions")
async def tigrao_user_actions(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "ações de usuário",
            "Ações que exigem grupo selecionado e, em geral, apenas o user_id do alvo.",
        ),
        user_actions_keyboard(),
    )


@router.callback_query(F.data == "tigrao:links")
async def tigrao_links(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "links",
            "Geração de links de entrada para o grupo selecionado.",
        ),
        links_keyboard(),
    )


@router.callback_query(F.data == "tigrao:messages")
async def tigrao_messages(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "mensagens",
            "Envio, fixação ou remoção de mensagens usando apenas o privado do dono.",
        ),
        messages_keyboard(),
    )


@router.callback_query(F.data == "tigrao:ddx")
async def tigrao_ddx(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "filtros DDX",
            "Configuração futura dos filtros de remoção automática por texto.",
        ),
        ddx_keyboard(),
    )


@router.callback_query(F.data == "tigrao:logs")
async def tigrao_logs(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "logs",
            "Consulta futura dos registros internos de ações executadas pelo painel.",
        ),
        logs_keyboard(),
    )


@router.callback_query(F.data == "tigrao:close")
async def tigrao_close(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão — painel fechado.")
    await callback.answer()
