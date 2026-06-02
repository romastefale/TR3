"""Filters customizados aiogram3 reutilizáveis.

Handlers privados usam `IsOwner()` no decorator para centralizar a checagem
por MODERATOR_IDS. O nome foi mantido por estabilidade de API interna.
"""
from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config.settings import MODERATOR_IDS


class IsOwner(Filter):
    """Passa se o `from_user.id` pertence aos moderadores autorizados."""

    async def __call__(self, event: TelegramObject) -> bool:
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
            return bool(user and user.id in MODERATOR_IDS)
        return False
