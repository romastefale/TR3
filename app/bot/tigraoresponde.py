from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import ForceReply, Message, Update

logger = logging.getLogger(__name__)

TIGRAORESPONDE_TARGET_CHAT_ID = -1003839856024
TIGRAORESPONDE_PREFIX = "Mira,"
TIGRAORESPONDE_PROMPT = "Qual é a sua pergunta?"
TIGRAORESPONDE_TTL_SECONDS = 180


@dataclass
class PendingTigraoQuestion:
    user_id: int
    origin_chat_id: int
    origin_message_id: int
    prompt_chat_id: int
    prompt_message_id: int
    created_at: datetime
    expires_at: datetime
    question_text: str | None = None
    relay_message_id: int | None = None


_pending_by_prompt: dict[tuple[int, int], PendingTigraoQuestion] = {}
_pending_by_relay_message_id: dict[int, PendingTigraoQuestion] = {}


def _first_token(text_value: str | None) -> str:
    if not text_value:
        return ""
    return text_value.strip().split(maxsplit=1)[0]


def _command_name(text_value: str | None) -> str:
    token = _first_token(text_value).lower()
    return token.split("@", 1)[0]


def _is_tigraoresponde_command(text_value: str | None) -> bool:
    return _command_name(text_value) == "/tigraoresponde"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup_expired() -> None:
    current = _now()
    expired_prompt_keys = [key for key, item in _pending_by_prompt.items() if item.expires_at <= current]
    for key in expired_prompt_keys:
        _pending_by_prompt.pop(key, None)

    expired_relay_ids = [message_id for message_id, item in _pending_by_relay_message_id.items() if item.expires_at <= current]
    for message_id in expired_relay_ids:
        _pending_by_relay_message_id.pop(message_id, None)


def _find_pending_prompt(message: Message) -> PendingTigraoQuestion | None:
    if not message.reply_to_message:
        return None
    return _pending_by_prompt.get((message.chat.id, message.reply_to_message.message_id))


def _find_pending_relay(message: Message) -> PendingTigraoQuestion | None:
    if message.chat.id != TIGRAORESPONDE_TARGET_CHAT_ID or not message.reply_to_message:
        return None
    return _pending_by_relay_message_id.get(message.reply_to_message.message_id)


async def _start_tigraoresponde(message: Message) -> bool:
    if not message.from_user:
        return True

    prompt = await message.answer(
        TIGRAORESPONDE_PROMPT,
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Digite sua pergunta aqui",
        ),
    )
    pending = PendingTigraoQuestion(
        user_id=message.from_user.id,
        origin_chat_id=message.chat.id,
        origin_message_id=message.message_id,
        prompt_chat_id=prompt.chat.id,
        prompt_message_id=prompt.message_id,
        created_at=_now(),
        expires_at=_now() + timedelta(seconds=TIGRAORESPONDE_TTL_SECONDS),
    )
    _pending_by_prompt[(prompt.chat.id, prompt.message_id)] = pending
    logger.warning(
        "TIGRAORESPONDE_PROMPT_SENT | chat_id=%s | user_id=%s | prompt_message_id=%s",
        message.chat.id,
        message.from_user.id,
        prompt.message_id,
    )
    return True


async def _handle_user_question(bot: Bot, message: Message) -> bool:
    pending = _find_pending_prompt(message)
    if pending is None:
        return False
    if not message.from_user:
        return True
    if message.from_user.id != pending.user_id:
        await message.reply("Apenas quem usou /tigraoresponde pode responder essa pergunta.")
        logger.warning(
            "TIGRAORESPONDE_QUESTION_DENIED | chat_id=%s | expected_user_id=%s | from_id=%s",
            message.chat.id,
            pending.user_id,
            message.from_user.id,
        )
        return True

    question_text = (message.text or "").strip()
    if not question_text:
        await message.reply("Envie a pergunta em texto.")
        return True
    if question_text.startswith("/"):
        await message.reply("Envie a pergunta em texto, não outro comando.")
        return True

    relay = await bot.send_message(
        TIGRAORESPONDE_TARGET_CHAT_ID,
        f"{TIGRAORESPONDE_PREFIX} {question_text}",
    )
    pending.origin_chat_id = message.chat.id
    pending.origin_message_id = message.message_id
    pending.question_text = question_text
    pending.relay_message_id = relay.message_id
    pending.expires_at = _now() + timedelta(seconds=TIGRAORESPONDE_TTL_SECONDS)
    _pending_by_prompt.pop((pending.prompt_chat_id, pending.prompt_message_id), None)
    _pending_by_relay_message_id[relay.message_id] = pending

    await message.answer("Pergunta enviada. Vou retornar a resposta aqui quando ela chegar.")
    logger.warning(
        "TIGRAORESPONDE_RELAY_SENT | origin_chat_id=%s | origin_message_id=%s | user_id=%s | target_chat_id=%s | relay_message_id=%s",
        pending.origin_chat_id,
        pending.origin_message_id,
        pending.user_id,
        TIGRAORESPONDE_TARGET_CHAT_ID,
        relay.message_id,
    )
    return True


async def _handle_mira_reply(bot: Bot, message: Message) -> bool:
    pending = _find_pending_relay(message)
    if pending is None:
        return False

    answer_text = (message.text or message.caption or "").strip()
    if not answer_text:
        answer_text = "Recebi uma resposta, mas ela não veio em texto."

    await bot.send_message(
        pending.origin_chat_id,
        answer_text,
        reply_to_message_id=pending.origin_message_id,
    )
    if pending.relay_message_id is not None:
        _pending_by_relay_message_id.pop(pending.relay_message_id, None)

    logger.warning(
        "TIGRAORESPONDE_ANSWER_RETURNED | origin_chat_id=%s | origin_message_id=%s | user_id=%s | relay_message_id=%s | answer_message_id=%s",
        pending.origin_chat_id,
        pending.origin_message_id,
        pending.user_id,
        pending.relay_message_id,
        message.message_id,
    )
    return True


async def handle_tigraoresponde_update(bot: Bot, update: Update) -> bool:
    message = update.message
    if not message:
        return False

    _cleanup_expired()

    if _is_tigraoresponde_command(message.text):
        return await _start_tigraoresponde(message)

    if message.reply_to_message and message.text:
        question_handled = await _handle_user_question(bot, message)
        if question_handled:
            return True

    if message.reply_to_message and message.chat.id == TIGRAORESPONDE_TARGET_CHAT_ID:
        mira_handled = await _handle_mira_reply(bot, message)
        if mira_handled:
            return True

    return False
