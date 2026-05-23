from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.btb.keyboards import (
    allowlist_keyboard,
    cancel_keyboard,
    confirm_keyboard,
    groups_keyboard,
    home_keyboard,
    logs_keyboard,
    opts_keyboard,
    run_modes_keyboard,
    targets_keyboard,
)
from app.btb.relay import relay_command
from app.btb.state import clear_waiting, get_session, reset_session
from app.btb.storage import add_target, list_logs, list_targets, remove_target
from app.moderation_tigrao.permissions import is_owner_callback, is_owner_private_message
from app.moderation_tigrao.storage import list_groups, remember_group

logger = logging.getLogger(__name__)
router = Router(name="btb")

WAITING_STATES = {
    "command_text",
    "group_chat_id",
    "wait_seconds",
    "add_target_username",
}


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _home_text() -> str:
    s = get_session()
    target = f"@{s.target_username}" if s.target_username else "—"
    group = f"{s.group_title} ({s.group_id})" if s.group_id else "—"
    cleanup = "✅" if s.cleanup else "❌"
    fallback = "✅" if s.fallback else "❌"
    return (
        "🤖 <b>BTB</b> — bot-to-bot relay\n\n"
        f"🎯 alvo: <code>{_escape(target)}</code>\n"
        f"👥 grupo: <code>{_escape(group)}</code>\n"
        f"🧹 cleanup: {cleanup} · ⏱ wait: {s.wait_seconds}s · 🛟 fallback: {fallback}\n\n"
        "Escolha uma ação:"
    )


async def _edit(cb: CallbackQuery, text_value: str, markup) -> None:
    if not is_owner_callback(cb):
        await cb.answer("Acesso negado.", show_alert=True)
        return
    if cb.message:
        try:
            await cb.message.edit_text(text_value, reply_markup=markup, parse_mode="HTML")
        except Exception:
            try:
                await cb.message.answer(text_value, reply_markup=markup, parse_mode="HTML")
            except Exception:
                logger.exception("BTB_EDIT_FAILED")
    await cb.answer()


def _is_owner_waiting(message: Message) -> bool:
    return is_owner_private_message(message) and get_session().waiting_for in WAITING_STATES


@router.message(Command("btb"))
async def btb_home(message: Message) -> None:
    if not is_owner_private_message(message):
        return
    clear_waiting()
    await message.answer(_home_text(), reply_markup=home_keyboard(get_session()), parse_mode="HTML")


@router.callback_query(F.data == "btb:home")
async def cb_home(cb: CallbackQuery) -> None:
    clear_waiting()
    await _edit(cb, _home_text(), home_keyboard(get_session()))


@router.callback_query(F.data == "btb:close")
async def cb_close(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        await cb.answer("Negado.", show_alert=True)
        return
    reset_session()
    if cb.message:
        try:
            await cb.message.delete()
        except Exception:
            pass
    await cb.answer("Fechado.")


@router.callback_query(F.data == "btb:not_ready")
async def cb_not_ready(cb: CallbackQuery) -> None:
    await cb.answer("Escolha alvo e grupo antes.", show_alert=True)


# ---------- TARGETS ----------

@router.callback_query(F.data == "btb:targets")
async def cb_targets(cb: CallbackQuery) -> None:
    await _edit(cb, _home_text() + "\n\n🎯 Selecione um alvo da allowlist:",
                targets_keyboard(get_session()))


@router.callback_query(F.data.startswith("btb:tpick:"))
async def cb_target_pick(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    username = (cb.data or "").split(":", 2)[-1]
    s = get_session()
    s.target_username = username.lower().lstrip("@")
    await _edit(cb, _home_text(), home_keyboard(s))


@router.callback_query(F.data == "btb:tadd")
async def cb_target_add(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    s = get_session()
    if not s.group_id:
        return await cb.answer("Escolha o grupo primeiro.", show_alert=True)
    s.waiting_for = "add_target_username"
    await _edit(cb,
        "🎯 <b>Adicionar alvo à allowlist</b>\n\n"
        f"Grupo: <code>{s.group_title} ({s.group_id})</code>\n\n"
        "Envie o @username do bot alvo (com ou sem @).\n"
        "Opcional: na linha seguinte, um rótulo curto.\n\n"
        "Exemplo:\n<code>@MissRose_bot\nRose principal</code>",
        cancel_keyboard())


# ---------- GROUPS ----------

@router.callback_query(F.data == "btb:groups")
async def cb_groups(cb: CallbackQuery) -> None:
    await _edit(cb, _home_text() + "\n\n👥 Selecione um grupo conhecido:",
                groups_keyboard(list_groups(50)))


@router.callback_query(F.data.startswith("btb:gpick:"))
async def cb_group_pick(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    try:
        cid = int((cb.data or "").rsplit(":", 1)[-1])
    except ValueError:
        return await cb.answer("chat_id inválido", show_alert=True)
    s = get_session()
    s.group_id = cid
    groups_map = {int(g["chat_id"]): g.get("title") for g in list_groups(50)}
    s.group_title = str(groups_map.get(cid) or cid)
    s.target_username = None
    await _edit(cb, _home_text(), home_keyboard(s))


@router.callback_query(F.data == "btb:gmanual")
async def cb_group_manual(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    get_session().waiting_for = "group_chat_id"
    await _edit(cb,
        "👥 Envie o chat_id do grupo (ex: <code>-1001234567890</code>).",
        cancel_keyboard())


# ---------- OPTS ----------

@router.callback_query(F.data == "btb:opts")
async def cb_opts(cb: CallbackQuery) -> None:
    await _edit(cb, _home_text() + "\n\n⚙️ Opções da próxima execução:",
                opts_keyboard(get_session()))


@router.callback_query(F.data == "btb:opt:cleanup")
async def cb_opt_cleanup(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    s = get_session()
    s.cleanup = not s.cleanup
    await _edit(cb, _home_text() + "\n\n⚙️ Opções da próxima execução:", opts_keyboard(s))


@router.callback_query(F.data == "btb:opt:fallback")
async def cb_opt_fallback(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    s = get_session()
    s.fallback = not s.fallback
    await _edit(cb, _home_text() + "\n\n⚙️ Opções da próxima execução:", opts_keyboard(s))


@router.callback_query(F.data == "btb:opt:wait")
async def cb_opt_wait(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    get_session().waiting_for = "wait_seconds"
    await _edit(cb,
        "⏱ Envie a janela de captura em segundos (1 a 30).",
        cancel_keyboard())


# ---------- RUN ----------

@router.callback_query(F.data == "btb:run")
async def cb_run(cb: CallbackQuery) -> None:
    s = get_session()
    if not s.target_username or not s.group_id:
        return await cb.answer("Escolha alvo e grupo.", show_alert=True)
    await _edit(cb,
        f"⚡ Pronto para disparar para <code>@{s.target_username}</code> "
        f"em <code>{_escape(s.group_title or '')}</code>.\n\nEscolha o modo:",
        run_modes_keyboard())


@router.callback_query(F.data.startswith("btb:mode:"))
async def cb_mode(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    mode = (cb.data or "").rsplit(":", 1)[-1]
    if mode not in {"visible", "silent", "dry"}:
        return await cb.answer("Modo inválido", show_alert=True)
    s = get_session()
    s.mode = mode
    s.waiting_for = "command_text"
    await _edit(cb,
        f"📝 Envie agora o comando que será relayado para "
        f"<code>@{s.target_username}</code>.\n"
        f"Modo: <b>{mode}</b>\n\nExemplo: <code>/ban 12345</code>",
        cancel_keyboard())


@router.callback_query(F.data == "btb:fire")
async def cb_fire(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    s = get_session()
    cmd = s.payload.get("command")
    if not cmd or not s.target_username or not s.group_id:
        return await cb.answer("Dados incompletos. Recomece.", show_alert=True)
    await cb.answer("Disparando...")
    result = await relay_command(
        cb.bot,
        owner_id=cb.from_user.id,
        target_username=s.target_username,
        group_id=int(s.group_id),
        group_title=s.group_title or str(s.group_id),
        command=cmd,
        mode=s.mode,
        wait_seconds=s.wait_seconds,
        cleanup=s.cleanup,
        fallback=s.fallback,
    )
    clear_waiting()
    if cb.message:
        try:
            await cb.message.edit_text(
                result["preview"],
                reply_markup=home_keyboard(s),
                parse_mode="HTML",
            )
        except Exception:
            await cb.message.answer(
                result["preview"],
                reply_markup=home_keyboard(s),
                parse_mode="HTML",
            )


# ---------- ALLOWLIST ----------

@router.callback_query(F.data == "btb:allowlist")
async def cb_allowlist(cb: CallbackQuery) -> None:
    s = get_session()
    targets = list_targets(s.group_id) if s.group_id else list_targets()
    text_value = _home_text() + "\n\n🗂 <b>Allowlist</b>"
    if s.group_id:
        text_value += f" (grupo: <code>{s.group_id}</code>)"
    if not targets:
        text_value += "\n\nNenhum alvo cadastrado."
    await _edit(cb, text_value, allowlist_keyboard(targets))


@router.callback_query(F.data.startswith("btb:arm:"))
async def cb_allowlist_rm(cb: CallbackQuery) -> None:
    if not is_owner_callback(cb):
        return await cb.answer("Negado", show_alert=True)
    try:
        tid = int((cb.data or "").rsplit(":", 1)[-1])
    except ValueError:
        return await cb.answer("ID inválido", show_alert=True)
    all_targets = list_targets()
    match = next((t for t in all_targets if t["id"] == tid), None)
    if match:
        remove_target(int(match["group_id"]), match["bot_username"])
        await cb.answer(f"@{match['bot_username']} removido.")
    else:
        await cb.answer("Já removido.", show_alert=True)
    s = get_session()
    new_targets = list_targets(s.group_id) if s.group_id else list_targets()
    text_value = _home_text() + "\n\n🗂 <b>Allowlist</b>"
    if not new_targets:
        text_value += "\n\nNenhum alvo cadastrado."
    if cb.message:
        try:
            await cb.message.edit_text(
                text_value, reply_markup=allowlist_keyboard(new_targets), parse_mode="HTML"
            )
        except Exception:
            pass


# ---------- LOGS ----------

@router.callback_query(F.data == "btb:logs")
async def cb_logs(cb: CallbackQuery) -> None:
    rows = list_logs(10)
    status_emoji = {
        "success": "✅", "no_reply": "⚠️", "partial": "⚠️",
        "error": "❌", "blocked": "🚫", "dry": "🧪",
        "sent_no_cleanup": "📤",
    }
    if not rows:
        text_value = _home_text() + "\n\n📜 <b>Logs</b>\n\nNenhum registro."
    else:
        lines = [_home_text(), "", "📜 <b>Últimos 10 relays:</b>"]
        for r in rows:
            cmd_short = (r["command"] or "")[:40]
            lines.append(
                f"{status_emoji.get(r['status'], '•')} #{r['id']} @{r['target_bot']} "
                f"[{r['mode']}] <code>{_escape(cmd_short)}</code> "
                f"(📥{r['captured_count']}/🧹{r['deleted_count']})"
            )
        text_value = "\n".join(lines)
    await _edit(cb, text_value, logs_keyboard())


# ---------- TEXT INPUT ----------

@router.message(F.text, _is_owner_waiting)
async def on_text(message: Message) -> None:
    s = get_session()
    waiting = s.waiting_for
    text_value = (message.text or "").strip()

    if waiting == "command_text":
        if not text_value:
            await message.answer("Comando vazio. Envie o texto do comando.")
            return
        s.payload["command"] = text_value
        s.waiting_for = None
        preview = (
            f"🔎 <b>Confirme o disparo</b>\n\n"
            f"📤 enviar em: <b>{_escape(s.group_title or '')}</b> (modo {s.mode})\n"
            f"🎯 alvo: <code>@{s.target_username}</code>\n"
            f"📝 comando: <code>{_escape(text_value)}</code>\n"
            f"⏱ janela: {s.wait_seconds}s · 🧹 cleanup: "
            f"{'on' if s.cleanup else 'off'}\n"
            f"📬 relatório virá nesta DM."
        )
        await message.answer(preview, reply_markup=confirm_keyboard(), parse_mode="HTML")
        return

    if waiting == "group_chat_id":
        try:
            cid = int(text_value.replace(" ", ""))
        except ValueError:
            await message.answer("Chat_id inválido. Envie um número (ex: -1001234567890).")
            return
        s.group_id = cid
        s.group_title = str(cid)
        s.target_username = None
        try:
            remember_group(cid, str(cid))
        except Exception:
            logger.exception("BTB_REMEMBER_GROUP_FAILED")
        clear_waiting()
        await message.answer("👥 Grupo selecionado.",
                             reply_markup=home_keyboard(s), parse_mode="HTML")
        return

    if waiting == "wait_seconds":
        try:
            n = int(text_value)
        except ValueError:
            await message.answer("Número inválido. Envie um inteiro entre 1 e 30.")
            return
        if not (1 <= n <= 30):
            await message.answer("Fora do range. Use 1 a 30.")
            return
        s.wait_seconds = n
        clear_waiting()
        await message.answer(f"⏱ Janela atualizada para {n}s.",
                             reply_markup=opts_keyboard(s))
        return

    if waiting == "add_target_username":
        if not s.group_id:
            clear_waiting()
            await message.answer("Grupo perdido. Recomece.",
                                 reply_markup=home_keyboard(s))
            return
        lines = [l.strip() for l in text_value.splitlines() if l.strip()]
        if not lines:
            await message.answer("@username vazio.")
            return
        bu = lines[0].lstrip("@").lower()
        if not bu or " " in bu or "@" in bu:
            await message.answer("@username inválido.")
            return
        if not bu.endswith("bot") and "_bot" not in bu:
            await message.answer(
                "⚠️ Telegram exige que usernames de bots terminem com 'bot'. "
                "Tem certeza? Reenvie ou cancele."
            )
            return
        label = lines[1] if len(lines) > 1 else None
        ok = add_target(int(s.group_id), bu, label)
        clear_waiting()
        if ok:
            s.target_username = bu
            await message.answer(
                f"✅ @{bu} adicionado à allowlist do grupo "
                f"<code>{s.group_id}</code>.",
                reply_markup=home_keyboard(s), parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠️ @{bu} já estava na allowlist (ou erro de inserção).",
                reply_markup=home_keyboard(s),
            )
        return
