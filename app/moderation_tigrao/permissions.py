from __future__ import annotations

from aiogram.types import CallbackQuery, Message

# S1: OWNER_ID centralizado em app.config.settings (lê de env). Antes esse
# módulo hardcodava 8505890439, criando duas fontes da verdade — se o env
# var OWNER_ID fosse mudado no Railway, btb/moderation_tigrao/storage
# continuariam usando o ID antigo. Re-export pra manter API pública estável.
from app.config.settings import MODERATOR_IDS, OWNER_ID, SECOND_MODERATOR_ID

__all__ = [
    "OWNER_ID",
    "SECOND_MODERATOR_ID",
    "MODERATOR_IDS",
    "is_owner_user",
    "is_moderator_user",
    "is_owner_private_message",
    "is_owner_callback",
]


def is_owner_user(user_id: int | None) -> bool:
    """True só pro OWNER exato. Use pra alvo de notificação DM e nada mais —
    autorização de uso do painel agora é `is_moderator_user`.
    """
    return user_id == OWNER_ID


def is_moderator_user(user_id: int | None) -> bool:
    """True pro owner OU co-moderador autorizado (MODERATOR_IDS).

    Co-moderação (decisão explícita do owner): o 2º ID hardcoded tem as
    MESMAS permissões de moderação e pode operar simultaneamente. Cada um
    tem sessão FSM própria (ver state.py), então não pisam no estado um do
    outro.
    """
    return user_id in MODERATOR_IDS


def is_owner_private_message(message: Message) -> bool:
    """Autorização de acesso ao painel em DM. Nome mantido por estabilidade
    de API, mas agora libera QUALQUER moderador autorizado (owner + 2º).
    """
    return bool(
        message.chat.type == "private"
        and message.from_user
        and is_moderator_user(message.from_user.id)
    )


def is_owner_callback(callback: CallbackQuery) -> bool:
    """Autorização de callbacks do painel. Nome mantido por estabilidade de
    API, mas agora libera QUALQUER moderador autorizado (owner + 2º).
    """
    return bool(callback.from_user and is_moderator_user(callback.from_user.id))
