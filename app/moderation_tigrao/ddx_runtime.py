from __future__ import annotations

import json
import logging
import re
import unicodedata

from aiogram.exceptions import TelegramForbiddenError

from app.moderation_tigrao.storage import get_ddx_filters, log_action

logger = logging.getLogger(__name__)


def _normalize_spaced(value: str) -> str:
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_compact(value: str) -> str:
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", value)


def _load_words(raw_words: object) -> list[str]:
    try:
        words = json.loads(str(raw_words or "[]"))
    except Exception:
        return []
    if not isinstance(words, list):
        return []
    return [str(word) for word in words if str(word).strip()]


def _matches(text_value: str, words: list[str]) -> bool:
    spaced_text = _normalize_spaced(text_value)
    compact_text = _normalize_compact(text_value)
    for word in words:
        spaced_word = _normalize_spaced(word)
        compact_word = _normalize_compact(word)
        if not spaced_word or not compact_word:
            continue
        if " " in spaced_word and spaced_word in spaced_text:
            return True
        if " " not in spaced_word and (spaced_word in spaced_text or compact_word in compact_text):
            return True
    return False


async def tigrao_ddx_preprocess_update(bot, update) -> bool:
    message = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if not message or message.chat.type not in {"group", "supergroup"}:
        return False

    text_value = message.text or message.caption
    if not text_value or not message.from_user or message.from_user.is_bot:
        return False

    row = get_ddx_filters(int(message.chat.id))
    if not row or not row.get("enabled"):
        return False

    words = _load_words(row.get("words"))
    if not words or not _matches(text_value, words):
        return False

    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in {"administrator", "creator"}:
            logger.warning(
                "TIGRAO_DDX_SKIP_ADMIN | chat_id=%s | user_id=%s | message_id=%s",
                message.chat.id,
                message.from_user.id,
                message.message_id,
            )
            return False
    except Exception:
        logger.exception(
            "TIGRAO_DDX_ADMIN_CHECK_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            getattr(message.from_user, "id", None),
            message.message_id,
        )
        return False

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        log_action(
            chat_id=int(message.chat.id),
            action="ddx_auto_delete",
            target_user_id=int(message.from_user.id),
            status="success",
        )
        logger.warning(
            "TIGRAO_DDX_DELETED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            message.from_user.id,
            message.message_id,
        )
        return True
    except TelegramForbiddenError as exc:
        log_action(
            chat_id=int(message.chat.id),
            action="ddx_auto_delete",
            target_user_id=int(message.from_user.id),
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.warning(
            "TIGRAO_DDX_FORBIDDEN | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            message.from_user.id,
            message.message_id,
        )
        return False
    except Exception as exc:
        log_action(
            chat_id=int(message.chat.id),
            action="ddx_auto_delete",
            target_user_id=int(message.from_user.id),
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.exception(
            "TIGRAO_DDX_DELETE_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            getattr(message.from_user, "id", None),
            message.message_id,
        )
        return False
