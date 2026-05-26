from __future__ import annotations

import json
import re
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
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_ddx_filters (
                    chat_id INTEGER PRIMARY KEY,
                    words TEXT,
                    enabled INTEGER,
                    updated_at DATETIME
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tigrao_ddx_soft_filters (
                    chat_id INTEGER PRIMARY KEY,
                    words TEXT,
                    enabled INTEGER,
                    updated_at DATETIME
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


_ERROR_MESSAGE_MAX_LEN = 200
_LONG_DIGITS_RE = re.compile(r"\d{6,}")


def _sanitize_error_message(message: str | None) -> str | None:
    """Sprint 7 (T02): trunca + redige números longos (>=6 dígitos) que
    podem vazar user_id/chat_id/phone via str(exception) do Telegram.

    DB do Railway tem backups — manter PII em texto puro lá é risco
    desnecessário. Aceitamos perder precisão de debug em troca.
    """
    if message is None:
        return None
    redacted = _LONG_DIGITS_RE.sub("***", message)
    if len(redacted) > _ERROR_MESSAGE_MAX_LEN:
        redacted = redacted[: _ERROR_MESSAGE_MAX_LEN - 3] + "..."
    return redacted


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
                "error_message": _sanitize_error_message(error_message),
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


def get_ddx_filters(chat_id: int) -> dict[str, Any] | None:
    ensure_tables()
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT chat_id, words, enabled, updated_at
                    FROM tigrao_ddx_filters
                    WHERE chat_id = :chat_id
                    """
                ),
                {"chat_id": chat_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def set_ddx_filters(chat_id: int, words: list[str], enabled: bool = True) -> None:
    ensure_tables()
    clean_words = [str(word).strip() for word in words if str(word).strip()]
    deduped_words = list(dict.fromkeys(clean_words))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tigrao_ddx_filters (chat_id, words, enabled, updated_at)
                VALUES (:chat_id, :words, :enabled, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    words = excluded.words,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "chat_id": chat_id,
                "words": json.dumps(deduped_words, ensure_ascii=False),
                "enabled": 1 if enabled else 0,
                "updated_at": datetime.now(timezone.utc),
            },
        )


def load_ddx_words(chat_id: int) -> list[str]:
    row = get_ddx_filters(chat_id)
    if not row:
        return []
    try:
        words = json.loads(str(row.get("words") or "[]"))
    except Exception:
        return []
    if not isinstance(words, list):
        return []
    return [str(word) for word in words]


# ---------------------------------------------------------------------------
# DDX Soft (lei de 10 minutos): mesma estrutura do DDX hard, mas a ação é
# delete agendado em 600s ao invés de imediato. Tabela SEPARADA pra zero
# acoplamento com o hard — palavras das duas listas nunca se cruzam por
# decisão do owner (regra explícita do projeto).
# ---------------------------------------------------------------------------


def get_ddx_soft_filters(chat_id: int) -> dict[str, Any] | None:
    ensure_tables()
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT chat_id, words, enabled, updated_at
                    FROM tigrao_ddx_soft_filters
                    WHERE chat_id = :chat_id
                    """
                ),
                {"chat_id": chat_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def set_ddx_soft_filters(chat_id: int, words: list[str], enabled: bool = True) -> None:
    ensure_tables()
    clean_words = [str(word).strip() for word in words if str(word).strip()]
    deduped_words = list(dict.fromkeys(clean_words))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tigrao_ddx_soft_filters (chat_id, words, enabled, updated_at)
                VALUES (:chat_id, :words, :enabled, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    words = excluded.words,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "chat_id": chat_id,
                "words": json.dumps(deduped_words, ensure_ascii=False),
                "enabled": 1 if enabled else 0,
                "updated_at": datetime.now(timezone.utc),
            },
        )


def load_ddx_soft_words(chat_id: int) -> list[str]:
    row = get_ddx_soft_filters(chat_id)
    if not row:
        return []
    try:
        words = json.loads(str(row.get("words") or "[]"))
    except Exception:
        return []
    if not isinstance(words, list):
        return []
    return [str(word) for word in words]
