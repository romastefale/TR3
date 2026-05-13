from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile, ChatPermissions

from app.config.settings import TELEGRAM_BOT_TOKEN


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
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=True)


async def unban_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)


async def mute_user(bot: Bot, chat_id: int, user_id: int, duration: timedelta | str) -> None:
    until_date = None if duration == "indefinido" else datetime.now(timezone.utc) + duration
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until_date,
    )


async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=_full_permissions(),
    )


async def create_direct_link(bot: Bot, chat_id: int) -> str:
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
        creates_join_request=False,
        member_limit=1,
    )
    return invite.invite_link


async def create_approval_link(bot: Bot, chat_id: int) -> str:
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
        creates_join_request=True,
    )
    return invite.invite_link


async def approve_join_request(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)


async def reset_entry(bot: Bot, chat_id: int, user_id: int) -> str:
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    return await create_direct_link(bot, chat_id)


async def delete_message(bot: Bot, chat_id: int | str, message_id: int) -> None:
    await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def copy_message(bot: Bot, target_chat_id: int, from_chat_id: int, message_id: int, pin: bool = False) -> int:
    copied = await bot.copy_message(
        chat_id=target_chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )
    if pin:
        await bot.pin_chat_message(
            chat_id=target_chat_id,
            message_id=copied.message_id,
            disable_notification=True,
        )
    return copied.message_id


async def set_group_title(bot: Bot, chat_id: int, title: str) -> None:
    await bot.set_chat_title(chat_id=chat_id, title=title)


async def set_group_description(bot: Bot, chat_id: int, description: str) -> None:
    normalized = "" if description.strip() == "." else description
    await bot.set_chat_description(chat_id=chat_id, description=normalized)


async def set_group_photo(bot: Bot, chat_id: int, image_bytes: bytes, filename: str = "group_photo.jpg") -> None:
    await bot.set_chat_photo(
        chat_id=chat_id,
        photo=BufferedInputFile(image_bytes, filename=filename),
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
