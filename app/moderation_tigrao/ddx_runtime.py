from __future__ import annotations

import html
import json
import logging
import re
import unicodedata

from aiogram.exceptions import TelegramForbiddenError

from app.config.settings import OWNER_ID
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


def _matching_words(text_value: str, words: list[str]) -> list[str]:
    spaced_text = _normalize_spaced(text_value)
    compact_text = _normalize_compact(text_value)
    matches: list[str] = []
    for word in words:
        original_word = str(word).strip()
        spaced_word = _normalize_spaced(original_word)
        compact_word = _normalize_compact(original_word)
        if not spaced_word or not compact_word:
            continue
        if " " in spaced_word and spaced_word in spaced_text:
            matches.append(original_word)
        elif " " not in spaced_word and (spaced_word in spaced_text or compact_word in compact_text):
            matches.append(original_word)
    return matches[:5]


def _shorten_text(value: str, limit: int = 900) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


async def _notify_owner_ddx_deleted(bot, message, text_value: str, words: list[str]) -> None:
    if not OWNER_ID:
        return
    try:
        author = message.from_user
        author_name = html.escape(author.full_name if author else "desconhecido")
        author_id = getattr(author, "id", "-")
        username = getattr(author, "username", None)
        username_line = f"\nUsername: @{html.escape(username)}" if username else ""
        group_title = html.escape(message.chat.title or str(message.chat.id))
        matched = _matching_words(text_value, words)
        matched_text = ", ".join(html.escape(word) for word in matched) if matched else "filtro DDX"
        message_text = html.escape(_shorten_text(text_value))
        notice = (
            "Tigrão — DDX apagou mensagem\n\n"
            f"Grupo: {group_title} ({message.chat.id})\n"
            f"Autor: {author_name} — <code>{author_id}</code>{username_line}\n"
            f"Mensagem ID: <code>{message.message_id}</code>\n"
            f"Filtro: {matched_text}\n\n"
            f"Mensagem apagada:\n<blockquote>{message_text}</blockquote>"
        )
        await bot.send_message(chat_id=OWNER_ID, text=notice, parse_mode="HTML")
        logger.warning(
            "TIGRAO_DDX_OWNER_NOTIFIED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            author_id,
            message.message_id,
        )
    except Exception:
        logger.exception(
            "TIGRAO_DDX_OWNER_NOTIFY_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            getattr(message.chat, "id", None),
            getattr(getattr(message, "from_user", None), "id", None),
            getattr(message, "message_id", None),
        )


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
        await _notify_owner_ddx_deleted(bot, message, text_value, words)
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
