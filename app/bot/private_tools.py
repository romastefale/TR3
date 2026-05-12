from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
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
