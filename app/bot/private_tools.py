from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import text

from app.config.settings import OWNER_ID
from app.db.database import engine

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


def _ensure_ddx_rules_table() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ddx_rules (
                    chat_id INTEGER PRIMARY KEY,
                    words TEXT,
                    enabled INTEGER,
                    updated_at DATETIME
                );
                """
            )
        )


def _ddx_normalize_spaced(value: str) -> str:
    value = value.lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _ddx_normalize_compact(value: str) -> str:
    value = value.lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def _ddx_get(chat_id: int) -> dict[str, object] | None:
    _ensure_ddx_rules_table()
    with engine.begin() as conn:
        row = (
            conn.execute(
                text("SELECT words, enabled FROM ddx_rules WHERE chat_id = :chat_id"),
                {"chat_id": chat_id},
            )
            .mappings()
            .first()
        )
    if not row:
        return None
    try:
        words = json.loads(str(row["words"] or "[]"))
        if not isinstance(words, list):
            words = []
    except Exception:
        logger.exception("DDX_LOAD_FAILED | chat_id=%s", chat_id)
        words = []
    return {"words": words, "enabled": bool(row["enabled"])}


def _ddx_match(text_value: str, words: list[str]) -> bool:
    spaced_text = _ddx_normalize_spaced(text_value)
    compact_text = _ddx_normalize_compact(text_value)
    for word in words:
        spaced_word = _ddx_normalize_spaced(str(word))
        compact_word = _ddx_normalize_compact(str(word))
        if not spaced_word or not compact_word:
            continue
        if " " in spaced_word:
            if spaced_word in spaced_text:
                return True
        else:
            if spaced_word in spaced_text or compact_word in compact_text:
                return True
    return False


def _extract_message_links(text_value: str | None) -> list[str]:
    if not text_value:
        return []

    without_command = re.sub(
        r"^\s*/dx(?:@\w+)?\b",
        "",
        text_value.strip(),
        count=1,
        flags=re.IGNORECASE,
    )

    links = re.findall(r"(?:https?://)?(?:www\.)?t\.me/[^\s<>]+", without_command)
    cleaned: list[str] = []
    seen: set[str] = set()

    for link in links:
        item = link.strip().strip("<>()[]{}\"'").rstrip(".,;:")
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)

    return cleaned


def _parse_message_link(link: str) -> tuple[int | str, int]:
    cleaned = link.strip().strip("<>()[]{}\"'").rstrip(".,;:")
    if cleaned.startswith("t.me/") or cleaned.startswith("www.t.me/"):
        cleaned = "https://" + cleaned

    match = re.match(
        r"https?://(?:www\.)?t\.me/([^/?#]+)/([^?#]+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("link inválido")

    chat_part = match.group(1)
    path = match.group(2).strip("/")
    parts = [part for part in path.split("/") if part]

    if chat_part.lower() == "c":
        if len(parts) < 2 or not parts[0].isdigit():
            raise ValueError("link privado inválido")

        message_candidates = [int(part) for part in parts[1:] if part.isdigit()]
        if not message_candidates:
            raise ValueError("message_id não encontrado")

        chat_id: int | str = int("-100" + parts[0])
        message_id = message_candidates[-1]
        return chat_id, message_id

    message_candidates = [int(part) for part in parts if part.isdigit()]
    if not message_candidates:
        raise ValueError("message_id não encontrado")

    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", chat_part):
        raise ValueError("username de chat inválido")

    return f"@{chat_part}", message_candidates[-1]


async def _delete_linked_message(bot, link: str) -> tuple[bool, str]:
    chat_id, message_id = _parse_message_link(link)
    await bot.delete_message(chat_id=chat_id, message_id=message_id)
    return True, f"{chat_id}/{message_id}"


async def ddx_preprocess_update(bot, update) -> bool:
    message = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if not message:
        return False
    if message.chat.type not in {"group", "supergroup"}:
        return False
    text_value = message.text or message.caption
    if not text_value:
        return False
    if not message.from_user or message.from_user.is_bot:
        return False
    payload = _ddx_get(message.chat.id)
    if not payload or not payload.get("enabled"):
        return False
    words = payload.get("words", [])
    if not isinstance(words, list) or not words:
        return False
    if not _ddx_match(text_value, words):
        return False

    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in {"administrator", "creator"}:
            logger.warning(
                "DDX_SKIP_ADMIN | chat_id=%s | user_id=%s | message_id=%s",
                message.chat.id,
                message.from_user.id,
                message.message_id,
            )
            return False
    except Exception:
        logger.exception(
            "DDX_ADMIN_CHECK_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            getattr(message.from_user, "id", None),
            message.message_id,
        )
        return False

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        logger.warning(
            "DDX_DELETED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            message.from_user.id,
            message.message_id,
        )
        return True
    except Exception:
        logger.exception(
            "DDX_DELETE_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.id,
            getattr(message.from_user, "id", None),
            message.message_id,
        )
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


@router.message(Command("dx"))
async def dx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return

    links = _extract_message_links(message.text)
    if not links:
        await message.answer(
            "MODERAÇÃO — APAGAR POR LINK\n\n"
            "Use:\n"
            "/dx\n"
            "<link_da_mensagem>\n"
            "[outros links opcionais]\n\n"
            "Exemplos aceitos:\n"
            "https://t.me/c/1234567890/55\n"
            "https://t.me/username_do_grupo/55"
        )
        return

    deleted: list[str] = []
    failed: list[str] = []

    for link in links:
        try:
            _, target = await _delete_linked_message(message.bot, link)
            deleted.append(f"{link} -> {target}")
        except TelegramForbiddenError:
            failed.append(f"{link} -> sem permissão para apagar")
        except Exception as exc:
            logger.exception("DX_DELETE_FAILED | link=%s", link)
            failed.append(f"{link} -> {type(exc).__name__}")

    response = [
        "DX FINALIZADO",
        "",
        f"Apagadas: {len(deleted)}",
        f"Falhas: {len(failed)}",
    ]

    if deleted:
        response.extend(["", "APAGADAS:"])
        response.extend(f"- {item}" for item in deleted[:15])
        if len(deleted) > 15:
            response.append(f"- ... mais {len(deleted) - 15}")

    if failed:
        response.extend(["", "FALHAS:"])
        response.extend(f"- {item}" for item in failed[:15])
        if len(failed) > 15:
            response.append(f"- ... mais {len(failed) - 15}")

    await message.answer("\n".join(response))
