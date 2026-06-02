from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from app.config.settings import OWNER_ID

ALLOWED_MODERATION_USER_IDS = frozenset({OWNER_ID, 7946870636})


def is_owner_user(user_id: int | None) -> bool:
    return user_id in ALLOWED_MODERATION_USER_IDS


def is_owner_private_message(message: Message) -> bool:
    return bool(
        message.chat.type == "private"
        and message.from_user
        and is_owner_user(message.from_user.id)
    )


def is_owner_callback(callback: CallbackQuery) -> bool:
    return bool(callback.from_user and is_owner_user(callback.from_user.id))
