from __future__ import annotations

import asyncio
import re

from aiogram import Router, types
from aiogram.filters import Command

router = Router()

OWNER_ID = 8505890439
KICK_RESET_DELAY_SECONDS = 1
INVITE_LINK_NAME = "vvv-reset-link"


def _parse_chat_id(value: str) -> int:
    raw = str(value).strip().replace(" ", "")
    if not re.fullmatch(r"-?\d+", raw):
        raise ValueError("chat_id deve ser numérico")
    chat_id = int(raw)
    if chat_id > 0:
        chat_id = -chat_id
    return chat_id


def _parse_user_id(value: str) -> int:
    raw = str(value).strip().replace(" ", "")
    if not re.fullmatch(r"\d+", raw):
        raise ValueError("user_id deve ser numérico")
    return int(raw)


def parse_payload(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    try:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        chat_id = _parse_chat_id(lines[1])
        user_id = _parse_user_id(lines[2])
        return chat_id, user_id
    except Exception:
        return None, None


async def kick_reset_user(bot, chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    await asyncio.sleep(KICK_RESET_DELAY_SECONDS)
    await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)


async def create_direct_invite_link(bot, chat_id: int) -> str:
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
        name=INVITE_LINK_NAME,
        creates_join_request=False,
    )
    return invite.invite_link


@router.message(Command("vvv"))
async def vvv(message: types.Message) -> None:
    if (
        message.chat.type != "private"
        or not message.from_user
        or message.from_user.id != OWNER_ID
    ):
        return

    chat_id, user_id = parse_payload(message.text)
    if not chat_id or not user_id:
        await message.answer(
            "Não deu certo.\n\n"
            "Erro: formato inválido.\n"
            "Use chat_id numérico com ou sem hífen e user_id numérico sem hífen.\n"
            "Use:\n"
            "/vvv\n"
            "<chat_id>\n"
            "<user_id>"
        )
        return

    try:
        await kick_reset_user(message.bot, chat_id, user_id)
    except Exception as exc:
        await message.answer(
            "Não deu certo.\n\n"
            "Erro no reset de entrada: não consegui remover e liberar o usuário.\n"
            f"Detalhe técnico: {type(exc).__name__}: {exc}"
        )
        return

    try:
        invite_link = await create_direct_invite_link(message.bot, chat_id)
    except Exception as exc:
        await message.answer(
            "Parcialmente bem-sucedido.\n\n"
            "O usuário foi removido e liberado para voltar, mas eu não consegui criar o link direto de entrada.\n"
            f"Detalhe técnico: {type(exc).__name__}: {exc}"
        )
        return

    await message.answer(
        "Bem-sucedido.\n\n"
        "O usuário foi removido, liberado novamente e agora pode entrar pelo link direto abaixo.\n\n"
        f"Grupo: {chat_id}\n"
        f"Usuário: {user_id}\n"
        f"Link direto: {invite_link}"
    )
