"""Testes de regressão da co-moderação (owner + 2º moderador).

Garante que os dois moderadores autorizados nunca interferem um no outro e
que o tráfego público não vaza estado nem cresce a memória das sessões. Ver
task-32: sem esses testes, mover `set_current_user` de lugar ou trocar o
transporte de update (webhook->polling) poderia reintroduzir vazamento de
estado entre os dois moderadores sem ninguém perceber.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config.settings import OWNER_ID, SECOND_MODERATOR_ID
from app.moderation_tigrao import state as tig_state
from app.btb import state as btb_state
from app.moderation_tigrao.permissions import MODERATOR_IDS, is_moderator_user

# Um id que NÃO é moderador autorizado (tráfego público / terceiro qualquer).
PUBLIC_ID = 111222333
assert PUBLIC_ID not in MODERATOR_IDS


@pytest.fixture(autouse=True)
def _clean_state():
    """Zera as sessões e o contexto antes/depois de cada teste.

    Os dicts `_sessions` são globais de módulo; sem limpeza um teste vazaria
    estado pro próximo e mascararia regressões reais.
    """
    tig_state._sessions.clear()
    btb_state._sessions.clear()
    tig_state.set_current_user(None)
    btb_state.set_current_user(None)
    yield
    tig_state._sessions.clear()
    btb_state._sessions.clear()
    tig_state.set_current_user(None)
    btb_state.set_current_user(None)


# --------------------------------------------------------------------------
# 1. Dois moderadores em fluxos simultâneos não se sobrescrevem
# --------------------------------------------------------------------------

def test_tigrao_two_moderators_do_not_overwrite():
    """Owner seleciona grupo A; 2º seleciona grupo B; voltando ao contexto de
    cada um, a sessão correta é preservada (keying por user_id)."""
    tig_state.set_current_user(OWNER_ID)
    tig_state.set_selected_group(-100111, "Grupo A")

    tig_state.set_current_user(SECOND_MODERATOR_ID)
    tig_state.set_selected_group(-100222, "Grupo B")

    tig_state.set_current_user(OWNER_ID)
    s_owner = tig_state.get_session()
    assert s_owner.selected_chat_id == -100111
    assert s_owner.selected_group_title == "Grupo A"

    tig_state.set_current_user(SECOND_MODERATOR_ID)
    s_second = tig_state.get_session()
    assert s_second.selected_chat_id == -100222
    assert s_second.selected_group_title == "Grupo B"


def test_tigrao_two_moderators_waiting_for_isolated():
    """O estado `waiting_for` (aguardando user_id) de um moderador não vaza
    pro outro."""
    tig_state.set_current_user(OWNER_ID)
    tig_state.set_action("ban", waiting_for="user_id")

    tig_state.set_current_user(SECOND_MODERATOR_ID)
    assert tig_state.get_session().waiting_for is None
    tig_state.set_action("mute", waiting_for="user_id", duration=600)

    tig_state.set_current_user(OWNER_ID)
    assert tig_state.get_session().selected_action == "ban"
    assert tig_state.get_session().payload == {}

    tig_state.set_current_user(SECOND_MODERATOR_ID)
    assert tig_state.get_session().selected_action == "mute"
    assert tig_state.get_session().payload == {"duration": 600}


def test_btb_two_moderators_do_not_overwrite():
    """O mesmo isolamento vale pro BTB (btb/state.py)."""
    btb_state.set_current_user(OWNER_ID)
    s = btb_state.get_session()
    s.target_username = "@alvo_owner"
    s.group_id = -100111

    btb_state.set_current_user(SECOND_MODERATOR_ID)
    s2 = btb_state.get_session()
    s2.target_username = "@alvo_second"
    s2.group_id = -100222

    btb_state.set_current_user(OWNER_ID)
    assert btb_state.get_session().target_username == "@alvo_owner"
    assert btb_state.get_session().group_id == -100111

    btb_state.set_current_user(SECOND_MODERATOR_ID)
    assert btb_state.get_session().target_username == "@alvo_second"
    assert btb_state.get_session().group_id == -100222


@pytest.mark.asyncio
async def test_tigrao_concurrent_async_tasks_isolated():
    """Simula simultaneidade real: dois moderadores em tasks asyncio
    intercaladas. Valida que o ContextVar propaga a sessão certa por task —
    é exatamente o invariante que quebraria se `set_current_user` saísse do
    início do processamento do update."""

    async def flow(user_id: int, chat_id: int, title: str) -> int | None:
        tig_state.set_current_user(user_id)
        tig_state.set_selected_group(chat_id, title)
        # Cede o loop várias vezes pra forçar intercalação entre as tasks.
        for _ in range(5):
            await asyncio.sleep(0)
        return tig_state.get_session().selected_chat_id

    results = await asyncio.gather(
        flow(OWNER_ID, -100111, "Grupo A"),
        flow(SECOND_MODERATOR_ID, -100222, "Grupo B"),
    )
    assert results == [-100111, -100222]


# --------------------------------------------------------------------------
# 2. Tráfego público NÃO cria entradas em _sessions (bound de memória)
# --------------------------------------------------------------------------

def test_tigrao_public_traffic_no_session_entry():
    tig_state.set_current_user(PUBLIC_ID)
    for _ in range(50):
        s = tig_state.get_session()
        assert s.waiting_for is None
    assert tig_state._sessions == {}
    assert PUBLIC_ID not in tig_state._sessions


def test_tigrao_public_mutation_not_persisted():
    """Mesmo mexendo na sessão transitória de um não-moderador, nada é
    armazenado em _sessions."""
    tig_state.set_current_user(PUBLIC_ID)
    tig_state.set_selected_group(-100999, "Tentativa pública")
    tig_state.set_action("ban", waiting_for="user_id")
    assert tig_state._sessions == {}


def test_btb_public_traffic_no_session_entry():
    btb_state.set_current_user(PUBLIC_ID)
    for _ in range(50):
        s = btb_state.get_session()
        assert s.waiting_for is None
    assert btb_state._sessions == {}
    assert PUBLIC_ID not in btb_state._sessions


def test_sessions_bounded_by_moderator_count():
    """Mesmo com tráfego público intercalado, _sessions nunca passa de
    len(MODERATOR_IDS) entradas."""
    for uid in (OWNER_ID, PUBLIC_ID, SECOND_MODERATOR_ID, 999, 1000):
        tig_state.set_current_user(uid)
        tig_state.set_selected_group(-1, "x")
        btb_state.set_current_user(uid)
        btb_state.get_session().group_id = -1
    assert set(tig_state._sessions) == set(MODERATOR_IDS)
    assert set(btb_state._sessions) == set(MODERATOR_IDS)
    assert len(tig_state._sessions) <= len(MODERATOR_IDS)
    assert len(btb_state._sessions) <= len(MODERATOR_IDS)


# --------------------------------------------------------------------------
# 3. is_moderator_user libera owner + 2º; bloqueia terceiros
# --------------------------------------------------------------------------

def test_is_moderator_user_allows_both_moderators():
    assert is_moderator_user(OWNER_ID) is True
    assert is_moderator_user(SECOND_MODERATOR_ID) is True


def test_is_moderator_user_blocks_third_parties():
    assert is_moderator_user(PUBLIC_ID) is False
    assert is_moderator_user(None) is False
    assert is_moderator_user(0) is False
    assert is_moderator_user(SECOND_MODERATOR_ID + 1) is False


# --------------------------------------------------------------------------
# 4. Hard-block protege AMBOS os moderadores de serem alvo
# --------------------------------------------------------------------------

def test_hard_block_protects_both_moderators_as_target():
    """O hard-block usado nos routers (router.py / inline_router.py) é
    `is_moderator_user(target_user_id)`. Ambos moderadores devem ser
    bloqueados como alvo; terceiros, não."""
    for target in MODERATOR_IDS:
        assert is_moderator_user(target) is True, f"{target} deveria ser protegido"
    assert is_moderator_user(PUBLIC_ID) is False
