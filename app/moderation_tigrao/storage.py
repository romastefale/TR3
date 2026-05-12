from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.db.database import engine
from app.moderation_tigrao.permissions import OWNER_ID


def ensure_tables() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_groups (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    last_seen_at DATETIME
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    chat_id INTEGER,
                    action TEXT,
                    target_user_id INTEGER,
                    status TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_at DATETIME
                );
                """
            )
        )


def remember_group(chat_id: int, title: str | None = None) -> None:
    ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tigrao_groups (chat_id, title, last_seen_at)
                VALUES (:chat_id, :title, :last_seen_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    last_seen_at = excluded.last_seen_at
                """
            ),
            {
                "chat_id": chat_id,
                "title": title or str(chat_id),
                "last_seen_at": datetime.now(timezone.utc),
            },
        )


def list_groups(limit: int = 20) -> list[dict[str, Any]]:
    ensure_tables()
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT chat_id, title, last_seen_at
                    FROM tigrao_groups
                    ORDER BY last_seen_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def log_action(
    *,
    chat_id: int | None,
    action: str,
    status: str,
    target_user_id: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tigrao_logs (
                    owner_id,
                    chat_id,
                    action,
                    target_user_id,
                    status,
                    error_type,
                    error_message,
                    created_at
                ) VALUES (
                    :owner_id,
                    :chat_id,
                    :action,
                    :target_user_id,
                    :status,
                    :error_type,
                    :error_message,
                    :created_at
                )
                """
            ),
            {
                "owner_id": OWNER_ID,
                "chat_id": chat_id,
                "action": action,
                "target_user_id": target_user_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
                "created_at": datetime.now(timezone.utc),
            },
        )


def list_logs(limit: int = 10) -> list[dict[str, Any]]:
    ensure_tables()
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, owner_id, chat_id, action, target_user_id, status,
                           error_type, error_message, created_at
                    FROM tigrao_logs
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]
