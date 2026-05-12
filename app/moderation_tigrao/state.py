from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.moderation_tigrao.permissions import OWNER_ID


@dataclass
class TigrãoSession:
    owner_id: int = OWNER_ID
    selected_chat_id: int | None = None
    selected_group_title: str | None = None
    selected_action: str | None = None
    waiting_for: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_session = TigrãoSession()


def get_session() -> TigrãoSession:
    return _session


def reset_session() -> TigrãoSession:
    global _session
    _session = TigrãoSession()
    return _session


def set_selected_group(chat_id: int, title: str | None = None) -> TigrãoSession:
    session = get_session()
    session.selected_chat_id = chat_id
    session.selected_group_title = title or str(chat_id)
    session.selected_action = None
    session.waiting_for = None
    session.payload = {}
    session.updated_at = datetime.now(timezone.utc)
    return session


def set_action(action: str, waiting_for: str | None = None, **payload: Any) -> TigrãoSession:
    session = get_session()
    session.selected_action = action
    session.waiting_for = waiting_for
    session.payload = payload
    session.updated_at = datetime.now(timezone.utc)
    return session


def clear_action() -> TigrãoSession:
    session = get_session()
    session.selected_action = None
    session.waiting_for = None
    session.payload = {}
    session.updated_at = datetime.now(timezone.utc)
    return session
