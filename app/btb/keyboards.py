from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.btb.state import BtbSession
from app.btb.storage import list_targets


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back_close() -> list[list[InlineKeyboardButton]]:
    return [[_btn("← Voltar", "btb:home"), _btn("✖ Fechar", "btb:close")]]


def home_keyboard(session: BtbSession) -> InlineKeyboardMarkup:
    ready = bool(session.target_username and session.group_id)
    fire_label = "⚡ Disparar comando" if ready else "⚡ Disparar (falta alvo+grupo)"
    rows = [
        [_btn("🎯 Escolher alvo", "btb:targets"), _btn("👥 Escolher grupo", "btb:groups")],
        [_btn(fire_label, "btb:run" if ready else "btb:not_ready")],
        [_btn("⚙️ Opções", "btb:opts"), _btn("📜 Logs", "btb:logs")],
        [_btn("🗂 Allowlist", "btb:allowlist")],
        [_btn("✖ Fechar", "btb:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def targets_keyboard(session: BtbSession) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    targets = list_targets(session.group_id) if session.group_id else list_targets()
    seen: set[str] = set()
    for t in targets[:15]:
        bu = t["bot_username"]
        if bu in seen:
            continue
        seen.add(bu)
        label = f"@{bu}" + (f" — {t['label']}" if t.get("label") else "")
        rows.append([_btn(label[:60], f"btb:tpick:{bu}")])
    rows.append([_btn("➕ Adicionar novo alvo", "btb:tadd")])
    rows.extend(_back_close())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def groups_keyboard(groups: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:15]:
        cid = int(g["chat_id"])
        title = str(g.get("title") or cid)
        label = title if len(title) <= 40 else title[:37] + "..."
        rows.append([_btn(label, f"btb:gpick:{cid}")])
    rows.append([_btn("Digitar chat_id", "btb:gmanual")])
    rows.extend(_back_close())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def run_modes_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_btn("👁 Visível no grupo", "btb:mode:visible")],
        [_btn("🤫 Silencioso (DM bot↔bot)", "btb:mode:silent")],
        [_btn("🧪 Dry-run (preview)", "btb:mode:dry")],
    ]
    rows.extend(_back_close())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def opts_keyboard(session: BtbSession) -> InlineKeyboardMarkup:
    cleanup_lbl = "🧹 Auto-limpar: ✅ ON" if session.cleanup else "🧹 Auto-limpar: ❌ OFF"
    fallback_lbl = "🛟 Fallback visível: ✅ ON" if session.fallback else "🛟 Fallback visível: ❌ OFF"
    rows = [
        [_btn(cleanup_lbl, "btb:opt:cleanup")],
        [_btn(f"⏱ Janela: {session.wait_seconds}s", "btb:opt:wait")],
        [_btn(fallback_lbl, "btb:opt:fallback")],
    ]
    rows.extend(_back_close())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Confirmar e disparar", "btb:fire")],
            [_btn("❌ Cancelar", "btb:home")],
        ]
    )


def allowlist_keyboard(targets: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in targets[:15]:
        rows.append([_btn(f"🗑 @{t['bot_username']} ({t['group_id']})", f"btb:arm:{t['id']}")])
    rows.append([_btn("➕ Adicionar alvo", "btb:tadd")])
    rows.extend(_back_close())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Cancelar", "btb:home")]])


def logs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🔄 Atualizar", "btb:logs"), _btn("← Voltar", "btb:home")],
        ]
    )
