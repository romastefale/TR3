from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config.settings import OWNER_ID

router = Router(name="owner_tools")


def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


def _sender_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


@router.message(Command("ownercheck"))
async def ownercheck(message: Message) -> None:
    if not _is_private(message):
        return

    user_id = _sender_id(message)
    if user_id is None:
        return

    if OWNER_ID == 0:
        await message.answer(
            "OWNER CHECK\n\n"
            f"Seu ID: {user_id}\n"
            "OWNER_ID atual: não configurado ou inválido\n\n"
            "Corrija a variável OWNER_ID no Railway para habilitar os comandos moderadores."
        )
        return

    if user_id != OWNER_ID:
        await message.answer(
            "OWNER CHECK\n\n"
            f"Seu ID: {user_id}\n"
            "Status: não autorizado para comandos moderadores."
        )
        return

    await message.answer(
        "OWNER CHECK\n\n"
        f"Seu ID: {user_id}\n"
        "Status: autorizado."
    )


@router.message(Command("hidden"))
async def hidden(message: Message) -> None:
    if not _is_private(message):
        return

    user_id = _sender_id(message)
    if user_id is None:
        return

    if OWNER_ID == 0:
        await message.answer(
            "COMANDOS OCULTOS\n\n"
            "Bloqueado: OWNER_ID não está configurado ou está inválido no Railway.\n\n"
            f"Seu ID: {user_id}\n"
            "Defina OWNER_ID com esse número para liberar os comandos moderadores."
        )
        return

    if user_id != OWNER_ID:
        await message.answer(
            "COMANDOS OCULTOS\n\n"
            "Bloqueado: este usuário não corresponde ao OWNER_ID configurado.\n\n"
            f"Seu ID: {user_id}\n"
            "Use /ownercheck para conferir."
        )
        return

    await message.answer(
        "COMANDOS OCULTOS\n\n"
        "/dx\n<link_da_mensagem>\n\n"
        "/ddx\n<chat_id>\n<add|remove|list|off|test>\n<palavras ou texto>\n\n"
        "/mx1\n<chat_id>\n\n"
        "/mx2\n<chat_id>\n\n"
        "/joinx\n<chat_id>\n<user_id>\n\n"
        "/vx\n<chat_id>\n<user_id>\n\n"
        "/uv\n<chat_id>\n<user_id>\n\n"
        "/mx\n<chat_id>\n<user_id>\n<10m|2h|3d|i|x>\n\n"
        "/xend <chat_id> — respondendo uma mensagem\n"
        "/xend pin <chat_id> — copia e fixa\n\n"
        "/ximg\n<chat_id> — respondendo uma mídia\n\n"
        "/vvv\n<chat_id>\n<user_id>"
    )
