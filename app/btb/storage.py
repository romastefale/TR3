from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.db.database import engine


def _pk_clause() -> str:
    return "SERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _ts_clause() -> str:
    return "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"


def ensure_tables() -> None:
    pk = _pk_clause()
    ts = _ts_clause()
    bigint = "BIGINT" if engine.dialect.name == "postgresql" else "INTEGER"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS btb_targets (
                    id {pk},
                    group_id {bigint} NOT NULL,
                    bot_username TEXT NOT NULL,
                    label TEXT,
                    added_at {ts},
                    UNIQUE(group_id, bot_username)
                );
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS btb_logs (
                    id {pk},
                    ts {ts},
                    from_user_id {bigint},
                    target_bot TEXT,
                    group_id {bigint},
                    mode TEXT,
                    command TEXT,
                    cmd_msg_id {bigint},
                    captured_count INTEGER,
                    deleted_count INTEGER,
                    status TEXT,
                    error_message TEXT
                );
                """
            )
        )


def add_target(group_id: int, bot_username: str, label: str | None = None) -> bool:
    ensure_tables()
    bu = bot_username.lower().lstrip("@")
    with engine.begin() as conn:
        try:
            conn.execute(
                text(
                    """
                    INSERT INTO btb_targets (group_id, bot_username, label, added_at)
                    VALUES (:gid, :bu, :lbl, :ts)
                    """
                ),
                {"gid": group_id, "bu": bu, "lbl": label, "ts": datetime.now(timezone.utc)},
            )
            return True
        except Exception:
            return False


def remove_target(group_id: int, bot_username: str) -> bool:
    ensure_tables()
    bu = bot_username.lower().lstrip("@")
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM btb_targets WHERE group_id=:gid AND bot_username=:bu"),
            {"gid": group_id, "bu": bu},
        )
        return result.rowcount > 0


def list_targets(group_id: int | None = None) -> list[dict[str, Any]]:
    ensure_tables()
    with engine.begin() as conn:
        if group_id is None:
            rows = conn.execute(
                text(
                    "SELECT id, group_id, bot_username, label, added_at "
                    "FROM btb_targets ORDER BY added_at DESC"
                )
            ).mappings().all()
        else:
            rows = conn.execute(
                text(
                    "SELECT id, group_id, bot_username, label, added_at "
                    "FROM btb_targets WHERE group_id=:gid ORDER BY added_at DESC"
                ),
                {"gid": group_id},
            ).mappings().all()
    return [dict(r) for r in rows]


def is_allowed(group_id: int, bot_username: str) -> bool:
    ensure_tables()
    bu = bot_username.lower().lstrip("@")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM btb_targets WHERE group_id=:gid AND bot_username=:bu LIMIT 1"),
            {"gid": group_id, "bu": bu},
        ).first()
    return row is not None


def log_relay(
    *,
    from_user_id: int,
    target_bot: str,
    group_id: int | None,
    mode: str,
    command: str,
    cmd_msg_id: int,
    captured_count: int,
    deleted_count: int,
    status: str,
    error_message: str | None = None,
) -> None:
    ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO btb_logs (
                    ts, from_user_id, target_bot, group_id, mode, command,
                    cmd_msg_id, captured_count, deleted_count, status, error_message
                ) VALUES (
                    :ts, :fu, :tb, :gid, :mode, :cmd,
                    :cmid, :cc, :dc, :st, :em
                )
                """
            ),
            {
                "ts": datetime.now(timezone.utc),
                "fu": from_user_id,
                "tb": target_bot,
                "gid": group_id,
                "mode": mode,
                "cmd": (command or "")[:512],
                "cmid": cmd_msg_id,
                "cc": captured_count,
                "dc": deleted_count,
                "st": status,
                "em": (error_message or "")[:512] if error_message else None,
            },
        )


def list_logs(limit: int = 10) -> list[dict[str, Any]]:
    ensure_tables()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, ts, target_bot, group_id, mode, command,
                       captured_count, deleted_count, status, error_message
                FROM btb_logs ORDER BY id DESC LIMIT :lim
                """
            ),
            {"lim": limit},
        ).mappings().all()
    return [dict(r) for r in rows]
