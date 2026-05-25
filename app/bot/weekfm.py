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


def _caption(card_data, display_name: str, user_id: int) -> str:
    """Legenda enxuta: '♫ <período> de <user>' com mention do autor."""
    if card_data is not None and getattr(card_data, "period_value", None):
        period = card_data.period_value.strip().lower()
    else:
        period = "esta semana"
    safe_name = html.escape(display_name or "Usuário")
    return f'♫ {html.escape(period)} de <a href="tg://user?id={user_id}">{safe_name}</a>'


async def _finish_weekfm(message: Message, user_id: int, display_name: str, raw_week: str | None) -> None:
    try:
        result = await lastfm_weekly_service.build_capsule(
            user_id=user_id,
            display_name=display_name,
            raw_week=raw_week,
        )
        text = result.text
        card_bytes = await render_monthfm_card(result.card_data) if result.card_data else None
        # Sprint 11: bot reage 🏆 no card de extrato.
        from app.bot.telegram import _react_to_own_card, _CARD_EMOJI_EXTRACT
        if card_bytes:
            # Card visual gerado → não enviamos a mensagem-texto duplicada.
            # O texto segue como fallback nos branches sem card_bytes.
            await _safe_delete(message)
            sent = await message.answer_photo(
                photo=BufferedInputFile(card_bytes, filename="weekfm-card.jpg"),
                caption=_caption(result.card_data, display_name, user_id),
                parse_mode="HTML",
            )
            await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, _CARD_EMOJI_EXTRACT)
            return
        if result.photo_bytes:
            await _safe_delete(message)
            sent = await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="weekfm.jpg"),
                caption=_caption(result.card_data, display_name, user_id),
                parse_mode="HTML",
            )
            await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, _CARD_EMOJI_EXTRACT)
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


# I4: mantém ref forte das background tasks pra GC não coletar antes do término.
_BG_TASKS: set[asyncio.Task] = set()


@router.message(Command("weekfm"))
async def weekfm(message: Message) -> None:
    if not message.from_user:
        return
    from app.services.connection_check import connect_hint_for, is_user_connected
    if not is_user_connected(message.from_user.id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_week = parts[1].strip() if len(parts) > 1 else None
    # U1: chat_action enquanto Playwright renderiza o card semanal.
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass
    status = await message.answer("Gerando extrato da semana do Last.fm...")
    task = asyncio.create_task(
        _finish_weekfm(
            status,
            user_id=message.from_user.id,
            display_name=message.from_user.full_name or "Usuário",
            raw_week=raw_week,
        )
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
