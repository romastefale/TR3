from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import copy_message
from app.moderation_tigrao.keyboards import customize_keyboard, home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, get_session, set_action
from app.moderation_tigrao.storage import log_action
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_pinned_media")

MEDIA_FILTER = F.photo | F.video | F.document | F.animation | F.sticker | F.audio | F.voice | F.video_note


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar esta ação.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _is_owner_waiting_pinned_media(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for == "outbound_media_pin"


@router.callback_query(F.data == "tigrao:message:media_pin")
async def tigrao_send_media_pin(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("send_media_pin", waiting_for="outbound_media_pin", pin=True)
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — enviar mídia e fixar\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora a foto, vídeo, documento, animação, sticker, áudio, voz ou outra mídia que será copiada e fixada no grupo.\n\n"
            "Se a mídia tiver legenda, ela será preservada pelo Telegram ao copiar."
        )
    await callback.answer()


@router.message(MEDIA_FILTER, _is_owner_waiting_pinned_media)
async def tigrao_private_pinned_media(message: Message) -> None:
    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    chat_id = int(session.selected_chat_id)
    try:
        copied_id = await copy_message(
            message.bot,
            target_chat_id=chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await message.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=copied_id,
            disable_notification=True,
        )
        log_action(chat_id=chat_id, action="send_media_pin", status="success")
        clear_action()
        await message.answer(
            success_text(
                "Mídia enviada e fixada",
                f"Grupo: {chat_id}\nMensagem: {copied_id}",
            ),
            reply_markup=customize_keyboard(),
        )
    except TelegramForbiddenError as exc:
        log_action(
            chat_id=chat_id,
            action="send_media_pin",
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        clear_action()
        await message.answer(
            error_text(
                "Permissão insuficiente",
                "O Telegram recusou o envio ou a fixação da mídia.",
                "Confira se o bot pode enviar mídia e fixar mensagens no grupo.",
            ),
            reply_markup=customize_keyboard(),
        )
    except Exception as exc:
        log_action(
            chat_id=chat_id,
            action="send_media_pin",
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        clear_action()
        await message.answer(
            error_text(
                "Falha ao enviar e fixar mídia",
                f"{type(exc).__name__}: {exc}",
                "Confira grupo, mídia e permissões do bot.",
            ),
            reply_markup=customize_keyboard(),
        )
