from __future__ import annotations

from app.bot.adeus import router as adeus_router
from app.bot.telegram import bot_dispatcher

# Registra cedo porque app.main importa submódulos de app.bot antes do startup.
# O include_router padrão do aiogram aceita o router antes dos demais handlers.
bot_dispatcher.include_router(adeus_router)
