from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.services.lastfm_capsule import lastfm_capsule_service

logger = logging.getLogger(__name__)
router = Router(name="monthfm")


async def _finish_monthfm(message: Message, user_id: int, display_name: str, raw_month: str | None) -> None:
    try:
        result = await lastfm_capsule_service.build_capsule(
            user_id=user_id,
            display_name=display_name,
            raw_month=raw_month,
        )
        text = result.text
        if result.photo_bytes and len(text) <= 1024:
            await message.delete()
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption=text,
                parse_mode="HTML",
            )
            return
        if result.photo_bytes:
            await message.delete()
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption="♫ <b>Sound Capsule</b>",
                parse_mode="HTML",
            )
            await message.answer(text, parse_mode="HTML")
            return
        if len(text) <= 3900:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.edit_text(text[:3900], parse_mode="HTML")
            await message.answer(text[3900:], parse_mode="HTML")
    except Exception:
        logger.exception("monthfm generation failed | user_id=%s | raw_month=%s", user_id, raw_month)
        try:
            await message.edit_text("Não consegui gerar a cápsula mensal agora. Tente novamente em alguns instantes.")
        except Exception:
            logger.exception("monthfm failure message failed | user_id=%s", user_id)


@router.message(Command("monthfm"))
async def monthfm(message: Message) -> None:
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_month = parts[1].strip() if len(parts) > 1 else None
    status = await message.answer("Gerando cápsula mensal do Last.fm...")
    asyncio.create_task(
        _finish_monthfm(
            status,
            user_id=message.from_user.id,
            display_name=message.from_user.full_name or "Usuário",
            raw_month=raw_month,
        )
    )
