from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import set_member_tag
from app.moderation_tigrao.keyboards import customize_keyboard, home_keyboard
from app.moderation_tigrao.parsers import parse_user_id
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import (
    clear_action,
    consume_if_expired,
    get_session,
    set_action,
    touch_session,
)
from app.moderation_tigrao.storage import log_action
from app.moderation_tigrao.texts import error_text, success_text

router = Router(name="moderation_tigrao_member_tag")

REGULAR_TAGGABLE_STATUSES = {"member", "restricted"}


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de alterar tag de membro.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _is_waiting_member_tag_text(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for in {"member_tag_user_id", "member_tag_value"}


def _has_manage_tags(member) -> bool:
    return bool(getattr(member, "can_manage_tags", False) or getattr(member, "can_pin_messages", False))


async def _validate_member_tag_request(message: Message, chat_id: int, target_user_id: int) -> str | None:
    bot_me = await message.bot.get_me()
    bot_member = await message.bot.get_chat_member(chat_id, bot_me.id)
    if getattr(bot_member, "status", None) not in {"administrator", "creator"}:
        return "O bot não é administrador neste grupo."
    if not _has_manage_tags(bot_member):
        return "O bot não possui permissão para gerenciar tags de membros. Ative can_manage_tags, ou a permissão equivalente de fixar mensagens quando o Telegram usar esse fallback."

    target_member = await message.bot.get_chat_member(chat_id, target_user_id)
    target_status = getattr(target_member, "status", None)
    if target_status not in REGULAR_TAGGABLE_STATUSES:
        return (
            "A tag só pode ser aplicada a membro comum/restrito. "
            f"O alvo está com status {target_status}, então o Telegram recusará a operação."
        )
    return None


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
            "Envie agora apenas o user_id do membro comum.\n"
            "A tag não pode ser aplicada em admin ou criador do grupo."
        )
    await callback.answer()


@router.message(F.text, _is_waiting_member_tag_text)
async def tigrao_member_tag_receive_text(message: Message) -> None:
    # Sprint 7 (T01): guard de expiração — protege ambos os passos
    # (member_tag_user_id e member_tag_value) do fluxo de tag.
    if consume_if_expired():
        await message.answer(
            error_text(
                "Sessão expirada",
                "O fluxo de tag de membro expirou (15 min).",
                "Use /tigrao para recomeçar.",
            )
        )
        return

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
        touch_session()  # Sprint 7 (T01-fix): refresh updated_at em transition
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

        chat_id = int(session.selected_chat_id)
        target_id = int(target_user_id)
        try:
            validation_error = await _validate_member_tag_request(message, chat_id, target_id)
            if validation_error:
                log_action(chat_id=chat_id, action="member_tag", target_user_id=target_id, status="error", error_type="MemberTagValidationError", error_message=validation_error)
                clear_action()
                await message.answer(
                    error_text(
                        "Tag não aplicada",
                        validation_error,
                        "Use um user_id de membro comum do grupo e confirme as permissões do bot.",
                    ),
                    reply_markup=customize_keyboard(),
                )
                return

            await set_member_tag(message.bot, chat_id, target_id, tag)
            log_action(chat_id=chat_id, action="member_tag", target_user_id=target_id, status="success")
            clear_action()
            detail = "Tag removida" if tag == "" else f"Tag aplicada: {tag}"
            await message.answer(
                success_text("Tag de membro atualizada", f"Grupo: {session.selected_chat_id}\nUsuário: {target_user_id}\n{detail}"),
                reply_markup=customize_keyboard(),
            )
        except Exception as exc:
            log_action(
                chat_id=chat_id,
                action="member_tag",
                target_user_id=target_id,
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            clear_action()
            correction = "Confira se o grupo suporta tags, se o bot possui can_manage_tags e se o user_id pertence ao grupo como membro comum. Tags não se aplicam a admin/criador."
            if "CHAT_CREATOR_REQUIRED" in str(exc):
                correction = "O Telegram recusou a operação porque esse alvo/ação exige criador ou não é um membro comum tagueável. Use um membro comum ou ajuste manualmente no Telegram."
            await message.answer(
                error_text(
                    "Falha ao alterar tag",
                    f"{type(exc).__name__}: {exc}",
                    correction,
                ),
                reply_markup=customize_keyboard(),
            )
