"""Sprint X4: callbacks do DM de alerta de membro novo.

Botões: Banir / Mutar 1h / Apagar msg / Ignorar.
Owner-only via is_owner_callback. Hard-block: nunca age sobre OWNER_ID.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, LinkPreviewOptions

from app.moderation_tigrao.actions import ban_user, delete_message, mute_user
from app.moderation_tigrao.permissions import OWNER_ID, is_moderator_user, is_owner_callback
from app.moderation_tigrao.storage import log_action

logger = logging.getLogger(__name__)

router = Router(name="moderation_tigrao_new_member_watch")


def _parse_two_ints(suffix: str) -> tuple[int, int] | None:
    parts = suffix.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


async def _append_status(callback: CallbackQuery, line: str) -> None:
    """Anexa uma linha de status ao DM, mantendo o texto original. Em
    caso de falha no edit, cai pra answer com alerta.

    Sprint X5: usa LinkPreviewOptions (forma moderna, Bot API 10.0) em
    vez de disable_web_page_preview deprecado.
    """
    msg = callback.message
    if msg is None:
        await callback.answer(line, show_alert=True)
        return
    base = msg.html_text if msg.text else (msg.caption or "")
    new_text = f"{base}\n\n— {line}"
    try:
        await msg.edit_text(
            new_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest:
        try:
            await msg.answer(line)
        except Exception:
            pass


@router.callback_query(F.data.startswith("tigrao:nmw:ban:"))
async def tigrao_nmw_ban(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    parsed = _parse_two_ints(callback.data[len("tigrao:nmw:ban:"):])
    if parsed is None:
        await callback.answer("Callback inválido.", show_alert=True)
        return
    chat_id, user_id = parsed
    if is_moderator_user(user_id):
        await callback.answer("Não posso banir um moderador.", show_alert=True)
        return
    try:
        await ban_user(callback.bot, chat_id, user_id)
        log_action(chat_id=chat_id, action="nmw_ban", target_user_id=user_id, status="success")
        await _append_status(callback, f"Banido (user <code>{user_id}</code>).")
        await callback.answer("Banido.")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log_action(
            chat_id=chat_id, action="nmw_ban", target_user_id=user_id,
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        await callback.answer(f"Falha ao banir: {type(exc).__name__}", show_alert=True)
    except Exception as exc:
        log_action(
            chat_id=chat_id, action="nmw_ban", target_user_id=user_id,
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        logger.exception("TIGRAO_NMW_BAN_FAILED chat=%s user=%s", chat_id, user_id)
        await callback.answer("Erro inesperado.", show_alert=True)


@router.callback_query(F.data.startswith("tigrao:nmw:mute:"))
async def tigrao_nmw_mute(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    parsed = _parse_two_ints(callback.data[len("tigrao:nmw:mute:"):])
    if parsed is None:
        await callback.answer("Callback inválido.", show_alert=True)
        return
    chat_id, user_id = parsed
    if is_moderator_user(user_id):
        await callback.answer("Não posso mutar um moderador.", show_alert=True)
        return
    try:
        await mute_user(callback.bot, chat_id, user_id, timedelta(hours=1))
        log_action(chat_id=chat_id, action="nmw_mute_1h", target_user_id=user_id, status="success")
        await _append_status(callback, f"Mutado 1h (user <code>{user_id}</code>).")
        await callback.answer("Mutado por 1h.")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log_action(
            chat_id=chat_id, action="nmw_mute_1h", target_user_id=user_id,
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        await callback.answer(f"Falha ao mutar: {type(exc).__name__}", show_alert=True)
    except Exception as exc:
        log_action(
            chat_id=chat_id, action="nmw_mute_1h", target_user_id=user_id,
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        logger.exception("TIGRAO_NMW_MUTE_FAILED chat=%s user=%s", chat_id, user_id)
        await callback.answer("Erro inesperado.", show_alert=True)


@router.callback_query(F.data.startswith("tigrao:nmw:del:"))
async def tigrao_nmw_del(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    parsed = _parse_two_ints(callback.data[len("tigrao:nmw:del:"):])
    if parsed is None:
        await callback.answer("Callback inválido.", show_alert=True)
        return
    chat_id, message_id = parsed
    try:
        await delete_message(callback.bot, chat_id, message_id)
        log_action(chat_id=chat_id, action="nmw_delete", status="success")
        await _append_status(callback, f"Msg <code>{message_id}</code> apagada.")
        await callback.answer("Apagada.")
    except TelegramBadRequest as exc:
        # Caso típico: outro bot (Rose/Help/DDX) já apagou. Reporta como
        # info, não como erro — o objetivo do owner foi atingido de qualquer jeito.
        log_action(
            chat_id=chat_id, action="nmw_delete",
            status="noop", error_type=type(exc).__name__, error_message=str(exc),
        )
        await _append_status(callback, "Msg já havia sido apagada (outro bot/admin).")
        await callback.answer("Já estava apagada.")
    except TelegramForbiddenError as exc:
        log_action(
            chat_id=chat_id, action="nmw_delete",
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        await callback.answer("Sem permissão pra apagar.", show_alert=True)
    except Exception as exc:
        log_action(
            chat_id=chat_id, action="nmw_delete",
            status="error", error_type=type(exc).__name__, error_message=str(exc),
        )
        logger.exception("TIGRAO_NMW_DEL_FAILED chat=%s msg=%s", chat_id, message_id)
        await callback.answer("Erro inesperado.", show_alert=True)


@router.callback_query(F.data == "tigrao:nmw:ignore")
async def tigrao_nmw_ignore(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    await _append_status(callback, "Ignorado.")
    await callback.answer("Ignorado.")
