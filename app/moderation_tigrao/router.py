from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.keyboards import home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.texts import home_text

router = Router(name="moderation_tigrao")


@router.message(Command("tigrao"))
async def tigrao_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    await message.answer(home_text(), reply_markup=home_keyboard())


@router.callback_query(F.data == "tigrao:close")
async def tigrao_close(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão — painel fechado.")
    await callback.answer()
