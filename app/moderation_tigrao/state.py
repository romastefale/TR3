from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.moderation_tigrao.permissions import MODERATOR_IDS, OWNER_ID


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


# Correção do FSM (co-moderação): antes o estado era um singleton global,
# válido só porque um único humano (OWNER) usava o /tigrao. Agora há 2
# moderadores autorizados que podem operar SIMULTANEAMENTE — se
# compartilhassem o mesmo singleton, o "selecionar grupo" / "aguardando
# user_id" de um sobrescreveria o do outro (state leak real).
#
# Solução: uma sessão por user_id. O user_id corrente é propagado por um
# ContextVar setado no início do processamento do update (ver
# set_current_user em app/main.py), antes dos handlers diretos e do
# dispatcher. Cada handler/asyncio task herda o contexto, então
# get_session() devolve a sessão certa sem precisar passar user_id em
# todas as ~70 chamadas.
_sessions: dict[int, TigrãoSession] = {}

_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "tigrao_current_user_id", default=None
)


def set_current_user(user_id: int | None) -> None:
    """Define o moderador corrente pro contexto atual (task/request).

    Chamado no início do processamento de cada update. Quando user_id é
    None (update sem from_user), cai no bucket 0 — inofensivo porque
    nenhum moderador autorizado tem id 0.
    """
    _current_user_id.set(user_id)


def _current_key() -> int:
    uid = _current_user_id.get()
    return uid if uid is not None else 0


def get_session() -> TigrãoSession:
    key = _current_key()
    # Bound de memória: só persiste sessão pra moderador autorizado. Filtros
    # de mensagem (ddx_router/ddx_soft_router) chamam get_session() pra TODA
    # msg de texto ANTES da checagem de auth — se persistíssemos por qualquer
    # user_id, _sessions cresceria sem limite com tráfego público (DoS). Pra
    # não-moderador retorna objeto transitório vazio (waiting_for=None),
    # nunca armazenado. _sessions tem no máx. len(MODERATOR_IDS) entradas.
    if key not in MODERATOR_IDS:
        return TigrãoSession(owner_id=key or OWNER_ID)
    session = _sessions.get(key)
    if session is None:
        session = TigrãoSession(owner_id=key)
        _sessions[key] = session
    return session


def reset_session() -> TigrãoSession:
    key = _current_key()
    session = TigrãoSession(owner_id=key or OWNER_ID)
    if key in MODERATOR_IDS:
        _sessions[key] = session
    return session


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
