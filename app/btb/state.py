from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.moderation_tigrao.permissions import MODERATOR_IDS, OWNER_ID


@dataclass
class BtbSession:
    owner_id: int = OWNER_ID
    target_username: str | None = None
    group_id: int | None = None
    group_title: str | None = None
    mode: str = "visible"  # visible | silent | dry
    wait_seconds: int = 8
    cleanup: bool = True
    fallback: bool = False
    waiting_for: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Correção do FSM (co-moderação): igual a moderation_tigrao/state.py. Os
# 2 moderadores autorizados podem usar o BTB ao mesmo tempo, então o estado
# é por user_id, propagado via ContextVar setado no início do update (ver
# set_current_user em app/main.py). Cada um tem sua BtbSession isolada.
_sessions: dict[int, BtbSession] = {}

_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "btb_current_user_id", default=None
)


def set_current_user(user_id: int | None) -> None:
    """Define o moderador corrente pro contexto atual (task/request)."""
    _current_user_id.set(user_id)


def _current_key() -> int:
    uid = _current_user_id.get()
    return uid if uid is not None else 0


def get_session() -> BtbSession:
    key = _current_key()
    # Bound de memória: igual a moderation_tigrao/state.py. Só persiste pra
    # moderador autorizado; não-moderador recebe objeto transitório vazio.
    if key not in MODERATOR_IDS:
        return BtbSession(owner_id=key or OWNER_ID)
    session = _sessions.get(key)
    if session is None:
        session = BtbSession(owner_id=key)
        _sessions[key] = session
    return session


def reset_session() -> BtbSession:
    key = _current_key()
    session = BtbSession(owner_id=key or OWNER_ID)
    if key in MODERATOR_IDS:
        _sessions[key] = session
    return session


def clear_waiting() -> None:
    s = get_session()
    s.waiting_for = None
    s.payload = {}
    s.updated_at = datetime.now(timezone.utc)
