from __future__ import annotations

from aiogram import Dispatcher

from app.bot.owner_tools import router as owner_router
from app.bot.private_tools import router as private_router
from app.bot.telegram import _register_handlers
from app.handlers.lili_rodou import router as lili_rodou_router

_registered = False


def register_all_handlers(dispatcher: Dispatcher) -> None:
    global _registered
    if _registered:
        return

    dispatcher.include_router(owner_router)
    dispatcher.include_router(private_router)
    dispatcher.include_router(lili_rodou_router)
    _register_handlers(dispatcher)
    _registered = True
