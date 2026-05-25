from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramServerError
from aiogram.types import BufferedInputFile, ChatPermissions

from app.config.settings import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Sprint 7 (T04): retry transitório nas chamadas Telegram. Cobre
# TelegramRetryAfter (flood control) e TelegramServerError (5xx).
# Erros permanentes (TelegramForbiddenError, TelegramBadRequest)
# continuam propagando sem retry — o caller já trata e loga.
_RETRY_MAX_ATTEMPTS = 3  # 1 tentativa + 2 retries
_RETRY_MAX_WAIT_SECONDS = 30.0  # se Telegram pedir mais que isso, desiste
_RETRY_BASE_BACKOFF = 0.5  # backoff exponencial pra 5xx


async def _with_telegram_retry(
    factory: Callable[[], Awaitable[_T]],
    *,
    label: str = "telegram_call",
) -> _T:
    """Executa factory() com retry pra TelegramRetryAfter / TelegramServerError.

    factory DEVE retornar uma coroutine nova a cada chamada (porque coroutines
    não podem ser awaited duas vezes). Use lambda: bot.X(...).
    """
    last_exc: Exception | None = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return await factory()
        except TelegramRetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 1.0))
            if wait > _RETRY_MAX_WAIT_SECONDS or attempt >= _RETRY_MAX_ATTEMPTS - 1:
                raise
            logger.warning(
                "TIGRAO_TELEGRAM_RETRY_AFTER | label=%s | attempt=%d | wait=%.1fs",
                label, attempt + 1, wait,
            )
            await asyncio.sleep(wait + 0.1)
            last_exc = exc
        except TelegramServerError as exc:
            if attempt >= _RETRY_MAX_ATTEMPTS - 1:
                raise
            backoff = _RETRY_BASE_BACKOFF * (2 ** attempt)
            logger.warning(
                "TIGRAO_TELEGRAM_SERVER_ERROR | label=%s | attempt=%d | backoff=%.2fs | %s",
                label, attempt + 1, backoff, exc,
            )
            await asyncio.sleep(backoff)
            last_exc = exc
    # inalcançável (último attempt sempre re-raises), mas satisfaz o type checker
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"_with_telegram_retry unreachable for {label}")


def _full_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )


async def ban_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await _with_telegram_retry(
        lambda: bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=True),
        label="ban_chat_member",
    )


async def unban_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await _with_telegram_retry(
        lambda: bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True),
        label="unban_chat_member",
    )


async def mute_user(bot: Bot, chat_id: int, user_id: int, duration: timedelta | str) -> None:
    until_date = None if duration == "indefinido" else datetime.now(timezone.utc) + duration
    await _with_telegram_retry(
        lambda: bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date,
        ),
        label="restrict_chat_member_mute",
    )


async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await _with_telegram_retry(
        lambda: bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=_full_permissions(),
        ),
        label="restrict_chat_member_unmute",
    )


async def create_direct_link(bot: Bot, chat_id: int) -> str:
    invite = await _with_telegram_retry(
        lambda: bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=False,
            member_limit=1,
        ),
        label="create_chat_invite_link_direct",
    )
    return invite.invite_link


async def create_approval_link(bot: Bot, chat_id: int) -> str:
    invite = await _with_telegram_retry(
        lambda: bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
        ),
        label="create_chat_invite_link_approval",
    )
    return invite.invite_link


async def approve_join_request(bot: Bot, chat_id: int, user_id: int) -> None:
    await _with_telegram_retry(
        lambda: bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id),
        label="approve_chat_join_request",
    )


async def reset_entry(bot: Bot, chat_id: int, user_id: int) -> str:
    await _with_telegram_retry(
        lambda: bot.ban_chat_member(chat_id=chat_id, user_id=user_id),
        label="reset_entry_ban",
    )
    await _with_telegram_retry(
        lambda: bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True),
        label="reset_entry_unban",
    )
    return await create_direct_link(bot, chat_id)


async def delete_message(bot: Bot, chat_id: int | str, message_id: int) -> None:
    await _with_telegram_retry(
        lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
        label="delete_message",
    )


async def copy_message(bot: Bot, target_chat_id: int, from_chat_id: int, message_id: int, pin: bool = False) -> int:
    copied = await _with_telegram_retry(
        lambda: bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        ),
        label="copy_message",
    )
    if pin:
        await _with_telegram_retry(
            lambda: bot.pin_chat_message(
                chat_id=target_chat_id,
                message_id=copied.message_id,
                disable_notification=True,
            ),
            label="pin_chat_message_after_copy",
        )
    return copied.message_id


async def set_group_title(bot: Bot, chat_id: int, title: str) -> None:
    await _with_telegram_retry(
        lambda: bot.set_chat_title(chat_id=chat_id, title=title),
        label="set_chat_title",
    )


async def set_group_description(bot: Bot, chat_id: int, description: str) -> None:
    normalized = "" if description.strip() == "." else description
    await _with_telegram_retry(
        lambda: bot.set_chat_description(chat_id=chat_id, description=normalized),
        label="set_chat_description",
    )


async def set_group_photo(bot: Bot, chat_id: int, image_bytes: bytes, filename: str = "group_photo.jpg") -> None:
    await _with_telegram_retry(
        lambda: bot.set_chat_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(image_bytes, filename=filename),
        ),
        label="set_chat_photo",
    )


async def set_member_tag(bot: Bot, chat_id: int, user_id: int, tag: str) -> None:
    if hasattr(bot, "set_chat_member_tag"):
        await bot.set_chat_member_tag(chat_id=chat_id, user_id=user_id, tag=tag)  # type: ignore[attr-defined]
        return

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ausente")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setChatMemberTag"
    payload = {"chat_id": chat_id, "user_id": user_id, "tag": tag}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload)
    data = response.json()
    if not data.get("ok"):
        description = data.get("description") or response.text
        raise RuntimeError(f"Telegram setChatMemberTag falhou: {description}")
