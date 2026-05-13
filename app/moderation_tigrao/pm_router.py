from __future__ import annotations

import html
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from app.config.settings import OWNER_ID
from app.moderation_tigrao.actions import ban_user
from app.moderation_tigrao.pm_storage import (
    delete_suspicious_message,
    get_suspicious_message,
    is_pm_enabled,
    is_recently_first_seen,
    list_pm_settings,
    mark_member_seen,
    save_suspicious_message,
    set_pm_enabled,
    update_suspicious_status,
)
from app.moderation_tigrao.storage import list_groups, remember_group

router = Router(name="moderation_tigrao_pm")

URL_RE = re.compile(r"(?i)(https?://|t\.me/|telegram\.me/|www\.|\.com\b|\.net\b|\.org\b|\.br\b)")
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]",
    flags=re.UNICODE,
)


def _button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    if style:
        try:
            return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)  # type: ignore[call-arg]
        except Exception:
            pass
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _pm_groups_keyboard() -> InlineKeyboardMarkup:
    enabled_map = {int(row["chat_id"]): int(row.get("enabled") or 0) for row in list_pm_settings()}
    rows: list[list[InlineKeyboardButton]] = []
    for group in list_groups()[:12]:
        chat_id = int(group["chat_id"])
        title = str(group.get("title") or chat_id)
        label = title if len(title) <= 32 else title[:29] + "..."
        status = "ON" if enabled_map.get(chat_id) == 1 else "OFF"
        rows.append([_button(f"{status} · {label}", f"tigraopm:toggle:{chat_id}", "primary")])
    rows.append([_button("Atualizar", "tigraopm:menu", "primary"), _button("Fechar", "tigraopm:close", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _alert_keyboard(snapshot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Banir + apagar tudo", f"tigraopm:ban:{snapshot_id}", "danger")],
            [_button("Ignorar", f"tigraopm:ignore:{snapshot_id}", "primary")],
        ]
    )


def _detect_reason(text_value: str) -> str | None:
    text = text_value.strip()
    if not text:
        return None
    if URL_RE.search(text):
        return "link suspeito"
    emojis = EMOJI_RE.findall(text)
    if len(emojis) >= 8:
        return "muitos emojis em sequência"
    if re.search(r"([!?.🔥🚨✅⭐️💎🎁🚀])\1{5,}", text):
        return "caractere/emoji repetido"
    return None


def _message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _user_name(message: Message) -> str:
    if not message.from_user:
        return "-"
    return message.from_user.full_name or message.from_user.username or str(message.from_user.id)


async def _send_pm_alert(message: Message, snapshot_id: int, reason: str, text_value: str) -> None:
    if not OWNER_ID:
        return
    chat_title = message.chat.title or str(message.chat.id)
    user_id = message.from_user.id if message.from_user else 0
    user_name = html.escape(_user_name(message))
    safe_chat = html.escape(chat_title)
    safe_reason = html.escape(reason)
    safe_text = html.escape(text_value[:1200])
    body = (
        "Tigrão PM — mensagem suspeita\n\n"
        f"Grupo: {safe_chat}\n"
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Usuário: {user_name}\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Motivo: {safe_reason}\n\n"
        "Conteúdo salvo:\n"
        f"<blockquote>{safe_text}</blockquote>"
    )
    await message.bot.send_message(
        chat_id=OWNER_ID,
        text=body,
        parse_mode="HTML",
        reply_markup=_alert_keyboard(snapshot_id),
    )


async def process_tigrao_pm_message(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    if message.chat.type not in {"group", "supergroup"}:
        return

    chat_id = int(message.chat.id)
    remember_group(chat_id, message.chat.title or str(chat_id))
    if not is_pm_enabled(chat_id):
        mark_member_seen(chat_id, int(message.from_user.id))
        return

    is_first_seen = mark_member_seen(chat_id, int(message.from_user.id))
    if not is_first_seen and not is_recently_first_seen(chat_id, int(message.from_user.id), minutes=30):
        return

    text_value = _message_text(message)
    reason = _detect_reason(text_value)
    if not reason:
        return

    snapshot_id = save_suspicious_message(
        chat_id=chat_id,
        chat_title=message.chat.title or str(chat_id),
        message_id=int(message.message_id),
        user_id=int(message.from_user.id),
        user_name=_user_name(message),
        text_value=text_value,
        reason=reason,
    )
    await _send_pm_alert(message, snapshot_id, reason, text_value)


async def tigrao_pm_preprocess_update(update: Update) -> None:
    message = update.message or update.edited_message
    if not message:
        return
    await process_tigrao_pm_message(message)


@router.message(Command("tigraopm"))
async def tigrao_pm_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    await message.answer(
        "Tigrão PM — proteção de membros novos\n\n"
        "Ative ou desative por grupo.\n"
        "Quando ativo, o bot salva mensagens suspeitas de membros novos e envia alerta no privado com ação rápida.",
        reply_markup=_pm_groups_keyboard(),
    )


@router.callback_query(F.data == "tigraopm:menu")
async def tigrao_pm_menu(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            "Tigrão PM — proteção de membros novos\n\n"
            "Ative ou desative por grupo.",
            reply_markup=_pm_groups_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tigraopm:toggle:"))
async def tigrao_pm_toggle(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Acesso negado.", show_alert=True)
        return
    try:
        chat_id = int((callback.data or "").rsplit(":", 1)[-1])
    except Exception:
        await callback.answer("Grupo inválido.", show_alert=True)
        return

    title = str(chat_id)
    for group in list_groups():
        if int(group["chat_id"]) == chat_id:
            title = str(group.get("title") or chat_id)
            break
    enabled = not is_pm_enabled(chat_id)
    set_pm_enabled(chat_id, title, enabled)
    if callback.message:
        await callback.message.edit_text(
            f"Tigrão PM — {'ativado' if enabled else 'desativado'}\n\nGrupo: {html.escape(title)}\nChat ID: <code>{chat_id}</code>",
            parse_mode="HTML",
            reply_markup=_pm_groups_keyboard(),
        )
    await callback.answer("Ativado" if enabled else "Desativado")


@router.callback_query(F.data == "tigraopm:close")
async def tigrao_pm_close(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão PM fechado.")
    await callback.answer()


@router.callback_query(F.data.startswith("tigraopm:ignore:"))
async def tigrao_pm_ignore(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Acesso negado.", show_alert=True)
        return
    try:
        snapshot_id = int((callback.data or "").rsplit(":", 1)[-1])
    except Exception:
        await callback.answer("Registro inválido.", show_alert=True)
        return
    update_suspicious_status(snapshot_id, "ignored")
    delete_suspicious_message(snapshot_id)
    if callback.message:
        await callback.message.edit_text((callback.message.text or "") + "\n\nStatus: ignorado. Dados locais removidos.")
    await callback.answer("Ignorado")


@router.callback_query(F.data.startswith("tigraopm:ban:"))
async def tigrao_pm_ban(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Acesso negado.", show_alert=True)
        return
    try:
        snapshot_id = int((callback.data or "").rsplit(":", 1)[-1])
    except Exception:
        await callback.answer("Registro inválido.", show_alert=True)
        return
    snapshot = get_suspicious_message(snapshot_id)
    if not snapshot:
        await callback.answer("Registro não encontrado.", show_alert=True)
        return
    try:
        await ban_user(callback.bot, int(snapshot["chat_id"]), int(snapshot["user_id"]))
        update_suspicious_status(snapshot_id, "banned")
        delete_suspicious_message(snapshot_id)
        if callback.message:
            await callback.message.edit_text((callback.message.text or "") + "\n\nStatus: banido, mensagens removidas e dados locais apagados.")
        await callback.answer("Banido")
    except Exception as exc:
        update_suspicious_status(snapshot_id, f"ban_error:{type(exc).__name__}")
        await callback.answer(f"Erro: {type(exc).__name__}", show_alert=True)
