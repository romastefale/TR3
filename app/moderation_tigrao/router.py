from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.moderation_tigrao.permissions import is_owner_private_message
from app.moderation_tigrao.texts import home_text

router = Router(name="moderation_tigrao")


@router.message(Command("tigrao"))
async def tigrao_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    await message.answer(home_text())
