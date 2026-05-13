from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import set_member_tag
from app.moderation_tigrao.keyboards import customize_keyboard, home_keyboard
from app.moderation_tigrao.parsers import parse_user_id
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, get_session, set_action
from app.moderation_tigrao.storage import log_action
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_member_tag")


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de alterar tag de membro.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _is_waiting_member_tag_text(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for in {"member_tag_user_id", "member_tag_value"}


@router.callback_query(F.data == "tigrao:customize:member_tag")
async def tigrao_member_tag_start(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return

    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return

    set_action("member_tag", waiting_for="member_tag_user_id")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — tag de membro\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora apenas o user_id do membro."
        )
    await callback.answer()


@router.message(F.text, _is_waiting_member_tag_text)
async def tigrao_member_tag_receive_text(message: Message) -> None:
    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return

    if session.waiting_for == "member_tag_user_id":
        try:
            user_id = parse_user_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("User ID inválido", str(exc), "Envie apenas o user_id numérico, sem hífen."))
            return
        session.payload["target_user_id"] = user_id
        session.waiting_for = "member_tag_value"
        await message.answer(
            "Tigrão — tag de membro\n\n"
            f"Grupo: {session.selected_chat_id}\n"
            f"Usuário: {user_id}\n\n"
            "Envie agora a tag que será aplicada.\n"
            "Para remover a tag, envie apenas um ponto: ."
        )
        return

    if session.waiting_for == "member_tag_value":
        target_user_id = session.payload.get("target_user_id")
        if not target_user_id:
            clear_action()
            await message.answer(
                error_text("Fluxo inválido", "O user_id do alvo não foi encontrado.", "Recomece a ação de tag de membro."),
                reply_markup=customize_keyboard(),
            )
            return

        raw_tag = (message.text or "").strip()
        tag = "" if raw_tag == "." else raw_tag
        if len(tag) > 16:
            await message.answer(
                error_text("Tag muito longa", "A tag deve ter no máximo 16 caracteres.", "Envie uma tag mais curta."),
                reply_markup=customize_keyboard(),
            )
            return

        try:
            await set_member_tag(message.bot, int(session.selected_chat_id), int(target_user_id), tag)
            log_action(chat_id=int(session.selected_chat_id), action="member_tag", target_user_id=int(target_user_id), status="success")
            clear_action()
            detail = "Tag removida" if tag == "" else f"Tag aplicada: {tag}"
            await message.answer(
                success_text("Tag de membro atualizada", f"Grupo: {session.selected_chat_id}\nUsuário: {target_user_id}\n{detail}"),
                reply_markup=customize_keyboard(),
            )
        except Exception as exc:
            log_action(
                chat_id=int(session.selected_chat_id),
                action="member_tag",
                target_user_id=int(target_user_id),
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            clear_action()
            await message.answer(
                error_text(
                    "Falha ao alterar tag",
                    f"{type(exc).__name__}: {exc}",
                    "Confira se o grupo suporta tags, se o bot possui can_manage_tags e se o user_id pertence ao grupo.",
                ),
                reply_markup=customize_keyboard(),
            )
