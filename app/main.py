from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse

from app.bot.telegram import _register_handlers, bot_dispatcher, shutdown_telegram_bot
from app.config.settings import BASE_URL, TELEGRAM_BOT_TOKEN
from app.db.database import init_db
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)
app = FastAPI(title="tigraoRADIO TR3")

bot: Bot | None = None
dispatcher: Dispatcher = bot_dispatcher
_configured = False


@app.on_event("startup")
async def on_startup() -> None:
    global bot, _configured
    init_db()
    if TELEGRAM_BOT_TOKEN:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        if not _configured:
            _register_handlers(dispatcher)
            _configured = True
        await bot.set_webhook(
            f"{BASE_URL}/webhook",
            allowed_updates=dispatcher.resolve_used_update_types(),
        )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await shutdown_telegram_bot()


@app.get("/healthz", status_code=200)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/spotify/login")
def spotify_login(user_id: int = Query(...)) -> RedirectResponse:
    return RedirectResponse(url=spotify_service.build_auth_url(user_id))


@app.get("/callback")
async def spotify_callback(code: str, state: str) -> dict[str, str]:
    user_id = spotify_service.resolve_user_id_from_state(state)
    if user_id is None:
        return {"status": "error", "message": "Invalid state. Use /login novamente."}
    await spotify_service.exchange_code_for_token(code, user_id)
    return {"status": "ok", "message": "Spotify conectado com sucesso!"}


@app.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    try:
        if bot is None:
            logger.error("Bot não inicializado")
            return {"ok": True}
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("WEBHOOK_FAILED")
        return {"ok": True}
