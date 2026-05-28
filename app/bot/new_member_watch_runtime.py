"""Sprint X4: preprocessor que detecta membro novo + msg com link e
notifica owner via DM com botões de moderação.

Roda ANTES do DDX no webhook (app/main.py) — assim:
- Captura o texto da msg suspeita ANTES que DDX/outro bot apague.
- Não consome o update: sempre retorna False pra que o pipeline normal
  (DDX preprocess + dispatcher.feed_update) continue.
- Owner pode clicar [Apagar] no DM mesmo depois do DDX apagar; tratamos
  TelegramBadRequest/MessageToDeleteNotFound silenciosamente.

Sprint X5: UX nativa Bot API 10.0 — `style` nativo nos botões,
`CopyTextButton` pros IDs e `LinkPreviewOptions` na DM. Sem emojis na
interface (política do owner — sinalização via cor `danger`/`success`/
`primary`).
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from app.config.settings import OWNER_ID
from app.moderation_tigrao.permissions import is_moderator_user
from app.services.new_member_watch import new_member_watch_service
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

_LINK_ENTITY_TYPES = {"url", "text_link"}


def _has_link(message: Any) -> bool:
    """True se a msg/caption contém url ou text_link via entities."""
    for entities in (message.entities, message.caption_entities):
        if not entities:
            continue
        for ent in entities:
            if ent.type in _LINK_ENTITY_TYPES:
                return True
    return False


def _shorten(value: str, limit: int = 600) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _build_keyboard(chat_id: int, user_id: int, message_id: int) -> InlineKeyboardMarkup:
    """Botões inline pro DM do owner.

    Layout (sem emojis, sinalização por cor Bot API 10.0):
    - Linha 1: Banir (danger) | Mutar 1h (primary)
    - Linha 2: Apagar msg (primary) | Ignorar (success)
    - Linha 3: Copiar ID user | Copiar ID chat (CopyTextButton, Bot API 10.0)

    callback_data ≤ 64 bytes (chat_id ~14 chars + user_id/msg_id ~10 cada).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Banir",
                    callback_data=f"tigrao:nmw:ban:{chat_id}:{user_id}",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="Mutar 1h",
                    callback_data=f"tigrao:nmw:mute:{chat_id}:{user_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Apagar msg",
                    callback_data=f"tigrao:nmw:del:{chat_id}:{message_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="Ignorar",
                    callback_data="tigrao:nmw:ignore",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Copiar ID user",
                    copy_text=CopyTextButton(text=str(user_id)),
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="Copiar ID chat",
                    copy_text=CopyTextButton(text=str(chat_id)),
                    style="primary",
                ),
            ],
        ]
    )


async def _notify_owner_new_member_link(bot, message: Any, info: dict) -> None:
    if not OWNER_ID:
        return
    try:
        author = message.from_user
        author_name = html.escape(
            info.get("user_name")
            or (author.full_name if author else "desconhecido")
        )
        username = info.get("user_username") or getattr(author, "username", None)
        username_line = f"\nUsername: @{html.escape(username)}" if username else ""
        group_title = html.escape(message.chat.title or str(message.chat.id))
        text_value = message.text or message.caption or ""
        body = html.escape(_shorten(text_value))

        joined_at = info.get("joined_at")
        if joined_at is not None:
            try:
                delta = utcnow_naive() - joined_at
                mins = max(0, int(delta.total_seconds() // 60))
                joined_line = f"\nEntrou: há {mins} min"
            except Exception:
                joined_line = ""
        else:
            joined_line = ""

        idx = info.get("alert_index", 1)
        cap = info.get("alert_max", 5)

        notice = (
            "<b>Tigrão — membro novo postou link</b>\n\n"
            f"Grupo: <b>{group_title}</b> (<code>{message.chat.id}</code>)\n"
            f"Membro: {author_name} — <code>{info.get('user_id')}</code>"
            f"{username_line}{joined_line}\n"
            f"Alerta {idx} de {cap}\n"
            f"Mensagem ID: <code>{message.message_id}</code>\n\n"
            f"Texto:\n<blockquote>{body}</blockquote>"
        )

        await bot.send_message(
            chat_id=OWNER_ID,
            text=notice,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=_build_keyboard(
                chat_id=int(message.chat.id),
                user_id=int(info["user_id"]),
                message_id=int(message.message_id),
            ),
        )
        logger.warning(
            "TIGRAO_NMW_OWNER_NOTIFIED | chat_id=%s | user_id=%s | message_id=%s | idx=%s",
            message.chat.id, info.get("user_id"), message.message_id, idx,
        )
    except Exception:
        logger.exception(
            "TIGRAO_NMW_OWNER_NOTIFY_FAILED | chat_id=%s | user_id=%s | message_id=%s",
            getattr(message.chat, "id", None),
            info.get("user_id"),
            getattr(message, "message_id", None),
        )


async def tigrao_new_member_watch_preprocess_update(bot, update) -> bool:
    """Preprocessor invocado pelo webhook ANTES do DDX.

    Sempre retorna False — nunca consome o update. Apenas observa e
    dispara DM. Qualquer falha é silenciosa (não interrompe o pipeline).
    """
    try:
        message = getattr(update, "message", None)
        if message is None:
            return False
        if message.chat.type not in {"group", "supergroup"}:
            return False

        # 1) Service msg: novo(s) membro(s) entraram. Marca em new_member_watch.
        new_members = getattr(message, "new_chat_members", None)
        if new_members:
            for member in new_members:
                if getattr(member, "is_bot", False):
                    continue
                if is_moderator_user(member.id):
                    continue
                new_member_watch_service.register_join(
                    chat_id=int(message.chat.id),
                    user_id=int(member.id),
                    user_name=member.full_name or None,
                    user_username=getattr(member, "username", None),
                )
            return False

        # 2) Msg comum: se autor é membro novo + tem link, consome 1 slot
        #    de alerta e dispara DM. Cap 5 por membro, TTL 24h via service.
        if not message.from_user:
            return False
        if is_moderator_user(message.from_user.id):
            return False
        # Sprint X9 (S2): self-bot whitelist. Sem isso, msgs postadas pelo
        # próprio bot (ex: confirmação esteganográfica X9, ou qualquer
        # send_message com link) podem disparar alerta de "membro novo
        # postou link". get_me() é cacheado pelo aiogram após a 1ª chamada.
        try:
            me = await bot.get_me()
            if message.from_user.id == me.id:
                return False
        except Exception:
            pass
        if not _has_link(message):
            return False

        info = new_member_watch_service.consume_alert_slot(
            chat_id=int(message.chat.id),
            user_id=int(message.from_user.id),
        )
        if info is None:
            return False

        await _notify_owner_new_member_link(bot, message, info)
        new_member_watch_service.maybe_purge()
        return False
    except Exception:
        logger.exception(
            "TIGRAO_NMW_PREPROCESS_FAILED | update_id=%s",
            getattr(update, "update_id", None),
        )
        return False
