from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)
router = Router(name="qb")


def _safe_button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    """Create an inline button with optional Telegram client color style."""
    try:
        if style:
            return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)  # type: ignore[call-arg]
    except Exception:
        pass
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def qb_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _safe_button("🔇 Mute", f"qb:mute:{chat_id}:{user_id}", style="primary"),
                _safe_button("🚷 Ban", f"qb:ban:{chat_id}:{user_id}", style="danger"),
            ]
        ]
    )


async def is_admin(message_or_callback: Message | CallbackQuery, chat_id: int, user_id: int) -> bool:
    try:
        member = await message_or_callback.bot.get_chat_member(chat_id, user_id)
    except Exception:
        logger.exception("QB_ADMIN_CHECK_FAILED | chat_id=%s | user_id=%s", chat_id, user_id)
        return False
    return member.status in {"administrator", "creator"}


@router.message(Command("qb"))
async def qb_panel(message: Message) -> None:
    logger.warning(
        "QB_HANDLER_RECEIVED | chat_type=%s | chat_id=%s | from_id=%s | has_reply=%s",
        getattr(message.chat, "type", None),
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        bool(message.reply_to_message),
    )
    if not message.from_user:
        logger.warning("QB_HANDLER_SKIP | reason=no_from_user")
        return
    if message.chat.type not in {"group", "supergroup"}:
        logger.warning("QB_HANDLER_PRIVATE_OR_UNSUPPORTED | chat_type=%s", message.chat.type)
        await message.reply("Use /qb respondendo a mensagem de um usuário dentro do grupo.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        logger.warning("QB_HANDLER_NO_REPLY | chat_id=%s | from_id=%s", message.chat.id, message.from_user.id)
        await message.reply("Responda a mensagem do usuário.")
        return
    if not await is_admin(message, message.chat.id, message.from_user.id):
        logger.warning("QB_HANDLER_DENIED | chat_id=%s | from_id=%s", message.chat.id, message.from_user.id)
        await message.reply("Sem permissão.")
        return

    target = message.reply_to_message.from_user
    username = f"@{html.escape(target.username)}" if target.username else "sem username"
    full_name = html.escape(target.full_name or "Usuário")

    text = (
        f"🆔 ID: <code>{target.id}</code>\n"
        f"👱 Nome: {full_name}\n"
        f"🌐 Nome de usuário: {username}"
    )

    await message.reply(
        text,
        reply_markup=qb_keyboard(message.chat.id, target.id),
        parse_mode="HTML",
    )
    logger.warning("QB_HANDLER_ANSWER_SENT | chat_id=%s | target_id=%s", message.chat.id, target.id)


@router.callback_query(F.data.startswith("qb:"))
async def qb_callback(callback: CallbackQuery) -> None:
    logger.warning(
        "QB_CALLBACK_RECEIVED | from_id=%s | data=%s",
        getattr(callback.from_user, "id", None),
        callback.data,
    )
    if not callback.data or not callback.from_user:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Dados inválidos.", show_alert=True)
        return

    _, action, raw_chat_id, raw_target_id = parts
    try:
        chat_id = int(raw_chat_id)
        target_id = int(raw_target_id)
    except ValueError:
        await callback.answer("Dados inválidos.", show_alert=True)
        return

    if not await is_admin(callback, chat_id, callback.from_user.id):
        logger.warning("QB_CALLBACK_DENIED | chat_id=%s | from_id=%s", chat_id, callback.from_user.id)
        await callback.answer("Sem permissão.", show_alert=True)
        return

    if not callback.message:
        await callback.answer("Mensagem indisponível.", show_alert=True)
        return

    try:
        if action == "mute":
            await callback.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
            result = f"{callback.message.html_text}\n\n~ Usuário <code>{target_id}</code> foi 🔇 silenciado."
        elif action == "ban":
            await callback.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
            result = f"{callback.message.html_text}\n\n~ Usuário <code>{target_id}</code> foi 🚷 banido."
        else:
            await callback.answer("Ação inválida.", show_alert=True)
            return
    except Exception as exc:
        logger.exception("QB_ACTION_FAILED | action=%s | chat_id=%s | target_id=%s", action, chat_id, target_id)
        await callback.answer(f"Falha: {type(exc).__name__}", show_alert=True)
        return

    await callback.message.edit_text(result, parse_mode="HTML")
    await callback.answer("Ação executada.")
    logger.warning("QB_CALLBACK_DONE | action=%s | chat_id=%s | target_id=%s", action, chat_id, target_id)
