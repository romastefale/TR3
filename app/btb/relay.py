from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.btb.storage import is_allowed, log_relay

logger = logging.getLogger(__name__)


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class PendingRelay:
    cmd_msg_id: int
    capture_chat_id: int
    original_group_id: int
    target_username: str
    mode: str
    captured_msg_ids: list[int] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cleanup_event: asyncio.Event = field(default_factory=asyncio.Event)


# key = (capture_chat_id, target_username_lower)
_pending: dict[tuple[int, str], PendingRelay] = {}


async def capture_bot_message(message: Any) -> bool:
    """Pre-dispatch hook. Captures a bot reply into the matching pending relay.
    Always returns False (does not consume the update) so other pipelines keep working."""
    try:
        if not message or not message.from_user or not message.from_user.is_bot:
            return False
        username = (message.from_user.username or "").lower()
        if not username:
            return False
        key = (message.chat.id, username)
        p = _pending.get(key)
        if not p:
            return False
        if (datetime.now(timezone.utc) - p.started_at).total_seconds() > 60:
            return False
        if message.message_id == p.cmd_msg_id:
            return False
        p.captured_msg_ids.append(message.message_id)
        logger.warning(
            "BTB_CAPTURED | chat_id=%s | target=@%s | msg_id=%s | total=%d",
            message.chat.id, username, message.message_id, len(p.captured_msg_ids),
        )
        if message.reply_to_message and message.reply_to_message.message_id == p.cmd_msg_id:
            p.cleanup_event.set()
    except Exception:
        logger.exception("BTB_CAPTURE_ERROR")
    return False


async def relay_command(
    bot: Bot,
    *,
    owner_id: int,
    target_username: str,
    group_id: int,
    group_title: str,
    command: str,
    mode: Literal["visible", "silent", "dry"],
    wait_seconds: int = 8,
    cleanup: bool = True,
    fallback: bool = False,
) -> dict:
    target_username = target_username.lower().lstrip("@")

    if mode == "dry":
        preview = (
            f"🧪 <b>DRY-RUN</b>\n"
            f"🎯 alvo: @{target_username}\n"
            f"👥 grupo: {_escape(group_title)} (<code>{group_id}</code>)\n"
            f"📝 comando: <code>{_escape(command)}</code>\n"
            f"ℹ️ nada enviado, nada deletado."
        )
        log_relay(from_user_id=owner_id, target_bot=target_username, group_id=group_id,
                  mode="dry", command=command, cmd_msg_id=0, captured_count=0,
                  deleted_count=0, status="dry")
        return {"ok": True, "preview": preview}

    if mode == "visible" and not is_allowed(group_id, target_username):
        msg = (
            f"🚫 <b>/btb bloqueado</b>\n"
            f"🎯 alvo: @{target_username}\n"
            f"👥 grupo: <code>{group_id}</code>\n"
            f"ℹ️ alvo não está na allowlist desse grupo.\n"
            f"Abra 🗂 Allowlist para adicionar."
        )
        log_relay(from_user_id=owner_id, target_bot=target_username, group_id=group_id,
                  mode=mode, command=command, cmd_msg_id=0, captured_count=0,
                  deleted_count=0, status="blocked", error_message="not in allowlist")
        return {"ok": False, "preview": msg}

    if mode == "silent":
        send_chat: Any = f"@{target_username}"
        prospective_capture_chat = None
    else:
        send_chat = group_id
        prospective_capture_chat = group_id

    if prospective_capture_chat is not None:
        existing = _pending.get((prospective_capture_chat, target_username))
        if existing is not None:
            elapsed = (datetime.now(timezone.utc) - existing.started_at).total_seconds()
            msg = (
                f"⏳ <b>/btb ocupado</b>\n"
                f"🎯 alvo: @{target_username}\n"
                f"👥 grupo: {_escape(group_title)} (<code>{group_id}</code>)\n"
                f"ℹ️ já existe um relay ativo para esse alvo nesse chat há "
                f"{elapsed:.1f}s. Aguarde a finalização e tente de novo."
            )
            log_relay(from_user_id=owner_id, target_bot=target_username,
                      group_id=group_id, mode=mode, command=command,
                      cmd_msg_id=0, captured_count=0, deleted_count=0,
                      status="busy", error_message="duplicate active relay")
            return {"ok": False, "preview": msg}

    try:
        sent = await bot.send_message(chat_id=send_chat, text=command)
        effective_mode = mode
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        if mode == "silent" and fallback:
            if not is_allowed(group_id, target_username):
                return _fail(owner_id, target_username, group_id, group_title, mode, command,
                             Exception("fallback bloqueado: alvo não está na allowlist"))
            try:
                sent = await bot.send_message(chat_id=group_id, text=command)
                effective_mode = "visible"
            except Exception as exc2:
                return _fail(owner_id, target_username, group_id, group_title, mode, command, exc2)
        else:
            return _fail(owner_id, target_username, group_id, group_title, mode, command, exc)
    except Exception as exc:
        return _fail(owner_id, target_username, group_id, group_title, mode, command, exc)

    capture_chat_id = sent.chat.id
    cmd_msg_id = sent.message_id

    existing = _pending.get((capture_chat_id, target_username))
    if existing is not None:
        elapsed = (datetime.now(timezone.utc) - existing.started_at).total_seconds()
        msg = (
            f"⏳ <b>/btb ocupado (race detectada após envio)</b>\n"
            f"🎯 alvo: @{target_username}\n"
            f"👥 chat: <code>{capture_chat_id}</code>\n"
            f"ℹ️ relay anterior ainda ativo há {elapsed:.1f}s.\n"
            f"Comando foi enviado (msg <code>{cmd_msg_id}</code>) mas não será trackeado/limpo automaticamente.\n"
            f"Aguarde e use cleanup manual se necessário."
        )
        log_relay(from_user_id=owner_id, target_bot=target_username,
                  group_id=group_id, mode=effective_mode, command=command,
                  cmd_msg_id=cmd_msg_id, captured_count=0, deleted_count=0,
                  status="busy_after_send",
                  error_message="duplicate active relay detected after send")
        return {"ok": False, "preview": msg}

    p = PendingRelay(
        cmd_msg_id=cmd_msg_id,
        capture_chat_id=capture_chat_id,
        original_group_id=group_id,
        target_username=target_username,
        mode=effective_mode,
    )
    _pending[(capture_chat_id, target_username)] = p

    asyncio.create_task(
        _finalize(
            bot, p,
            owner_id=owner_id,
            group_title=group_title,
            command=command,
            wait_seconds=wait_seconds,
            cleanup=cleanup,
        )
    )

    return {
        "ok": True,
        "preview": (
            f"📤 <b>/btb disparado</b>\n"
            f"🎯 alvo: @{target_username}\n"
            f"👥 grupo: {_escape(group_title)}\n"
            f"📝 <code>{_escape(command)}</code>\n"
            f"🎛 modo: {effective_mode}\n"
            f"⏱ capturando respostas por {wait_seconds}s...\n"
            f"📬 relatório virá nesta DM."
        ),
    }


def _fail(owner_id: int, target: str, gid: int, gtitle: str, mode: str, command: str, exc: Exception) -> dict:
    err_type = type(exc).__name__
    err_msg = str(exc)
    log_relay(from_user_id=owner_id, target_bot=target, group_id=gid, mode=mode,
              command=command, cmd_msg_id=0, captured_count=0, deleted_count=0,
              status="error", error_message=f"{err_type}: {err_msg}")
    low = err_msg.lower()
    hint = ""
    if "chat not found" in low or "can't initiate" in low or "bot can't" in low:
        hint = "\nℹ️ habilite bot-to-bot communication nos dois bots no @BotFather (Bot API 10)."
    elif "forbidden" in err_type.lower():
        hint = "\nℹ️ tigraoRADIO sem permissão. Confirme que é admin do grupo."
    return {
        "ok": False,
        "preview": (
            f"❌ <b>/btb falhou</b>\n"
            f"🎯 alvo: @{target}\n"
            f"👥 grupo: {_escape(gtitle)} (<code>{gid}</code>)\n"
            f"📝 <code>{_escape(command)}</code>\n"
            f"💥 {err_type}: {_escape(err_msg)[:300]}{hint}"
        ),
    }


async def _finalize(
    bot: Bot, p: PendingRelay, *,
    owner_id: int, group_title: str,
    command: str, wait_seconds: int, cleanup: bool,
) -> None:
    try:
        try:
            await asyncio.wait_for(p.cleanup_event.wait(), timeout=wait_seconds)
            await asyncio.sleep(1.5)
        except asyncio.TimeoutError:
            pass

        deleted: list[int] = []
        errors: list[str] = []

        if cleanup:
            ids_to_delete = [p.cmd_msg_id, *p.captured_msg_ids]
            for mid in ids_to_delete:
                try:
                    await bot.delete_message(p.capture_chat_id, mid)
                    deleted.append(mid)
                except TelegramBadRequest as e:
                    errors.append(f"msg {mid}: {e.message}")
                except TelegramForbiddenError:
                    errors.append(f"msg {mid}: sem permissão")
                except Exception as e:
                    errors.append(f"msg {mid}: {type(e).__name__}")

        elapsed = (datetime.now(timezone.utc) - p.started_at).total_seconds()
        captured = len(p.captured_msg_ids)

        if not cleanup:
            status = "sent_no_cleanup"
            summary = (
                f"📤 <b>/btb enviado (sem limpeza)</b>\n"
                f"🎯 alvo: @{p.target_username}\n"
                f"👥 grupo: {_escape(group_title)}\n"
                f"📝 <code>{_escape(command)}</code>\n"
                f"📥 {captured} resposta(s) capturada(s)\n"
                f"⏱ {elapsed:.1f}s"
            )
        elif not errors and captured > 0:
            status = "success"
            extras = max(0, len(deleted) - 1)
            summary = (
                f"✅ <b>/btb concluído</b>\n"
                f"🎯 alvo: @{p.target_username}\n"
                f"👥 grupo: {_escape(group_title)}\n"
                f"📝 <code>{_escape(command)}</code>\n"
                f"🧹 {len(deleted)} mensagens deletadas (1 comando + {extras} respostas)\n"
                f"⏱ {elapsed:.1f}s"
            )
        elif not errors and captured == 0:
            status = "no_reply"
            summary = (
                f"⚠️ <b>/btb sem resposta</b>\n"
                f"🎯 alvo: @{p.target_username}\n"
                f"👥 grupo: {_escape(group_title)}\n"
                f"📝 <code>{_escape(command)}</code>\n"
                f"🧹 {len(deleted)} mensagem deletada (só o comando)\n"
                f"⏱ {elapsed:.1f}s\n"
                f"ℹ️ alvo não respondeu — bot off, comando inválido, ou bot-to-bot desativado no destino"
            )
        else:
            status = "partial"
            errs_text = "\n".join(f"  • {e}" for e in errors[:5])
            summary = (
                f"⚠️ <b>/btb limpeza parcial</b>\n"
                f"🎯 alvo: @{p.target_username}\n"
                f"👥 grupo: {_escape(group_title)}\n"
                f"📝 <code>{_escape(command)}</code>\n"
                f"📥 {captured} resposta(s) capturada(s)\n"
                f"🧹 {len(deleted)} deletada(s)\n"
                f"❌ {len(errors)} falha(s):\n{errs_text}\n"
                f"⏱ {elapsed:.1f}s\n"
                f"ℹ️ verifique can_delete_messages do tigraoRADIO"
            )

        log_relay(from_user_id=owner_id, target_bot=p.target_username,
                  group_id=p.original_group_id, mode=p.mode, command=command,
                  cmd_msg_id=p.cmd_msg_id, captured_count=captured,
                  deleted_count=len(deleted), status=status,
                  error_message="; ".join(errors)[:500] if errors else None)

        try:
            await bot.send_message(owner_id, summary, parse_mode="HTML")
        except Exception:
            logger.exception("BTB_OWNER_DM_FAILED")
    finally:
        _pending.pop((p.capture_chat_id, p.target_username), None)
