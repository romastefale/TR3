from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.services.lastfm_capsule import lastfm_capsule_service
from app.services.monthfm_card import render_monthfm_card

logger = logging.getLogger(__name__)
router = Router(name="monthfm")


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.warning("monthfm status delete failed | message_id=%s", message.message_id, exc_info=True)


def _format_caption(card_data, raw_month: str | None) -> str:
    """Legenda enxuta: '♫ <período> de 𝐩𝐢𝐝𝐫𝐨'.

    Prioriza `period_value` do card (já resolvido em PT-BR, ex: 'FEVEREIRO 2026'),
    cai para o input do usuário e por fim para um rótulo genérico.
    """
    if card_data is not None and getattr(card_data, "period_value", None):
        period = card_data.period_value.strip().lower()
    elif raw_month:
        period = raw_month.strip().lower()
    else:
        period = "este mês"
    return f'♫ {html.escape(period)} de 𝐩𝐢𝐝𝐫𝐨'


async def _finish_monthfm(message: Message, user_id: int, display_name: str, raw_month: str | None) -> None:
    try:
        result = await lastfm_capsule_service.build_capsule(
            user_id=user_id,
            display_name=display_name,
            raw_month=raw_month,
        )
        text = result.text
        card_bytes = await render_monthfm_card(result.card_data) if result.card_data else None
        if card_bytes:
            # Quando o card visual é gerado, dispensamos a mensagem-texto
            # com o mesmo conteúdo (evita duplicação na thread). O texto
            # continua disponível como fallback nos branches seguintes.
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(card_bytes, filename="monthfm-card.jpg"),
                caption=_format_caption(result.card_data, raw_month),
                parse_mode="HTML",
            )
            return
        if result.photo_bytes and len(text) <= 1024:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption=text,
                parse_mode="HTML",
            )
            return
        if result.photo_bytes:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption="♫ Extrato mensal",
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
    status = await message.answer("Gerando extrato mensal do Last.fm...")
    asyncio.create_task(
        _finish_monthfm(
            status,
            user_id=message.from_user.id,
            display_name=message.from_user.full_name or "Usuário",
            raw_month=raw_month,
        )
    )
