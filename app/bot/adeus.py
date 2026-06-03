from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.moderation_tigrao.permissions import is_owner_user
from app.moderation_tigrao.storage import list_groups, log_action

logger = logging.getLogger(__name__)

router = Router(name="adeus")


@router.message(Command("adeus"))
async def adeus(message: Message) -> None:
    """Owner-only: faz o bot sair de todos os grupos conhecidos."""
    if not message.from_user or not is_owner_user(message.from_user.id):
        return
    if message.chat.type != "private":
        await message.answer("Use /adeus no privado do bot.")
        return

    groups = list_groups(limit=10000)
    if not groups:
        await message.answer("/adeus: nenhum grupo conhecido encontrado.")
        return

    total = len(groups)
    left = 0
    failed = 0
    already_out = 0

    await message.answer(f"/adeus: saindo de {total} grupo(s) conhecido(s).")

    for group in groups:
        chat_id = int(group["chat_id"])
        title = str(group.get("title") or chat_id)
        try:
            await message.bot.leave_chat(chat_id)
            left += 1
            log_action(chat_id=chat_id, action="adeus_leave_chat", status="success")
            logger.warning("ADEUS_LEFT_GROUP | chat_id=%s | title=%s", chat_id, title)
        except TelegramForbiddenError as exc:
            already_out += 1
            log_action(
                chat_id=chat_id,
                action="adeus_leave_chat",
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            logger.warning("ADEUS_ALREADY_OUT_OR_FORBIDDEN | chat_id=%s | title=%s", chat_id, title)
        except TelegramBadRequest as exc:
            failed += 1
            log_action(
                chat_id=chat_id,
                action="adeus_leave_chat",
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            logger.warning("ADEUS_BAD_REQUEST | chat_id=%s | title=%s | error=%s", chat_id, title, exc)
        except Exception as exc:
            failed += 1
            log_action(
                chat_id=chat_id,
                action="adeus_leave_chat",
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            logger.exception("ADEUS_FAILED | chat_id=%s | title=%s", chat_id, title)

    await message.answer(
        "/adeus concluído.\n"
        f"Grupos conhecidos: {total}\n"
        f"Saiu: {left}\n"
        f"Já estava fora/sem acesso: {already_out}\n"
        f"Falhas: {failed}"
    )
