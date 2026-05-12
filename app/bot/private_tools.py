from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config.settings import OWNER_ID

logger = logging.getLogger(__name__)
router = Router(name="private_tools")


def _is_owner_private_message(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == OWNER_ID
        and message.chat.type == "private"
    )


def _lines(message: Message) -> list[str]:
    return [line.strip() for line in (message.text or "").splitlines() if line.strip()]


def _parse_chat_id(value: str) -> int:
    return int(value.strip())


def _parse_user_id(value: str) -> int:
    return int(value.strip())


def _error_text(reason: str, fix: str) -> str:
    return f"Erro:\nMotivo: {reason}\nComo corrigir: {fix}"


def _success_text(title: str, details: str) -> str:
    return f"Sucesso.\n\n{title}\n{details}"


async def ddx_preprocess_update(bot, update) -> bool:
    return False


@router.message(Command("hidden"))
async def hidden(message: Message) -> None:
    logger.warning(
        "HIDDEN_COMMAND_RECEIVED | user_id=%s | chat_type=%s | owner_id=%s",
        getattr(message.from_user, "id", None),
        message.chat.type,
        OWNER_ID,
    )
    if not _is_owner_private_message(message):
        return
    await message.answer(
        "COMANDOS OCULTOS\n\n"
        "/dx\n"
        "/ddx\n"
        "/mx1\n"
        "/mx2\n"
        "/joinx\n"
        "/vx\n"
        "/uv\n"
        "/mx\n"
        "/xend\n"
        "/ximg\n"
        "/vvv"
    )
