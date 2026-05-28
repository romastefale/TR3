from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.moderation_tigrao.actions import (
    _with_telegram_retry,
    approve_join_request,
    ban_user,
    copy_message,
    create_approval_link,
    create_direct_link,
    delete_all_message_reactions,
    delete_message,
    delete_message_reaction,
    mute_reactions,
    mute_user,
    reset_entry,
    resolve_user_target,
    set_group_description,
    set_group_title,
    unban_user,
    unmute_user,
)
from app.moderation_tigrao.keyboards import (
    confirm_keyboard,
    customize_keyboard,
    ddx_keyboard,
    groups_keyboard,
    home_keyboard,
    link_result_keyboard,
    links_keyboard,
    logs_keyboard,
    messages_keyboard,
    reactions_mod_keyboard,
    rmod_confirm_keyboard,
    rmod_duration_keyboard,
    rmod_reactors_picker_keyboard,
    user_actions_keyboard,
)
from app.services.reaction_audit import reaction_audit_service
import secrets as _secrets


def _new_picker_nonce() -> str:
    """Sprint X3: token curto pra invalidar pickers antigos."""
    return _secrets.token_urlsafe(6)
from app.moderation_tigrao.parsers import parse_chat_id, parse_duration, parse_message_link, parse_user_id
from app.moderation_tigrao.permissions import (
    OWNER_ID,
    is_moderator_user,
    is_owner_callback,
    is_owner_private_message,
)
from app.moderation_tigrao.state import (
    clear_action,
    consume_if_expired,
    get_session,
    set_action,
    set_selected_group,
    touch_session,
)
from app.moderation_tigrao.storage import list_groups, list_logs, log_action, remember_group
from app.moderation_tigrao.texts import error_text, home_text, success_text

logger = logging.getLogger(__name__)

router = Router(name="moderation_tigrao")


async def _validate_group_access(bot, chat_id: int) -> tuple[str | None, str | None]:
    """Sprint 7 (T03-fix2, architect): valida proativamente se o bot tem
    permissão no grupo. Reusado pelos 2 caminhos de seleção (botão E manual).

    Returns:
        (blocking_error_text, warning_suffix)
        - blocking_error_text != None: caller envia erro e bloqueia seleção.
        - warning_suffix != None: caller mostra success com aviso anexado.
        - ambos None: tudo OK.
    """
    try:
        bot_me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, bot_me.id)
        status = getattr(bot_member, "status", None)
        if status not in {"administrator", "creator"}:
            return (
                error_text(
                    "Bot sem permissão",
                    f"O bot não é administrador no grupo {chat_id} (status: {status}).",
                    "Escolha outro grupo ou promova o bot a admin antes de prosseguir.",
                ),
                None,
            )
    except TelegramForbiddenError:
        return (
            error_text(
                "Bot removido do grupo",
                f"O bot não está mais no grupo {chat_id}.",
                "Escolha outro grupo ou readicione o bot antes de prosseguir.",
            ),
            None,
        )
    except TelegramBadRequest as exc:
        # Determinístico: fail-closed em vez de deixar o owner descobrir tarde.
        return (
            error_text(
                "Grupo inválido",
                f"O Telegram recusou consultar o grupo {chat_id}: {type(exc).__name__}.",
                "Confira o chat_id e tente outro grupo.",
            ),
            None,
        )
    except Exception as exc:
        # Transitório (rede/5xx): fail-open com aviso pro owner.
        logger.warning(
            "TIGRAO_GROUP_ACCESS_CHECK_FAILED | chat_id=%s | %s: %s",
            chat_id, type(exc).__name__, exc,
        )
        return (None, " (permissão não verificada — siga com cautela)")
    return (None, None)

ACTION_LABELS = {
    "ban": "Banir usuário",
    "unban": "Desbanir usuário",
    "mute": "Mutar usuário",
    "unmute": "Desmutar usuário",
    "approve": "Aprovar entrada",
    "reset": "Resetar entrada",
    "rmod_del_user_msg": "Apagar reaction de 1 pessoa (msg)",
    "rmod_del_user_chat": "Apagar reactions de 1 pessoa (grupo)",
    "rmod_del_all_msg": "Apagar TODAS reactions desta msg",
    "rmod_mute_react": "Silenciar reactor",
}
SIMPLE_EXECUTABLE_ACTIONS = {"ban", "unban", "unmute", "approve", "reset"}
TEXT_WAITING_STATES = {
    "chat_id", "outbound_text", "message_link", "user_id", "duration",
    "customize_title", "customize_bio",
    "rmod_link", "rmod_user",
}


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


def _rmod_confirm_text() -> str:
    session = get_session()
    action = session.selected_action or ""
    action_label = ACTION_LABELS.get(action, action)
    p = session.payload
    lines = ["Tigrão — confirmar moderação de reactions", "", f"Ação: {action_label}"]
    if action == "rmod_del_user_msg":
        lines.append(f"Mensagem: {p.get('link_chat_id')} / {p.get('link_msg_id')}")
        lines.append(f"Alvo: {p.get('target_label')} ({p.get('target_user_id')})")
        lines.append("")
        lines.append("Vai apagar a reaction dessa pessoa NESSA mensagem (Telegram permite 1 reaction por user/msg).")
    elif action == "rmod_del_user_chat":
        lines.append(f"Grupo: {session.selected_chat_id}")
        lines.append(f"Alvo: {p.get('target_label')} ({p.get('target_user_id')})")
        lines.append("")
        lines.append("Vai apagar até 10000 reactions RECENTES dessa pessoa no GRUPO INTEIRO (todas mensagens).")
    elif action == "rmod_del_all_msg":
        lines.append(f"Mensagem: {p.get('link_chat_id')} / {p.get('link_msg_id')}")
        lines.append("")
        lines.append("Atenção: vai remover TODAS as reactions desta mensagem, inclusive as do próprio bot.")
    elif action == "rmod_mute_react":
        lines.append(f"Grupo: {session.selected_chat_id}")
        lines.append(f"Alvo: {p.get('target_label')} ({p.get('target_user_id')})")
        lines.append(f"Duração: {p.get('duration_label')}")
        lines.append("")
        lines.append("Apenas a permissão de reagir será alterada. Outras permissões serão preservadas.")
    lines.append("")
    lines.append("Confirme para prosseguir ou cancele para abandonar.")
    return "\n".join(lines)


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
    # Sprint 7 (T01): se o fluxo waiting expirou (>15min sem atividade),
    # limpa o estado e avisa em vez de processar input antigo.
    if consume_if_expired():
        await message.answer(
            error_text(
                "Sessão expirada",
                "O fluxo anterior expirou por inatividade (15 min).",
                "Use /tigrao para abrir o painel novamente.",
            )
        )
        return

    session = get_session()

    if session.waiting_for == "chat_id":
        try:
            chat_id = parse_chat_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Chat ID inválido", str(exc), "Envie apenas o chat_id numérico, com ou sem hífen."))
            return
        # Sprint 7 (T03-fix2, architect): mesmo check proativo do caminho
        # via botão. Sem isso o caminho manual aceitava qualquer chat_id e
        # só falhava na primeira ação.
        blocking, warn = await _validate_group_access(message.bot, chat_id)
        if blocking:
            await message.answer(blocking, reply_markup=home_keyboard())
            return
        remember_group(chat_id, str(chat_id))
        set_selected_group(chat_id, str(chat_id))
        await message.answer(
            success_text("Grupo selecionado", f"Grupo: {chat_id}{warn or ''}"),
            reply_markup=home_keyboard(),
        )
        return

    if session.waiting_for == "customize_title":
        if not session.selected_chat_id:
            await message.answer(_need_group_text(), reply_markup=home_keyboard())
            return
        new_title = (message.text or "").strip()
        if not new_title:
            await message.answer(error_text("Nome vazio", "Não há nome para aplicar.", "Envie um nome válido para o grupo."), reply_markup=customize_keyboard())
            return
        try:
            await set_group_title(message.bot, int(session.selected_chat_id), new_title)
            log_action(chat_id=int(session.selected_chat_id), action="customize_title", status="success")
            clear_action()
            await message.answer(success_text("Nome alterado", f"Grupo: {session.selected_chat_id}\nNovo nome: {new_title}"), reply_markup=customize_keyboard())
        except TelegramForbiddenError as exc:
            log_action(chat_id=int(session.selected_chat_id), action="customize_title", status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Permissão insuficiente", f"O Telegram recusou a alteração do nome. Erro: {type(exc).__name__}: {exc}", "Confira se o bot é administrador e possui permissão para alterar informações do grupo."), reply_markup=customize_keyboard())
        except Exception as exc:
            log_action(chat_id=int(session.selected_chat_id), action="customize_title", status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Falha ao alterar nome", f"{type(exc).__name__}: {exc}", "Confira o nome, grupo e permissões do bot."), reply_markup=customize_keyboard())
        return

    if session.waiting_for == "customize_bio":
        if not session.selected_chat_id:
            await message.answer(_need_group_text(), reply_markup=home_keyboard())
            return
        new_bio = (message.text or "").strip()
        try:
            await set_group_description(message.bot, int(session.selected_chat_id), new_bio)
            log_action(chat_id=int(session.selected_chat_id), action="customize_bio", status="success")
            clear_action()
            await message.answer(success_text("Bio alterada", f"Grupo: {session.selected_chat_id}\nCaracteres: {len(new_bio)}"), reply_markup=customize_keyboard())
        except TelegramForbiddenError as exc:
            log_action(chat_id=int(session.selected_chat_id), action="customize_bio", status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Permissão insuficiente", f"O Telegram recusou a alteração da bio. Erro: {type(exc).__name__}: {exc}", "Confira se o bot é administrador e possui permissão para alterar informações do grupo."), reply_markup=customize_keyboard())
        except Exception as exc:
            log_action(chat_id=int(session.selected_chat_id), action="customize_bio", status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Falha ao alterar bio", f"{type(exc).__name__}: {exc}", "Confira a bio, grupo e permissões do bot."), reply_markup=customize_keyboard())
        return

    if session.waiting_for == "outbound_text":
        if not session.selected_chat_id:
            await message.answer(_need_group_text(), reply_markup=home_keyboard())
            return
        text_to_send = message.text or ""
        if not text_to_send.strip():
            await message.answer(error_text("Texto vazio", "Não há texto para enviar.", "Envie uma mensagem de texto válida."), reply_markup=customize_keyboard())
            return
        action = "send_text_pin" if session.payload.get("pin") else "send_text"
        try:
            # Sprint 7 (T04-fix2, architect): send + pin via retry wrapper
            # pra fechar coverage. Antes ambos bypassavam _with_telegram_retry.
            target = int(session.selected_chat_id)
            sent = await _with_telegram_retry(
                lambda: message.bot.send_message(chat_id=target, text=text_to_send),
                label="send_message_outbound_text",
            )
            if session.payload.get("pin"):
                await _with_telegram_retry(
                    lambda: message.bot.pin_chat_message(
                        chat_id=target,
                        message_id=sent.message_id,
                        disable_notification=True,
                    ),
                    label="pin_chat_message_outbound_text",
                )
            log_action(chat_id=int(session.selected_chat_id), action=action, status="success")
            clear_action()
            await message.answer(
                success_text(
                    "Mensagem enviada" if action == "send_text" else "Mensagem enviada e fixada",
                    f"Grupo: {session.selected_chat_id}\nMensagem: {sent.message_id}",
                ),
                reply_markup=customize_keyboard(),
            )
        except TelegramForbiddenError as exc:
            log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Permissão insuficiente", "O Telegram recusou o envio ou fixação da mensagem.", "Confira se o bot pode enviar e fixar mensagens no grupo."), reply_markup=customize_keyboard())
        except Exception as exc:
            log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Falha ao enviar", f"{type(exc).__name__}: {exc}", "Confira grupo, texto e permissões do bot."), reply_markup=customize_keyboard())
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
            clear_action()
            await message.answer(error_text("Permissão insuficiente", "O Telegram recusou a remoção da mensagem.", "Confira se o bot é administrador e pode apagar mensagens."), reply_markup=messages_keyboard())
        except Exception as exc:
            log_action(chat_id=int(link_chat_id) if isinstance(link_chat_id, int) else None, action="delete_by_link", status="error", error_type=type(exc).__name__, error_message=str(exc))
            clear_action()
            await message.answer(error_text("Falha ao apagar", f"{type(exc).__name__}: {exc}", "Confira o link e as permissões do bot."), reply_markup=messages_keyboard())
        return

    if session.waiting_for == "user_id":
        try:
            user_id = parse_user_id(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("User ID inválido", str(exc), "Envie apenas o user_id numérico, sem hífen."))
            return
        session.payload["target_user_id"] = user_id
        # Sprint 7 (T01-fix): garante refresh quando NÃO é mute (cai no else)
        if session.selected_action == "mute":
            session.waiting_for = "duration"
            touch_session()  # Sprint 7 (T01-fix): refresh updated_at em transition
            await message.answer(
                "Tigrão — duração do mute\n\n"
                f"Grupo: {session.selected_chat_id}\n"
                f"Usuário: {user_id}\n\n"
                "Envie a duração. Exemplos:\n"
                "10m, 2h, 3d ou i para indefinido."
            )
            return
        session.waiting_for = None
        touch_session()  # Sprint 7 (T01-fix): refresh updated_at em transition
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
        touch_session()  # Sprint 7 (T01-fix): refresh updated_at em transition
        await message.answer(_confirm_text(), reply_markup=confirm_keyboard())
        return

    # Sprint X1 (TR3): Reaction Moderation — handlers de texto
    if session.waiting_for == "rmod_link":
        try:
            link_chat_id, link_msg_id = parse_message_link(message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Link inválido", str(exc), "Cole um link de mensagem do Telegram (t.me/grupo/123 ou t.me/c/123/456)."))
            return
        session.payload["link_chat_id"] = link_chat_id
        session.payload["link_msg_id"] = link_msg_id
        if session.selected_action == "rmod_del_user_msg":
            # Sprint X3: em vez de pedir @username (que falha quando o
            # bot nunca interagiu com o user), busca a lista de quem
            # reagiu nessa msg (últimas 24h) e mostra como botões.
            # Se o link aponta pra outro chat e a lista vier vazia
            # (msg antiga ou bot não viu reactions), cai no fallback
            # de texto manual.
            reactors = []
            try:
                if isinstance(link_chat_id, int):
                    reactors = reaction_audit_service.list_message_reactors(
                        chat_id=link_chat_id, message_id=int(link_msg_id),
                    )
            except Exception:
                logger.exception("RMOD_PICKER_QUERY_FAILED chat=%s msg=%s", link_chat_id, link_msg_id)
            if reactors:
                nonce = _new_picker_nonce()
                session.payload["reactors"] = reactors
                session.payload["picker_nonce"] = nonce
                session.waiting_for = None
                touch_session()
                await message.answer(
                    "Tigrão — escolha o reactor\n\n"
                    f"Mensagem: {link_chat_id} / {link_msg_id}\n"
                    f"Reactors detectados (últimas 24h): {len(reactors)}\n\n"
                    "Toque na pessoa cuja reaction deve ser apagada.",
                    reply_markup=rmod_reactors_picker_keyboard(reactors, nonce),
                )
                return
            # Fallback: sem dados no audit → pede manual.
            session.waiting_for = "rmod_user"
            touch_session()
            await message.answer(
                "Tigrão — apagar reaction de 1 pessoa\n\n"
                f"Mensagem: {link_chat_id} / {link_msg_id}\n\n"
                "Não encontrei reactions rastreadas dessa msg nas últimas 24h "
                "(o bot só vê reactions feitas após estar admin com message_reaction ligado).\n\n"
                "Envie agora o user_id numérico OU @username da pessoa."
            )
            return
        # rmod_del_all_msg → direto pra confirmação
        session.waiting_for = None
        touch_session()
        await message.answer(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
        return

    if session.waiting_for == "rmod_user":
        try:
            target_user_id, target_label = await resolve_user_target(message.bot, message.text or "")
        except ValueError as exc:
            await message.answer(error_text("Entrada inválida", str(exc), "Envie user_id numérico ou @username."))
            return
        except RuntimeError as exc:
            await message.answer(error_text("Não foi possível resolver", str(exc), "Confira o @username ou use o user_id numérico."))
            return
        # Hard-block moderadores: nenhum moderador autorizado (owner ou 2º
        # co-moderador) pode ser alvo de moderação de reactions/mute.
        if is_moderator_user(target_user_id):
            await message.answer(error_text("Operação bloqueada", "Você não pode moderar um moderador.", "Cancele e escolha outro alvo."))
            return
        session.payload["target_user_id"] = target_user_id
        session.payload["target_label"] = target_label
        # Discrimina próximo passo por ação selecionada:
        # - mute_react → escolher duração (teclado)
        # - del_user_msg / del_user_chat → direto pra confirmação
        if session.selected_action == "rmod_mute_react":
            session.waiting_for = None
            touch_session()
            await message.answer(
                "Tigrão — duração do silêncio de reactions\n\n"
                f"Grupo: {session.selected_chat_id}\n"
                f"Alvo: {target_label} ({target_user_id})\n\n"
                "Escolha por quanto tempo o alvo ficará sem poder reagir.",
                reply_markup=rmod_duration_keyboard(),
            )
            return
        # del_user_msg ou del_user_chat
        session.waiting_for = None
        touch_session()
        await message.answer(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
        return


@router.message(F.photo | F.video | F.document | F.animation | F.sticker | F.audio | F.voice | F.video_note, _is_owner_waiting_media)
async def tigrao_private_media(message: Message) -> None:
    # Sprint 7 (T01): mesmo guard de expiração do handler de texto.
    if consume_if_expired():
        await message.answer(
            error_text(
                "Sessão expirada",
                "O fluxo anterior expirou por inatividade (15 min).",
                "Use /tigrao para abrir o painel novamente.",
            )
        )
        return
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
        await message.answer(success_text("Mídia enviada", f"Grupo: {session.selected_chat_id}\nMensagem: {copied_id}"), reply_markup=customize_keyboard())
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(session.selected_chat_id), action="send_media", status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        await message.answer(error_text("Permissão insuficiente", "O Telegram recusou o envio da mídia.", "Confira se o bot pode enviar mídia no grupo."), reply_markup=customize_keyboard())
    except Exception as exc:
        log_action(chat_id=int(session.selected_chat_id), action="send_media", status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        await message.answer(error_text("Falha ao enviar mídia", f"{type(exc).__name__}: {exc}", "Confira grupo, mídia e permissões do bot."), reply_markup=customize_keyboard())


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

    # Sprint 7 (T03): check proativo de permissão antes de selecionar.
    # Evita "selecionar → escolher ação → enviar user_id → erro permissão".
    blocking, perm_warning = await _validate_group_access(callback.bot, chat_id)
    if blocking:
        if callback.message:
            await callback.message.edit_text(blocking, reply_markup=home_keyboard())
        await callback.answer()
        return

    set_selected_group(chat_id, str(chat_id))
    if callback.message:
        await callback.message.edit_text(
            success_text("Grupo selecionado", f"Grupo: {chat_id}{perm_warning or ''}"),
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
        clear_action()
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
        clear_action()
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
            # Sprint X5: pós-criação mostra botão "Copiar link" (CopyTextButton, Bot API 10.0).
            await callback.message.edit_text(
                success_text(title, f"Grupo: {session.selected_chat_id}\nLink: {invite_link}"),
                reply_markup=link_result_keyboard(invite_link),
            )
    except TelegramForbiddenError as exc:
        log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                error_text("Permissão insuficiente", "O Telegram recusou a criação do link.", "Confira se o bot é administrador e pode criar links de convite."),
                reply_markup=links_keyboard(),
            )
    except Exception as exc:
        log_action(chat_id=int(session.selected_chat_id), action=action, status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                error_text("Falha ao criar link", f"{type(exc).__name__}: {exc}", "Confira grupo e permissões do bot."),
                reply_markup=links_keyboard(),
            )
    await callback.answer()


@router.callback_query(F.data == "tigrao:messages")
async def tigrao_messages(callback: CallbackQuery) -> None:
    await _edit_private_panel(callback, _section_text("mensagens", "Use esta seção somente para apagar mensagens por link."), messages_keyboard())


@router.callback_query(F.data == "tigrao:customize")
async def tigrao_customize(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "personalização",
            "Envio de conteúdo e alterações de dados do grupo selecionado. Foto e tag serão ligadas por etapas.",
        ),
        customize_keyboard(),
    )


@router.callback_query(F.data == "tigrao:customize:title")
async def tigrao_customize_title(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action("customize_title", waiting_for="customize_title")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — alterar nome\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora o novo nome do grupo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:customize:bio")
async def tigrao_customize_bio(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    set_action("customize_bio", waiting_for="customize_bio")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — alterar bio\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Envie agora a nova bio/descrição do grupo.\n"
            "Para apagar a bio, envie apenas um ponto: ."
        )
    await callback.answer()


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


# Sprint X1 (TR3): Reaction Moderation — callbacks
@router.callback_query(F.data == "tigrao:rmod")
async def tigrao_rmod(callback: CallbackQuery) -> None:
    await _edit_private_panel(
        callback,
        _section_text(
            "moderar reactions",
            "Apague reactions individuais ou todas de uma mensagem, ou silencie quem reagiu.\n"
            "Apagar não precisa de grupo selecionado (o link já tem o chat).\n"
            "Silenciar precisa de grupo selecionado e do user_id ou @username.",
        ),
        reactions_mod_keyboard(),
    )


@router.callback_query(F.data == "tigrao:rmod:del_user_msg")
async def tigrao_rmod_del_user_msg(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    set_action("rmod_del_user_msg", waiting_for="rmod_link")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — apagar reaction de 1 pessoa (msg)\n\n"
            "Cole agora o link da mensagem.\n\n"
            "Exemplos:\n"
            "https://t.me/c/1234567890/55\n"
            "https://t.me/nomedogrupo/55"
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:rmod:del_user_chat")
async def tigrao_rmod_del_user_chat(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    # Sprint X3: tenta mostrar picker de reactors recentes do chat
    # antes de pedir @username. Se nada rastreado, cai no fluxo texto.
    reactors = []
    try:
        reactors = reaction_audit_service.list_chat_recent_reactors(
            chat_id=int(session.selected_chat_id),
        )
    except Exception:
        logger.exception("RMOD_PICKER_CHAT_QUERY_FAILED chat=%s", session.selected_chat_id)
    if reactors:
        nonce = _new_picker_nonce()
        set_action("rmod_del_user_chat", waiting_for=None, reactors=reactors, picker_nonce=nonce)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "Tigrão — apagar reactions de 1 pessoa (grupo inteiro)\n\n"
                    f"Grupo: {session.selected_chat_id}\n"
                    f"Reactors recentes (últimas 24h): {len(reactors)}\n\n"
                    "Toque na pessoa cujas reactions devem ser apagadas no grupo todo.",
                    reply_markup=rmod_reactors_picker_keyboard(reactors, nonce),
                )
            except TelegramBadRequest:
                # Mensagem antiga/não editável → manda nova.
                await callback.message.answer(
                    f"Tigrão — escolha o reactor ({len(reactors)} recentes)",
                    reply_markup=rmod_reactors_picker_keyboard(reactors, nonce),
                )
        await callback.answer()
        return
    set_action("rmod_del_user_chat", waiting_for="rmod_user")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — apagar reactions de 1 pessoa (grupo inteiro)\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Sem reactors rastreados nas últimas 24h.\n"
            "Envie agora o user_id numérico OU @username do alvo.\n"
            "Vai apagar até 10000 reactions RECENTES dessa pessoa em TODAS as mensagens deste grupo."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:rmod:del_all_msg")
async def tigrao_rmod_del_all_msg(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    set_action("rmod_del_all_msg", waiting_for="rmod_link")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — apagar TODAS reactions desta msg\n\n"
            "Cole agora o link da mensagem.\n\n"
            "Atenção: vai tentar remover todas as reactions desta mensagem (incluindo as do próprio bot).\n"
            "Observação: na Bot API atual o escopo por mensagem pode ter mudado — se a chamada falhar, use a opção 'Apagar reactions de 1 pessoa (grupo)' por usuário."
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:rmod:mute_react")
async def tigrao_rmod_mute_react(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if not session.selected_chat_id:
        if callback.message:
            await callback.message.edit_text(_need_group_text(), reply_markup=home_keyboard())
        await callback.answer()
        return
    # Sprint X3: picker antes de pedir @username.
    reactors = []
    try:
        reactors = reaction_audit_service.list_chat_recent_reactors(
            chat_id=int(session.selected_chat_id),
        )
    except Exception:
        logger.exception("RMOD_PICKER_MUTE_QUERY_FAILED chat=%s", session.selected_chat_id)
    if reactors:
        nonce = _new_picker_nonce()
        set_action("rmod_mute_react", waiting_for=None, reactors=reactors, picker_nonce=nonce)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "Tigrão — silenciar reactor\n\n"
                    f"Grupo: {session.selected_chat_id}\n"
                    f"Reactors recentes (últimas 24h): {len(reactors)}\n\n"
                    "Toque na pessoa que vai perder a permissão de reagir.",
                    reply_markup=rmod_reactors_picker_keyboard(reactors, nonce),
                )
            except TelegramBadRequest:
                await callback.message.answer(
                    f"Tigrão — silenciar reactor ({len(reactors)} recentes)",
                    reply_markup=rmod_reactors_picker_keyboard(reactors, nonce),
                )
        await callback.answer()
        return
    set_action("rmod_mute_react", waiting_for="rmod_user")
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — silenciar reactor\n\n"
            f"Grupo: {session.selected_chat_id}\n\n"
            "Sem reactors rastreados nas últimas 24h.\n"
            "Envie agora o user_id numérico OU @username do alvo.\n"
            "Apenas a permissão de reagir será removida; o resto fica preservado."
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:rmod:dur:"))
async def tigrao_rmod_duration(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    if session.selected_action != "rmod_mute_react":
        await callback.answer("Fluxo inválido.", show_alert=True)
        return
    raw = (callback.data or "").rsplit(":", 1)[-1]
    try:
        duration = parse_duration(raw)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    session.payload["duration"] = duration
    session.payload["duration_label"] = raw
    touch_session()
    if callback.message:
        try:
            await callback.message.edit_text(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
        except TelegramBadRequest:
            await callback.message.answer(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:rmod:pick:"))
async def tigrao_rmod_pick(callback: CallbackQuery) -> None:
    """Sprint X3: escolhe reactor a partir do picker.

    callback_data: `tigrao:rmod:pick:<nonce>:<user_id>`
    - Valida nonce vs session.payload['picker_nonce']: clique em
      picker antigo (após owner abrir outro fluxo) é rejeitado.
    - user_id é identidade imutável; lookup na lista de reactors
      só serve pra recuperar o label amigável.
    Avança o fluxo: mute → escolher duração; del_* → confirmação.
    """
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    action = session.selected_action or ""
    if action not in {"rmod_del_user_msg", "rmod_del_user_chat", "rmod_mute_react"}:
        await callback.answer("Fluxo inválido.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # ['tigrao','rmod','pick',nonce,user_id] → exatamente 5 partes
    if len(parts) != 5:
        await callback.answer("Callback malformado. Reabra o fluxo.", show_alert=True)
        return
    nonce, user_id_raw = parts[3], parts[4]
    expected_nonce = session.payload.get("picker_nonce")
    if not expected_nonce or nonce != expected_nonce:
        await callback.answer(
            "Esse picker é de um fluxo antigo. Reabra o menu Moderar Reactions.",
            show_alert=True,
        )
        return
    try:
        target_user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("user_id inválido no callback.", show_alert=True)
        return
    if is_moderator_user(target_user_id):
        await callback.answer("Você não pode moderar um moderador.", show_alert=True)
        return
    reactors = session.payload.get("reactors") or []
    reactor = next((r for r in reactors if int(r.get("user_id", 0)) == target_user_id), None)
    if reactor is None:
        await callback.answer("Seleção fora da lista atual.", show_alert=True)
        return
    target_label = (
        reactor.get("user_name")
        or (f"@{reactor['user_username']}" if reactor.get("user_username") else None)
        or str(target_user_id)
    )
    session.payload["target_user_id"] = target_user_id
    session.payload["target_label"] = target_label
    # Invalida o picker (qualquer clique posterior em outro botão dessa
    # mesma keyboard cai no nonce-mismatch) e libera memória da lista.
    session.payload.pop("reactors", None)
    session.payload.pop("picker_nonce", None)
    touch_session()
    if action == "rmod_mute_react":
        if callback.message:
            try:
                await callback.message.edit_text(
                    "Tigrão — duração do silêncio de reactions\n\n"
                    f"Grupo: {session.selected_chat_id}\n"
                    f"Alvo: {target_label} ({target_user_id})\n\n"
                    "Escolha por quanto tempo o alvo ficará sem poder reagir.",
                    reply_markup=rmod_duration_keyboard(),
                )
            except TelegramBadRequest:
                await callback.message.answer(
                    f"Alvo: {target_label} ({target_user_id}) — escolha a duração:",
                    reply_markup=rmod_duration_keyboard(),
                )
        await callback.answer()
        return
    # del_user_msg ou del_user_chat → confirmação direta
    if callback.message:
        try:
            await callback.message.edit_text(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
        except TelegramBadRequest:
            await callback.message.answer(_rmod_confirm_text(), reply_markup=rmod_confirm_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("tigrao:rmod:manual"))
async def tigrao_rmod_manual(callback: CallbackQuery) -> None:
    """Sprint X3: fallback do picker — pede user_id/@username em texto.

    callback_data: `tigrao:rmod:manual:<nonce>` (nonce bind ao picker
    ativo). Rejeita cliques em pickers antigos pra evitar que um botão
    stale altere o fluxo atual.

    Reaproveita o handler `rmod_user` existente (waiting_for='rmod_user').
    Mantém compat com cenários onde o reactor não está no audit (msg
    antiga, reactions pré-deploy do bot como admin, etc).
    """
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    action = session.selected_action or ""
    if action not in {"rmod_del_user_msg", "rmod_del_user_chat", "rmod_mute_react"}:
        await callback.answer("Fluxo inválido.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # ['tigrao','rmod','manual',nonce] → 4 partes
    if len(parts) != 4:
        await callback.answer("Callback malformado. Reabra o fluxo.", show_alert=True)
        return
    nonce = parts[3]
    expected_nonce = session.payload.get("picker_nonce")
    if not expected_nonce or nonce != expected_nonce:
        await callback.answer(
            "Esse botão é de um fluxo antigo. Reabra o menu Moderar Reactions.",
            show_alert=True,
        )
        return
    session.payload.pop("reactors", None)
    session.payload.pop("picker_nonce", None)
    session.waiting_for = "rmod_user"
    touch_session()
    if callback.message:
        try:
            await callback.message.edit_text(
                "Tigrão — digitar alvo manualmente\n\n"
                f"Grupo: {session.selected_chat_id}\n\n"
                "Envie agora o user_id numérico OU @username do alvo.\n"
                "Dica: @username só resolve se o bot já interagiu com a pessoa antes."
            )
        except TelegramBadRequest:
            await callback.message.answer(
                "Tigrão — envie agora o user_id numérico OU @username do alvo."
            )
    await callback.answer()


@router.callback_query(F.data == "tigrao:rmod:cancel")
async def tigrao_rmod_cancel(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    clear_action()
    if callback.message:
        await callback.message.edit_text(
            "Tigrão — moderação de reactions cancelada.",
            reply_markup=reactions_mod_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "tigrao:rmod:confirm")
async def tigrao_rmod_confirm(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    session = get_session()
    action = session.selected_action or ""
    p = dict(session.payload)
    bot = callback.bot

    if action not in {"rmod_del_user_msg", "rmod_del_user_chat", "rmod_del_all_msg", "rmod_mute_react"}:
        await callback.answer("Fluxo inválido.", show_alert=True)
        return

    await callback.answer("Executando ação...")
    if callback.message:
        await callback.message.edit_text(
            f"Tigrão — executando {ACTION_LABELS.get(action, action)}...",
            reply_markup=None,
        )

    chat_id_for_log: int | None = None
    target_for_log: int | None = None

    try:
        if action == "rmod_del_user_msg":
            link_chat_id = p.get("link_chat_id")
            link_msg_id = p.get("link_msg_id")
            target_user_id = p.get("target_user_id")
            if link_chat_id is None or link_msg_id is None or target_user_id is None:
                raise RuntimeError("dados incompletos no payload")
            chat_id_for_log = link_chat_id if isinstance(link_chat_id, int) else None
            target_for_log = int(target_user_id)
            await delete_message_reaction(bot, link_chat_id, int(link_msg_id), int(target_user_id))
            details = (
                f"Mensagem: {link_chat_id} / {link_msg_id}\n"
                f"Alvo: {p.get('target_label')} ({target_user_id})\n"
                "Reaction da pessoa nessa mensagem removida."
            )
            title = "Reaction removida"

        elif action == "rmod_del_user_chat":
            chat_id = session.selected_chat_id
            target_user_id = p.get("target_user_id")
            if not chat_id or target_user_id is None:
                raise RuntimeError("dados incompletos no payload")
            chat_id_for_log = int(chat_id)
            target_for_log = int(target_user_id)
            await delete_all_message_reactions(
                bot, int(chat_id), user_id=int(target_user_id)
            )
            details = (
                f"Grupo: {chat_id}\n"
                f"Alvo: {p.get('target_label')} ({target_user_id})\n"
                "Até 10000 reactions recentes desta pessoa no grupo foram removidas."
            )
            title = "Reactions da pessoa removidas"

        elif action == "rmod_del_all_msg":
            link_chat_id = p.get("link_chat_id")
            link_msg_id = p.get("link_msg_id")
            if link_chat_id is None or link_msg_id is None:
                raise RuntimeError("dados incompletos no payload")
            chat_id_for_log = link_chat_id if isinstance(link_chat_id, int) else None
            await delete_all_message_reactions(bot, link_chat_id, message_id=int(link_msg_id))
            details = f"Mensagem: {link_chat_id} / {link_msg_id}\nTodas as reactions removidas"
            title = "Todas reactions removidas"

        else:  # rmod_mute_react
            chat_id = session.selected_chat_id
            target_user_id = p.get("target_user_id")
            duration = p.get("duration")
            if not chat_id or target_user_id is None or duration is None:
                raise RuntimeError("dados incompletos no payload")
            chat_id_for_log = int(chat_id)
            target_for_log = int(target_user_id)
            await mute_reactions(bot, int(chat_id), int(target_user_id), duration)
            details = (
                f"Grupo: {chat_id}\n"
                f"Alvo: {p.get('target_label')} ({target_user_id})\n"
                f"Duração: {p.get('duration_label')}\n"
                "Permissão de reagir removida (outras permissões preservadas)."
            )
            title = "Reactor silenciado"

        log_action(chat_id=chat_id_for_log, action=action, target_user_id=target_for_log, status="success")
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                success_text(title, details),
                reply_markup=reactions_mod_keyboard(),
            )

    except TelegramForbiddenError as exc:
        log_action(chat_id=chat_id_for_log, action=action, target_user_id=target_for_log,
                   status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                error_text(
                    "Permissão insuficiente",
                    f"O Telegram recusou a ação. Erro: {type(exc).__name__}: {exc}",
                    "Confira se o bot é administrador e tem can_delete_messages / can_restrict_members.",
                ),
                reply_markup=reactions_mod_keyboard(),
            )
    except TelegramBadRequest as exc:
        log_action(chat_id=chat_id_for_log, action=action, target_user_id=target_for_log,
                   status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                error_text(
                    "Telegram recusou a operação",
                    f"{type(exc).__name__}: {exc}",
                    "Possíveis causas: mensagem não existe, reaction já removida, user não está no grupo, ou método não suportado para reactions de terceiros.",
                ),
                reply_markup=reactions_mod_keyboard(),
            )
    except Exception as exc:
        log_action(chat_id=chat_id_for_log, action=action, target_user_id=target_for_log,
                   status="error", error_type=type(exc).__name__, error_message=str(exc))
        clear_action()
        if callback.message:
            await callback.message.edit_text(
                error_text("Falha ao executar", f"{type(exc).__name__}: {exc}", "Confira o link/alvo e as permissões do bot."),
                reply_markup=reactions_mod_keyboard(),
            )


@router.callback_query(F.data == "tigrao:close")
async def tigrao_close(callback: CallbackQuery) -> None:
    if not is_owner_callback(callback):
        await callback.answer("Acesso negado.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Tigrão — painel fechado.")
    await callback.answer()
