"""Filters customizados aiogram3 reutilizáveis.

S3: antes ~10 handlers repetiam o boilerplate
    `if not message.from_user or message.from_user.id != OWNER_ID: return`
com sutis variações (alguns retornam silenciosos, outros respondem "Acesso
negado"). Filter `IsOwner()` aplicado no decorator faz o handler nem ser
chamado — silencioso por padrão, igual ao comportamento original dos
comandos owner-only do TR3.
"""
from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config.settings import OWNER_ID


class IsOwner(Filter):
    """Passa só se o `from_user.id` == OWNER_ID. Funciona pra Message e
    CallbackQuery. Silencioso quando bloqueia (handler simplesmente não roda).
    """

    async def __call__(self, event: TelegramObject) -> bool:
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
            return bool(user and user.id == OWNER_ID)
        return False
