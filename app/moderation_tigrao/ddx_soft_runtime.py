"""DDX Soft — lei dos 10 minutos.

Espelho funcional do `ddx_runtime.py`, mas:
- usa tabela SEPARADA `tigrao_ddx_soft_filters` (palavras nunca colidem
  com o DDX hard por decisão do owner)
- não deleta imediato; agenda `delete_message` pra +600s via asyncio.Task
- notifica owner por DM APÓS o delete bem-sucedido (silencioso pro
  grupo — ninguém vê — mas o owner recebe confirmação como no hard)
- exempt OWNER_ID (política: nenhuma ação de moderação atinge owner)
- in-memory scheduler: tasks pendentes morrem se o bot reinicia
  (aceito — palavras soft = "ruído tolerável temporário")

Helpers de normalização DUPLICADOS do ddx_runtime.py de propósito — zero
acoplamento garante que mudanças no soft não prejudicam o hard.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config.settings import OWNER_ID
from app.moderation_tigrao.storage import get_ddx_soft_filters, log_action

logger = logging.getLogger(__name__)

DDX_SOFT_DELAY_SECONDS = 10 * 60

_scheduled: set[tuple[int, int]] = set()
_SCHEDULED_BOUND = 1000


@dataclass(frozen=True)
class _Snapshot:
    """Estado capturado no momento do schedule. Necessário porque ao
    deletar 10min depois a mensagem já sumiu — sem snapshot, a DM ao
    owner não teria texto/autor/grupo pra mostrar."""
    chat_id: int
    message_id: int
    chat_title: str
    user_id: int
    user_full_name: str
    user_username: str | None
    text_value: str
    matched_words: list[str]


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
        elif " " not in spaced_word and (
            spaced_word in spaced_text or compact_word in compact_text
        ):
            matches.append(original_word)
    return matches[:5]


def _shorten_text(value: str, limit: int = 900) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


async def _notify_owner_ddx_soft_scheduled(bot, snap: _Snapshot) -> None:
    """DM ao owner NO MOMENTO da detecção — avisa que apagamento foi
    agendado pra +10min. Espelho do deleted, com header "agendou" e
    horizonte temporal pra você saber quando esperar a 2ª DM."""
    if not OWNER_ID:
        return
    try:
        author_name = html.escape(snap.user_full_name or "desconhecido")
        username_line = (
            f"\nUsername: @{html.escape(snap.user_username)}"
            if snap.user_username
            else ""
        )
        group_title = html.escape(snap.chat_title or str(snap.chat_id))
        matched_text = (
            ", ".join(html.escape(word) for word in snap.matched_words)
            if snap.matched_words
            else "filtro DDX 10min"
        )
        message_text = html.escape(_shorten_text(snap.text_value))
        notice = (
            "Tigrão — DDX 10min agendou apagamento\n\n"
            f"Grupo: {group_title} ({snap.chat_id})\n"
            f"Autor: {author_name} — <code>{snap.user_id}</code>{username_line}\n"
            f"Mensagem ID: <code>{snap.message_id}</code>\n"
            f"Filtro: {matched_text}\n"
            f"Apaga em: 10 minutos\n\n"
            f"Mensagem detectada:\n<blockquote>{message_text}</blockquote>"
        )
        await bot.send_message(chat_id=OWNER_ID, text=notice, parse_mode="HTML")
        logger.warning(
            "TIGRAO_DDX_SOFT_OWNER_SCHEDULED_NOTIFIED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )
    except Exception:
        logger.exception(
            "TIGRAO_DDX_SOFT_OWNER_SCHEDULED_NOTIFY_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )


async def _notify_owner_ddx_soft_deleted(bot, snap: _Snapshot) -> None:
    """DM ao owner APÓS delete bem-sucedido. Espelho do
    _notify_owner_ddx_deleted do hard, com header indicando "10min"
    pra você distinguir das DMs do hard."""
    if not OWNER_ID:
        return
    try:
        author_name = html.escape(snap.user_full_name or "desconhecido")
        username_line = (
            f"\nUsername: @{html.escape(snap.user_username)}"
            if snap.user_username
            else ""
        )
        group_title = html.escape(snap.chat_title or str(snap.chat_id))
        matched_text = (
            ", ".join(html.escape(word) for word in snap.matched_words)
            if snap.matched_words
            else "filtro DDX 10min"
        )
        message_text = html.escape(_shorten_text(snap.text_value))
        notice = (
            "Tigrão — DDX 10min apagou mensagem\n\n"
            f"Grupo: {group_title} ({snap.chat_id})\n"
            f"Autor: {author_name} — <code>{snap.user_id}</code>{username_line}\n"
            f"Mensagem ID: <code>{snap.message_id}</code>\n"
            f"Filtro: {matched_text}\n"
            f"Atraso aplicado: 10 minutos\n\n"
            f"Mensagem apagada:\n<blockquote>{message_text}</blockquote>"
        )
        await bot.send_message(chat_id=OWNER_ID, text=notice, parse_mode="HTML")
        logger.warning(
            "TIGRAO_DDX_SOFT_OWNER_NOTIFIED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )
    except Exception:
        logger.exception(
            "TIGRAO_DDX_SOFT_OWNER_NOTIFY_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )


async def _notify_owner_ddx_soft_failed(
    bot, snap: _Snapshot, error_type: str, error_message: str
) -> None:
    """DM ao owner quando o delete falha (forbidden, bad_request
    não-noop, exception). NÃO dispara em `noop` (msg já tinha sumido
    por outro motivo — silencioso é desejado)."""
    if not OWNER_ID:
        return
    try:
        author_name = html.escape(snap.user_full_name or "desconhecido")
        username_line = (
            f"\nUsername: @{html.escape(snap.user_username)}"
            if snap.user_username
            else ""
        )
        group_title = html.escape(snap.chat_title or str(snap.chat_id))
        matched_text = (
            ", ".join(html.escape(word) for word in snap.matched_words)
            if snap.matched_words
            else "filtro DDX 10min"
        )
        message_text = html.escape(_shorten_text(snap.text_value))
        error_text = html.escape(_shorten_text(error_message, limit=300))
        notice = (
            "Tigrão — DDX 10min FALHOU ao apagar\n\n"
            f"Grupo: {group_title} ({snap.chat_id})\n"
            f"Autor: {author_name} — <code>{snap.user_id}</code>{username_line}\n"
            f"Mensagem ID: <code>{snap.message_id}</code>\n"
            f"Filtro: {matched_text}\n"
            f"Erro: <code>{html.escape(error_type)}</code>\n"
            f"Motivo: {error_text}\n\n"
            f"Mensagem que ficou no grupo:\n<blockquote>{message_text}</blockquote>"
        )
        await bot.send_message(chat_id=OWNER_ID, text=notice, parse_mode="HTML")
        logger.warning(
            "TIGRAO_DDX_SOFT_OWNER_FAILED_NOTIFIED | chat_id=%s | user_id=%s | message_id=%s | error_type=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
            error_type,
        )
    except Exception:
        logger.exception(
            "TIGRAO_DDX_SOFT_OWNER_FAILED_NOTIFY_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )


async def _delete_after_delay(bot, snap: _Snapshot, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _scheduled.discard((snap.chat_id, snap.message_id))
        raise
    try:
        await bot.delete_message(chat_id=snap.chat_id, message_id=snap.message_id)
        log_action(
            chat_id=snap.chat_id,
            action="ddx_soft_delete",
            target_user_id=snap.user_id,
            status="success",
        )
        logger.warning(
            "TIGRAO_DDX_SOFT_DELETED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )
        # DM ao owner em sucesso. noop (msg já foi por outro motivo)
        # permanece silencioso por design. Caminhos de erro têm DM
        # separada disparada nos respectivos except (forbidden,
        # bad_request não-noop, exception).
        await _notify_owner_ddx_soft_deleted(bot, snap)
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
            chat_id=snap.chat_id,
            action="ddx_soft_delete",
            target_user_id=snap.user_id,
            status="noop" if is_already_gone else "error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        if is_already_gone:
            logger.info(
                "TIGRAO_DDX_SOFT_ALREADY_GONE | chat_id=%s | user_id=%s | message_id=%s | reason=%s",
                snap.chat_id,
                snap.user_id,
                snap.message_id,
                exc,
            )
        else:
            logger.warning(
                "TIGRAO_DDX_SOFT_BAD_REQUEST | chat_id=%s | user_id=%s | message_id=%s | reason=%s",
                snap.chat_id,
                snap.user_id,
                snap.message_id,
                exc,
            )
            # noop NÃO notifica (silencioso desejado); só error real.
            await _notify_owner_ddx_soft_failed(
                bot, snap, type(exc).__name__, str(exc)
            )
    except TelegramForbiddenError as exc:
        log_action(
            chat_id=snap.chat_id,
            action="ddx_soft_delete",
            target_user_id=snap.user_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.warning(
            "TIGRAO_DDX_SOFT_FORBIDDEN | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )
        await _notify_owner_ddx_soft_failed(
            bot, snap, type(exc).__name__, str(exc)
        )
    except Exception as exc:
        log_action(
            chat_id=snap.chat_id,
            action="ddx_soft_delete",
            target_user_id=snap.user_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logger.exception(
            "TIGRAO_DDX_SOFT_DELETE_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
        )
        await _notify_owner_ddx_soft_failed(
            bot, snap, type(exc).__name__, str(exc)
        )
    finally:
        _scheduled.discard((snap.chat_id, snap.message_id))


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

    # Captura snapshot AGORA — em 10min a msg pode ter sumido e a DM
    # ao owner ficaria sem contexto. Texto/autor/grupo persistidos
    # no objeto que vai junto pra task.
    snap = _Snapshot(
        chat_id=int(message.chat.id),
        message_id=int(message.message_id),
        chat_title=message.chat.title or str(message.chat.id),
        user_id=int(message.from_user.id),
        user_full_name=message.from_user.full_name or "desconhecido",
        user_username=message.from_user.username,
        text_value=text_value,
        matched_words=_matching_words(text_value, words),
    )

    _scheduled.add(key)
    try:
        log_action(
            chat_id=snap.chat_id,
            action="ddx_soft_scheduled",
            target_user_id=snap.user_id,
            status="success",
        )
        logger.info(
            "TIGRAO_DDX_SOFT_SCHEDULED | chat_id=%s | user_id=%s | message_id=%s | delay=%ss",
            snap.chat_id,
            snap.user_id,
            snap.message_id,
            DDX_SOFT_DELAY_SECONDS,
        )
        # DM "agendou" em background — não bloqueia o preprocess
        # (que precisa retornar rápido pro próximo update). Falha
        # na DM não cancela o agendamento do delete.
        asyncio.create_task(_notify_owner_ddx_soft_scheduled(bot, snap))
        asyncio.create_task(_delete_after_delay(bot, snap, DDX_SOFT_DELAY_SECONDS))
    except Exception:
        _scheduled.discard(key)
        logger.exception(
            "TIGRAO_DDX_SOFT_SCHEDULE_FAILED | chat_id=%s | message_id=%s",
            snap.chat_id,
            snap.message_id,
        )
    return False
