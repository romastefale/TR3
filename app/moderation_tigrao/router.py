from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import (
    approve_join_request,
    ban_user,
    copy_message,
    create_approval_link,
    create_direct_link,
    delete_message,
    mute_user,
    reset_entry,
    unban_user,
    unmute_user,
)
from app.moderation_tigrao.keyboards import (
    confirm_keyboard,
    ddx_keyboard,
    groups_keyboard,
    home_keyboard,
    links_keyboard,
    logs_keyboard,
    messages_keyboard,
    user_actions_keyboard,
)
from app.moderation_tigrao.parsers import parse_chat_id, parse_duration, parse_message_link, parse_user_id
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.state import clear_action, get_session, set_action, set_selected_group
from app.moderation_tigrao.storage import list_groups, list_logs, log_action, remember_group
from app.moderation_tigrao.texts import error_text, home_text, success_text

router = Router(name="moderation_tigrao")

ACTION_LABELS = {
    "ban": "Banir usuário",
    "unban": "Desbanir usuário",
    "mute": "Mutar usuário",
    "unmute": "Desmutar usuário",
    "approve": "Aprovar entrada",
    "reset": "Resetar entrada",
}
SIMPLE_EXECUTABLE_ACTIONS = {"ban", "unban", "unmute", "approve", "reset"}
TEXT_WAITING_STATES = {"chat_id", "outbound_text", "message_link", "user_id", "duration"}


def _section_text(title: str, detail: str) -> str:
    session = get_session()
    selected = ""
    if session.selected_chat_id:
        selected = f"\n\nGrupo selecionado: {session.selected_group_title or session.selected_chat_id} ({session.selected_chat_id})"
    return f"Tigrão — {title}\n\n{detail}{selected}\n\nEscolha uma opção pelos botões abaixo."


def _confirm_text() -> str:
    session = get_session()
    action_label = ACTION_LABELS.get(session.selected_action or "", session.selected_action or "ação")
    target_user_id = session.payload.get("target_user_id")
    duration_label = session.payload.get("duration_label")
    duration_line = f"Duração: {duration_label}\n" if duration_label else ""
    return (
        "Tigrão — confirmar ação\n\n"
        f"Grupo: {session.selected_chat_id}\n"
        f"Ação: {action_label}\n"
        f"Usuário: {target_user_id}\n"
        f"{duration_line}\n"
        "Confirme para prosseguir ou cancele para abandonar."
    )


def _execution_text(action: str, chat_id: int | str, target_user_id: int | str, payload: dict) -> str:
    action_label = ACTION_LABELS.get(action, action)
    duration_line = f"\nDuração: {payload.get('duration_label')}" if payload.get("duration_label") else ""
    return (
        "Tigrão — executando ação\n\n"
        f"Grupo: {chat_id}\n"
        f"Ação: {action_label}\n"
        f"Usuário: {target_user_id}"
        f"{duration_line}\n\n"
        "Aguarde o retorno de conclusão ou erro."
    )


def _logs_text() -> str:
    rows = list_logs(10)
    if not rows:
        return "Tigrão — logs\n\nNenhum registro encontrado."
    lines = ["Tigrão — logs", "", "Últimos registros:"]
    for row in rows:
        status = row.get("status") or "-"
        action = row.get("action") or "-"
        chat_id = row.get("chat_id") or "-"
        target = row.get("target_user_id") or "-"
        created_at = row.get("created_at") or "-"
        error_type = row.get("error_type")
        line = f"#{row.get('id')} | {status} | {action} | grupo {chat_id} | alvo {target} | {created_at}"
        if error_type:
            line += f" | erro {error_type}"
        lines.append(line)
    return "\n".join(lines)


async def _edit_private_panel(callback: CallbackQuery, text: str, reply_markup) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()


def _need_group_text() -> str:
    return error_text(
        "Nenhum grupo selecionado",
        "Você precisa escolher o grupo antes de usar esta ação.",
        "Toque em Escolher grupo e selecione ou digite o chat_id.",
    )


def _is_owner_waiting_text(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for in TEXT_WAITING_STATES


def _is_owner_waiting_media(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for == "outbound_media"


async def _execute_simple_action(bot, action: str, chat_id: int, user_id: int, payload: dict) -> str | None:
    if action == "ban":
        await ban_user(bot, chat_id, user_id)
        return None
    if action == "unban":
        await unban_user(bot, chat_id, user_id)
        return None
    if action == "unmute":
        await unmute_user(bot, chat_id, user_id)
        return None
    if action == "mute":
        await mute_user(bot, chat_id, user_id, payload["duration"])
        return None
    if action == "approve":
        await approve_join_request(bot, chat_id, user_id)
        return None
    if action == "reset":
        return await reset_entry(bot, chat_id, user_id)
    raise ValueError(f"ação ainda não executável: {action}")


@router.message(Command("tigrao"))
async def tigrao_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    await message.answer(home_text(), reply_markup=home_keyboard())


@router.message(F.text, _is_owner_waiting_text)
async def tigrao_private_text(message: Message) -> None:
    session = get_session()

    if session.waiting_for == "chat_id":
        try:
            chat_id = parse_chat_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Chat ID inválido", str(exc), "Envie apenas o chat_id numérico, com ou sem hífen."))
            return
        remember_group(chat_id, str(chat_id))
        set_selected_group(chat_id, str(chat_id))
        await message.answer(success_text("Grupo selecionado", f"Grupo: {chat_id}"), reply_markup=home_keyboard())
        return

    if session.waiting_for == "outbound_text":
        if not session.selected_chat_id:
            await message.answer(_need_group_text(), reply_markup=home_keyboard())
            return
        text_to_send = message.text or ""
        if not text_to_send.strip():
            await message.answer(error_text("Texto vazio", "Não há texto para enviar.", "Envie uma mensagem de texto válida."), reply_markup=messages_keyboard())
            return
        action = "send_text_pin" if session.payload.get("pin") else "send_text"
        try:
            sent = await message.bot.send_message(chat_id=int(session.selected_chat_id), text=text_to_send)
            if session.payload.get("pin"):
                await message.bot.pin_chat_message(chat_id=int(session.selected_chat_id), message_id=sent.message_id, disable_notification=True)
            log_action(chat_id=int(session.selected_chat_id), action=action, status="success")
            clear_action()
            await message.answer(
                success_text(
                    "Mensagem enviada" if action == "send_text" else "Mensagem enviada e fixada",
                    f"Grupo: {session.selected_chat_id}\nMensagem: {sent.message_id}",
                ),
                reply_markup=messages_keyboard(),
            )
        except TelegramForbiddenError as exc:
            log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
            await message.answer(error_text("Permissão insuficiente", "O Telegram recusou o envio ou fixação da mensagem.", "Confira se o bot pode enviar e fixar mensagens no grupo."), reply_markup=messages_keyboard())
        except Exception as exc:
            log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
            await message.answer(error_text("Falha ao enviar", f"{type(exc).__name__}: {exc}", "Confira grupo, texto e permissões do bot."), reply_markup=messages_keyboard())
        return

    if session.waiting_for == "message_link":
        try:
            link_chat_id, message_id = parse_message_link(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Link inválido", str(exc), "Envie um link de mensagem do Telegram."))
            return
        try:
            await delete_message(message.bot, link_chat_id, message_id)
            log_action(chat_id=int(link_chat_id) if isinstance(link_chat_id, int) else None, action="delete_by_link", status="success")
            clear_action()
            await message.answer(success_text("Mensagem apagada", f"Origem: {link_chat_id}\nMensagem: {message_id}"), reply_markup=messages_keyboard())
        except TelegramForbiddenError as exc:
            log_action(chat_id=int(link_chat_id) if isinstance(link_chat_id, int) else None, action="delete_by_link", status="error", error_type=type(exc).__name__, error_message=str(exc))
            await message.answer(error_text("Permissão insuficiente", "O Telegram recusou a remoção da mensagem.", "Confira se o bot é administrador e pode apagar mensagens."), reply_markup=messages_keyboard())
        except Exception as exc:
            log_action(chat_id=int(link_chat_id) if isinstance(link_chat_id, int) else None, action="delete_by_link", status="error", error_type=type(exc).__name__, error_message=str(exc))
            await message.answer(error_text("Falha ao apagar", f"{type(exc).__name__}: {exc}", "Confira o link e as permissões do bot."), reply_markup=messages_keyboard())
        return

    if session.waiting_for == "user_id":
        try:
            user_id = parse_user_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("User ID inválido", str(exc), "Envie apenas o user_id numérico, sem hífen."))
            return
        session.payload["target_user_id"] = user_id
        if session.selected_action == "mute":
            session.waiting_for = "duration"
            await message.answer(
                "Tigrão — duração do mute\n\n"
                f"Grupo: {session.selected_chat_id}\n"
                f"Usuário: {user_id}\n\n"
                "Envie a duração. Exemplos:\n"
                "10m, 2h, 3d ou i para indefinido."
            )
            return
        session.waiting_for = None
        await message.answer(_confirm_text(), reply_markup=confirm_keyboard())
        return

    if session.waiting_for == "duration":
        try:
            duration = parse_duration(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Duração inválida", str(exc), "Use valores como 10m, 2h, 3d ou i."))
            return
        if duration == "desmutar":
            await message.answer(error_text("Duração inválida", "x é usado para desmutar, não para mutar.", "Use 10m, 2h, 3d ou i."))
            return
        session.payload["duration"] = duration
        session.payload["duration_label"] = str(message.text or "").strip()
        session.waiting_for = None
        await message.answer(_confirm_text(), reply_markup=confirm_keyboard())
        return


@router.message(F.photo | F.video | F.document | F.animation | F.sticker | F.audio | F.voice | F.video_note, _is_owner_waiting_media)
async def tigrao_private_media(message: Message) -> None:
    session = get_session()
    if not session.selected_chat_id:
        await message.answer(_need_group_text(), reply_markup=home_keyboard())
        return
    try:
        copied_id = await copy_message(
            message.bot,
            target_chat_id=int(session.selected_chat_id),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        log_action(chat_id=int(session.selected_chat_id), action="send_media", status="success")
        clear_action()
        await message.answer(success_text("Mídia enviada", f"Grupo: {session.selected_chat_id}\nMensagem: {copied_id}"), reply_markup=messages_keyboard())
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(session.selected_chat_id), action="send_media", status="error", error_type=type(exc).__name__, error_message=str(exc))
        await message.answer(error_text("Permissão insuficiente", "O Telegram recusou o envio da mídia.", "Confira se o bot pode enviar mídia no grupo."), reply_markup=messages_keyboard())
    except Exception as exc:
        log_action(chat_id=int(session.selected_chat_id), action="send_media", status="error", error_type=type(exc).__name__, error_message=str(exc))
        await message.answer(error_text("Falha ao enviar mídia", f"{type(exc).__name__}: {exc}", "Confira grupo, mídia e permissões do bot."), reply_markup=messages_keyboard())


@router.callback_query(F.data == "tigrao:home")
async def tigrao_home_callback(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, home_text(), home_keyboard())


@router.callback_query(F.data == "tigrao:groups")
async def tigrao_groups(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "escolher grupo",
            "Selecione um grupo já conhecido ou use a opção para digitar o chat_id.",
        ),
        groups_keyboard(list_groups()),
    )


@router.callback_query(F.data == "tigrao:group:manual")
async def tigrao_group_manual(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    set_action("select_group", waiting_for="chat_id")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — escolher grupo\n\n"
            "Envie agora o chat_id numérico do grupo.\n"
            "Pode ser com ou sem hífen.\n\n"
            "Exemplo:\n"
            "-1001234567890"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:group:"))
async def tigrao_group_select(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.data == "tigrao:group:manual":
        return
    try:
        chat_id = parse_chat_id(callback.data.rsplit(":", 1)[-1])
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    set_selected_group(chat_id, str(chat_id))
    if callback.message:
        await callback.message.edit_text(
            success_text("Grupo selecionado", f"Grupo: {chat_id}"),
            reply_markup=home_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:user_actions")
async def tigrao_user_actions(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text("ações de usuário", "Ações que exigem grupo selecionado e, em geral, apenas o user_id do alvo."),
        user_actions_keyboard(),
    )


@router.callback_query(F.data.startswith("tigrao:action:"))
async def tigrao_prepare_user_action(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    action = (callback.data or "").rsplit(":", 1)[-1]
    if action not in ACTION_LABELS:
        await callback.answer("Ação inválida.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action(action, waiting_for="user_id")
    if callback.message:
        await callback.message.edit_text(
            f"Tigrão — {ACTION_LABELS[action]}\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora apenas o user_id do alvo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:confirm")
async def tigrao_confirm(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    chat_id = session.selected_chat_id
    action = session.selected_action
    target_user_id = session.payload.get("target_user_id")
    if not chat_id or not action or not target_user_id:
        if callback.message:
            await callback.message.edit_text(
                error_text("Confirmação inválida", "Faltam dados para confirmar a ação.", "Volte ao painel e recomece o fluxo."),
                reply_markup=home_keyboard(),
            )
        await callback.answer()
        return
    if action == "mute" and "duration" not in session.payload:
        if callback.message:
            await callback.message.edit_text(
                error_text("Duração ausente", "Falta informar a duração do mute.", "Recomece a ação de mutar usuário."),
                reply_markup=user_actions_keyboard(),
            )
        await callback.answer()
        return
    if action not in SIMPLE_EXECUTABLE_ACTIONS and action != "mute":
        if callback.message:
            await callback.message.edit_text(
                error_text("Ação ainda não habilitada", f"A ação {ACTION_LABELS.get(action, action)} será ligada em etapa separada.", "Use uma ação já habilitada."),
                reply_markup=user_actions_keyboard(),
            )
        await callback.answer()
        return

    await callback.answer("Executando ação...")
    if callback.message:
        await callback.message.edit_text(
            _execution_text(action, chat_id, target_user_id, session.payload),
            reply_markup=None,
        )

    try:
        extra = await _execute_simple_action(callback.bot, action, int(chat_id), int(target_user_id), session.payload)
        log_action(chat_id=int(chat_id), action=action, target_user_id=int(target_user_id), status="success")
        details = f"Grupo: {chat_id}\nAção: {ACTION_LABELS[action]}\nUsuário: {target_user_id}\nStatus: concluído com sucesso"
        if session.payload.get("duration_label"):
            details += f"\nDuração: {session.payload['duration_label']}"
        if extra:
            details += f"\nLink direto: {extra}"
        clear_action()
        if callback.message:
            await callback.message.edit_text(success_text("Ação executada", details), reply_markup=user_actions_keyboard())
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(chat_id), action=action, target_user_id=int(target_user_id), status="error", error_type=type(exc).__name__, error_message=str(exc))
        if callback.message:
            await callback.message.edit_text(
                error_text(
                    "Permissão insuficiente",
                    f"O Telegram recusou a ação. Erro: {type(exc).__name__}: {exc}",
                    "Confira se o bot é administrador do grupo e tem a permissão necessária.",
                ),
                reply_markup=user_actions_keyboard(),
            )
    except Exception as exc:
        log_action(chat_id=int(chat_id), action=action, target_user_id=int(target_user_id), status="error", error_type=type(exc).__name__, error_message=str(exc))
        if callback.message:
            await callback.message.edit_text(
                error_text("Falha ao executar", f"{type(exc).__name__}: {exc}", "Confira grupo, user_id e permissões do bot."),
                reply_markup=user_actions_keyboard(),
            )


@router.callback_query(F.data == "tigrao:cancel")
async def tigrao_cancel(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    clear_action()
    if callback.message:
        await callback.message.edit_text("Tigrão — ação cancelada.", reply_markup=home_keyboard())
    await callback.answer()


@router.callback_query(F.data == "tigrao:links")
async def tigrao_links(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("links", "Geração de links de entrada para o grupo selecionado."), links_keyboard())


@router.callback_query(F.data.startswith("tigrao:link:"))
async def tigrao_create_link(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    link_type = (callback.data or "").rsplit(":", 1)[-1]
    action = "link_direct" if link_type == "direct" else "link_approval"
    try:
        if link_type == "direct":
            invite_link = await create_direct_link(callback.bot, int(session.selected_chat_id))
            title = "Link direto gerado"
        elif link_type == "approval":
            invite_link = await create_approval_link(callback.bot, int(session.selected_chat_id))
            title = "Link com aprovação gerado"
        else:
            await callback.answer("Tipo de link inválido.", show_alert=True)
            return
        log_action(chat_id=int(session.selected_chat_id), action=action, status="success")
        if callback.message:
            await callback.message.edit_text(
                success_text(title, f"Grupo: {session.selected_chat_id}\nLink: {invite_link}"),
                reply_markup=links_keyboard(),
            )
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
        if callback.message:
            await callback.message.edit_text(
                error_text("Permissão insuficiente", "O Telegram recusou a criação do link.", "Confira se o bot é administrador do grupo e pode criar links de convite."),
                reply_markup=links_keyboard(),
            )
    except Exception as exc:
        log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
        if callback.message:
            await callback.message.edit_text(
                error_text("Falha ao criar link", f"{type(exc).__name__}: {exc}", "Confira grupo e permissões do bot."),
                reply_markup=links_keyboard(),
            )
    await callback.answer()


@router.callback_query(F.data == "tigrao:messages")
async def tigrao_messages(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("mensagens", "Envio, fixação ou remoção de mensagens usando apenas o privado do dono."), messages_keyboard())


@router.callback_query(F.data == "tigrao:message:send")
async def tigrao_send_text(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action("send_text", waiting_for="outbound_text", pin=False)
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — enviar mensagem\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora o texto que será publicado no grupo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:message:pin")
async def tigrao_send_text_pin(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action("send_text_pin", waiting_for="outbound_text", pin=True)
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — enviar e fixar\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora o texto que será publicado e fixado no grupo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:message:media")
async def tigrao_send_media(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action("send_media", waiting_for="outbound_media")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — enviar mídia\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora a foto, vídeo, documento, sticker ou outra mídia que será copiada para o grupo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:message:delete_link")
async def tigrao_delete_by_link(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    set_action("delete_by_link", waiting_for="message_link")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — apagar por link\n\n"
            "Envie agora o link da mensagem que deve ser apagada.\n\n"
            "Exemplos:\n"
            "https://t.me/c/1234567890/55\n"
            "https://t.me/nomedogrupo/55"
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:ddx")
async def tigrao_ddx(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("filtros DDX", "Configuração futura dos filtros de remoção automática por texto."), ddx_keyboard())


@router.callback_query(F.data.in_({"tigrao:logs", "tigrao:logs:refresh"}))
async def tigrao_logs(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _logs_text(), logs_keyboard())


@router.callback_query(F.data == "tigrao:close")
async def tigrao_close(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão — painel fechado.")
    await callback.answer()
