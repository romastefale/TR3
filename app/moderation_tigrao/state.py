from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.moderation_tigrao.permissions import OWNER_ID


# Sprint 7 (T01): se o owner abre um fluxo "envie user_id" / "envie texto"
# e abandona, o waiting_for fica grudado. Mensagem comum mandada horas
# depois vira input do fluxo antigo (risco real: colar algo aleatório
# vira tentativa de ban). 15min cobre uso natural sem ser intrusivo.
SESSION_TIMEOUT_SECONDS = 15 * 60


@dataclass
class TigrãoSession:
    owner_id: int = OWNER_ID
    selected_chat_id: int | None = None
    selected_group_title: str | None = None
    selected_action: str | None = None
    waiting_for: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Singleton global por design: todos os entry points do /tigrao checam
# `is_owner_private_message` / `is_owner_callback` (OWNER_ID em DM), então
# o "estado" só existe pra um único usuário humano de cada vez. NÃO
# refatorar pra dict por user_id sem antes mudar o modelo de permissão.
# Sprint 6 (TR3) revisou e confirmou: zero risco prático de state leak.
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


def is_waiting_expired(now: datetime | None = None) -> bool:
    """Sprint 7 (T01): True se há waiting_for ativo e expirou (15min).

    Sem waiting_for ativo retorna False (não há fluxo pra expirar).
    """
    session = get_session()
    if session.waiting_for is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - session.updated_at) > timedelta(seconds=SESSION_TIMEOUT_SECONDS)


def touch_session() -> TigrãoSession:
    """Sprint 7 (T01-fix): atualiza updated_at sem mexer em waiting_for/payload.

    Use em transitions internas que mudam `waiting_for` direto (não via
    set_action). Garante que `is_waiting_expired()` mede inatividade real
    do usuário, não tempo desde o início do fluxo.
    """
    session = get_session()
    session.updated_at = datetime.now(timezone.utc)
    return session


def consume_if_expired() -> bool:
    """Sprint 7 (T01): se o fluxo waiting está expirado, limpa e retorna True.

    Callers usam pra abortar o handler com mensagem de "sessão expirada":
        if consume_if_expired():
            await message.answer("Sessão expirada. Recomece em /tigrao.")
            return

    Sprint 7 (T01-fix2, architect): quando o fluxo NÃO expirou e há
    waiting_for ativo, renova updated_at automaticamente. Assim qualquer
    tentativa de input (inclusive inválida que faz retry no mesmo state)
    conta como atividade real do owner — não só transitions completas.
    """
    if is_waiting_expired():
        clear_action()
        return True
    session = get_session()
    if session.waiting_for is not None:
        session.updated_at = datetime.now(timezone.utc)
    return False
