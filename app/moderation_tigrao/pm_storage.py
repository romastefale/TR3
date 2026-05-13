from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.db.database import engine


def init_tigrao_pm_tables() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_pm_settings (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    title TEXT,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_pm_seen_members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_seen_at DATETIME NOT NULL,
                    last_seen_at DATETIME NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_pm_suspicious_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    text TEXT,
                    reason TEXT,
                    created_at DATETIME NOT NULL,
                    action_status TEXT
                )
                """
            )
        )


def set_pm_enabled(chat_id: int, title: str | None, enabled: bool) -> None:
    now = datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tigrao_pm_settings (chat_id, enabled, title, updated_at)
                VALUES (:chat_id, :enabled, :title, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """
            ),
            {"chat_id": chat_id, "enabled": 1 if enabled else 0, "title": title, "updated_at": now},
        )


def is_pm_enabled(chat_id: int) -> bool:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT enabled FROM tigrao_pm_settings WHERE chat_id = :chat_id"), {"chat_id": chat_id}).first()
        return bool(row and int(row[0]) == 1)


def list_pm_settings() -> list[dict]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT chat_id, enabled, title, updated_at FROM tigrao_pm_settings ORDER BY updated_at DESC")
        ).mappings().all()
        return [dict(row) for row in rows]


def mark_member_seen(chat_id: int, user_id: int) -> bool:
    now = datetime.utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT first_seen_at FROM tigrao_pm_seen_members WHERE chat_id = :chat_id AND user_id = :user_id"),
            {"chat_id": chat_id, "user_id": user_id},
        ).first()
        if row:
            conn.execute(
                text("UPDATE tigrao_pm_seen_members SET last_seen_at = :now WHERE chat_id = :chat_id AND user_id = :user_id"),
                {"now": now, "chat_id": chat_id, "user_id": user_id},
            )
            return False
        conn.execute(
            text(
                """
                INSERT INTO tigrao_pm_seen_members (chat_id, user_id, first_seen_at, last_seen_at)
                VALUES (:chat_id, :user_id, :now, :now)
                """
            ),
            {"chat_id": chat_id, "user_id": user_id, "now": now},
        )
        return True


def is_recently_first_seen(chat_id: int, user_id: int, minutes: int = 30) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT first_seen_at FROM tigrao_pm_seen_members WHERE chat_id = :chat_id AND user_id = :user_id"),
            {"chat_id": chat_id, "user_id": user_id},
        ).first()
        if not row or not row[0]:
            return True
        try:
            first_seen = datetime.fromisoformat(str(row[0]))
        except Exception:
            return True
        return first_seen >= cutoff


def save_suspicious_message(
    *,
    chat_id: int,
    chat_title: str | None,
    message_id: int,
    user_id: int,
    user_name: str | None,
    text_value: str,
    reason: str,
) -> int:
    now = datetime.utcnow()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO tigrao_pm_suspicious_messages (
                    chat_id, chat_title, message_id, user_id, user_name, text, reason, created_at, action_status
                ) VALUES (
                    :chat_id, :chat_title, :message_id, :user_id, :user_name, :text, :reason, :created_at, :action_status
                )
                """
            ),
            {
                "chat_id": chat_id,
                "chat_title": chat_title,
                "message_id": message_id,
                "user_id": user_id,
                "user_name": user_name,
                "text": text_value[:2000],
                "reason": reason,
                "created_at": now,
                "action_status": "pending",
            },
        )
        return int(result.lastrowid or 0)


def get_suspicious_message(snapshot_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM tigrao_pm_suspicious_messages WHERE id = :id"),
            {"id": snapshot_id},
        ).mappings().first()
        return dict(row) if row else None


def update_suspicious_status(snapshot_id: int, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tigrao_pm_suspicious_messages SET action_status = :status WHERE id = :id"),
            {"status": status, "id": snapshot_id},
        )


def delete_suspicious_message(snapshot_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM tigrao_pm_suspicious_messages WHERE id = :id"),
            {"id": snapshot_id},
        )


def cleanup_old_suspicious_messages(hours: int = 24) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM tigrao_pm_suspicious_messages WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        return int(result.rowcount or 0)
