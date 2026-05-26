"""DDX Soft — lei dos 10 minutos.

Espelho funcional do `ddx_runtime.py`, mas:
- usa tabela SEPARADA `tigrao_ddx_soft_filters` (palavras nunca colidem
  com o DDX hard por decisão do owner)
- não deleta imediato; agenda `delete_message` pra +600s via asyncio.Task
- silencioso (não notifica owner por DM — só registra em tigrao_logs)
- exempt OWNER_ID (política: nenhuma ação de moderação atinge owner)
- in-memory scheduler: tasks pendentes morrem se o bot reinicia
  (aceito — palavras soft = "ruído tolerável temporário")

Helpers de normalização DUPLICADOS do ddx_runtime.py de propósito — zero
acoplamento garante que mudanças no soft não prejudicam o hard.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config.settings import OWNER_ID
from app.moderation_tigrao.storage import get_ddx_soft_filters, log_action

logger = logging.getLogger(__name__)

DDX_SOFT_DELAY_SECONDS = 10 * 60

_scheduled: set[tuple[int, int]] = set()
_SCHEDULED_BOUND = 1000


def _normalize_spaced(value: str) -> str:
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_compact(value: str) -> str:
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", value)


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
        if " " not in spaced_word and (
            spaced_word in spaced_text or compact_word in compact_text
        ):
            return True
    return False


async def _delete_after_delay(
    bot, chat_id: int, message_id: int, user_id: int, delay: float
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _scheduled.discard((chat_id, message_id))
        raise
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        log_action(
            chat_id=chat_id,
            action="ddx_soft_delete",
            target_user_id=user_id,
            status="success",
        )
        logger.warning(
            "TIGRAO_DDX_SOFT_DELETED | chat_id=%s | user_id=%s | message_id=%s",
            chat_id,
            user_id,
            message_id,
        )
    except TelegramBadRequest as exc:
        # Só tratar como noop quando a msg realmente sumiu (apagada por
        # admin, DDX hard, autor). Outros BadRequest (ex.: permissão
        # estranha, msg muito antiga) viram error pra não mascarar bug.
        msg_lower = str(exc).lower()
        is_already_gone = (
            "message to delete not found" in msg_lower
            or "message_id_invalid" in msg_lower
            or "message can't be deleted" in msg_lower
        )
        log_action(
            chat_id=chat_id,
            action="ddx_soft_delete",
            target_user_id=user_id,
            status="noop" if is_already_gone else "error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        if is_already_gone:
            logger.info(
                "TIGRAO_DDX_SOFT_ALREADY_GONE | chat_id=%s | user_id=%s | message_id=%s | reason=%s",
                chat_id,
                user_id,
                message_id,
                exc,
            )
        else:
            logger.warning(
                "TIGRAO_DDX_SOFT_BAD_REQUEST | chat_id=%s | user_id=%s | message_id=%s | reason=%s",
                chat_id,
                user_id,
                message_id,
                exc,
            )
    except TelegramForbiddenError as exc:
        log_action(
            chat_id=chat_id,
            action="ddx_soft_delete",
            target_user_id=user_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.warning(
            "TIGRAO_DDX_SOFT_FORBIDDEN | chat_id=%s | user_id=%s | message_id=%s",
            chat_id,
            user_id,
            message_id,
        )
    except Exception as exc:
        log_action(
            chat_id=chat_id,
            action="ddx_soft_delete",
            target_user_id=user_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.exception(
            "TIGRAO_DDX_SOFT_DELETE_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            chat_id,
            user_id,
            message_id,
        )
    finally:
        _scheduled.discard((chat_id, message_id))


async def tigrao_ddx_soft_preprocess_update(bot, update) -> bool:
    """Roda DEPOIS do DDX hard. NUNCA retorna True (não consome o update —
    mensagem fica visível durante os 600s antes do delete agendado)."""
    message = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if not message or message.chat.type not in {"group", "supergroup"}:
        return False

    text_value = message.text or message.caption
    if not text_value or not message.from_user:
        return False

    # Hard-block owner: política do projeto.
    if OWNER_ID and message.from_user.id == OWNER_ID:
        return False

    row = get_ddx_soft_filters(int(message.chat.id))
    if not row or not row.get("enabled"):
        return False

    try:
        import json
        words = json.loads(str(row.get("words") or "[]"))
        if not isinstance(words, list):
            return False
        words = [str(w) for w in words if str(w).strip()]
    except Exception:
        return False

    if not words or not _matches(text_value, words):
        return False

    key = (int(message.chat.id), int(message.message_id))
    if key in _scheduled:
        return False
    if len(_scheduled) >= _SCHEDULED_BOUND:
        logger.warning(
            "TIGRAO_DDX_SOFT_BOUND_HIT | scheduled=%s | skipping chat=%s msg=%s",
            len(_scheduled),
            message.chat.id,
            message.message_id,
        )
        return False

    _scheduled.add(key)
    try:
        log_action(
            chat_id=int(message.chat.id),
            action="ddx_soft_scheduled",
            target_user_id=int(message.from_user.id),
            status="success",
        )
        logger.info(
            "TIGRAO_DDX_SOFT_SCHEDULED | chat_id=%s | user_id=%s | message_id=%s | delay=%ss",
            message.chat.id,
            message.from_user.id,
            message.message_id,
            DDX_SOFT_DELAY_SECONDS,
        )
        asyncio.create_task(
            _delete_after_delay(
                bot,
                int(message.chat.id),
                int(message.message_id),
                int(message.from_user.id),
                DDX_SOFT_DELAY_SECONDS,
            )
        )
    except Exception:
        _scheduled.discard(key)
        logger.exception(
            "TIGRAO_DDX_SOFT_SCHEDULE_FAILED | chat_id=%s | message_id=%s",
            message.chat.id,
            message.message_id,
        )
    return False
