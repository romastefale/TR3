from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.services.lastfm_weekly import lastfm_weekly_service
from app.services.monthfm_card import render_monthfm_card

logger = logging.getLogger(__name__)
router = Router(name="weekfm")


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.warning("weekfm status delete failed | message_id=%s", message.message_id, exc_info=True)


def _caption(display_name: str, user_id: int) -> str:
    safe_name = html.escape(display_name or "Usuário")
    return f'♫ Extrato da semana de <a href="tg://user?id={user_id}">{safe_name}</a>'


async def _finish_weekfm(message: Message, user_id: int, display_name: str, raw_week: str | None) -> None:
    try:
        result = await lastfm_weekly_service.build_capsule(
            user_id=user_id,
            display_name=display_name,
            raw_week=raw_week,
        )
        text = result.text
        card_bytes = await render_monthfm_card(result.card_data) if result.card_data else None
        if card_bytes:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(card_bytes, filename="weekfm-card.jpg"),
                caption=_caption(display_name, user_id),
                parse_mode="HTML",
            )
            await message.answer(text, parse_mode="HTML")
            return
        if result.photo_bytes:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="weekfm.jpg"),
                caption=_caption(display_name, user_id),
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
        logger.exception("weekfm generation failed | user_id=%s | raw_week=%s", user_id, raw_week)
        try:
            await message.edit_text("Não consegui gerar o extrato da semana agora. Tente novamente em alguns instantes.")
        except Exception:
            logger.exception("weekfm failure message failed | user_id=%s", user_id)


@router.message(Command("weekfm"))
async def weekfm(message: Message) -> None:
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_week = parts[1].strip() if len(parts) > 1 else None
    status = await message.answer("Gerando extrato da semana do Last.fm...")
    asyncio.create_task(
        _finish_weekfm(
            status,
            user_id=message.from_user.id,
            display_name=message.from_user.full_name or "Usuário",
            raw_week=raw_week,
        )
    )
