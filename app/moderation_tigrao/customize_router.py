from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import set_group_photo
from app.moderation_tigrao.keyboards import customize_keyboard, home_keyboard
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, get_session, set_action
from app.moderation_tigrao.storage import log_action
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_customize")


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de alterar a foto.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _is_waiting_group_photo(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for == "customize_photo"


@router.callback_query(F.data == "tigrao:customize:photo")
async def tigrao_customize_photo(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("customize_photo", waiting_for="customize_photo")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — alterar foto do grupo\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora a imagem no privado do bot.\n"
            "Use uma foto/imagem em boa resolução. O Telegram aplicará o recorte próprio da foto do grupo."
        )
    await callback.answer()


@router.message(F.photo | F.document, _is_waiting_group_photo)
async def tigrao_receive_group_photo(message: Message) -> None:
    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    photo = None
    filename = "group_photo.jpg"
    if message.photo:
        photo = message.photo[-1]
    elif message.document and str(message.document.mime_type or "").startswith("image/"):
        photo = message.document
        filename = message.document.file_name or "group_photo.jpg"

    if not photo:
        await message.answer(
            error_text("Imagem inválida", "Envie uma foto ou documento de imagem.", "Use imagem JPG/PNG em boa resolução."),
            reply_markup=customize_keyboard(),
        )
        return

    try:
        file = await message.bot.get_file(photo.file_id)
        image_bytes = await message.bot.download_file(file.file_path)
        if image_bytes is None:
            raise RuntimeError("download_file retornou vazio")
        raw = image_bytes.read()
        await set_group_photo(message.bot, int(session.selected_chat_id), raw, filename=filename)
        log_action(chat_id=int(session.selected_chat_id), action="customize_photo", status="success")
        clear_action()
        await message.answer(
            success_text("Foto do grupo alterada", f"Grupo: {session.selected_chat_id}\nArquivo: {filename}"),
            reply_markup=customize_keyboard(),
        )
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(session.selected_chat_id), action="customize_photo", status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        await message.answer(
            error_text(
                "Permissão insuficiente",
                f"O Telegram recusou a alteração da foto. Erro: {type(exc).__name__}: {exc}",
                "Confira se o bot é administrador e possui permissão para alterar informações do grupo.",
            ),
            reply_markup=customize_keyboard(),
        )
    except Exception as exc:
        log_action(chat_id=int(session.selected_chat_id), action="customize_photo", status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        await message.answer(
            error_text("Falha ao alterar foto", f"{type(exc).__name__}: {exc}", "Confira a imagem, o grupo e as permissões do bot."),
            reply_markup=customize_keyboard(),
        )
