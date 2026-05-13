from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.lastfm_capsule import lastfm_capsule_service

router = Router(name="monthfm")


@router.message(Command("monthfm"))
async def monthfm(message: Message) -> None:
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_month = parts[1].strip() if len(parts) > 1 else None
    text = await lastfm_capsule_service.build_capsule_text(
        user_id=message.from_user.id,
        display_name=message.from_user.full_name or "Usuário",
        raw_month=raw_month,
    )
    await message.answer(text, parse_mode="HTML")
