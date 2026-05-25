from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.moderation_tigrao.permissions import OWNER_ID


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


# Singleton global por design: todos os entry points do BTB checam
# `is_owner_private_message` (OWNER_ID em DM), então o "estado" só existe
# pra um único usuário humano de cada vez. NÃO refatorar pra dict por
# user_id sem antes mudar o modelo de permissão. Sprint 6 (TR3) revisou
# e confirmou: zero risco prático de state leak.
_session = BtbSession()


def get_session() -> BtbSession:
    return _session


def reset_session() -> BtbSession:
    global _session
    _session = BtbSession()
    return _session


def clear_waiting() -> None:
    s = get_session()
    s.waiting_for = None
    s.payload = {}
    s.updated_at = datetime.now(timezone.utc)
