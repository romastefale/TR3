from __future__ import annotations

import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.bot.private_tools import router as private_router, ddx_preprocess_update
from app.handlers.lili_rodou import router as lili_rodou_router
from app.bot.telegram import _register_handlers, shutdown_telegram_bot, bot_dispatcher
from app.config.settings import BASE_URL, TELEGRAM_BOT_TOKEN
from app.db.database import engine, init_db, run_migrations
from app.moderation_tigrao import ddx_router as tigrao_ddx_router, router as tigrao_router
from app.services.music_proxy import install_music_proxy
from app.services.spotify import spotify_service

app = FastAPI(title="Minimal Backend")
logger = logging.getLogger(__name__)

bot: Bot | None = None
dispatcher: Dispatcher = bot_dispatcher
_telegram_dispatcher_configured = False


@app.on_event("startup")
async def on_startup() -> None:
    global bot, _telegram_dispatcher_configured
    install_music_proxy()
    init_db()
    run_migrations(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS join_requests (
                    user_id INTEGER,
                    chat_id INTEGER,
                    created_at DATETIME
                );
                """
            )
        )
    if TELEGRAM_BOT_TOKEN:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        if not _telegram_dispatcher_configured:
            dispatcher.include_router(private_router)
            dispatcher.include_router(lili_rodou_router)
            dispatcher.include_router(tigrao_router)
            dispatcher.include_router(tigrao_ddx_router)
            _register_handlers(dispatcher)
            _telegram_dispatcher_configured = True
        await bot.set_webhook(
            f"{BASE_URL}/webhook",
            allowed_updates=dispatcher.resolve_used_update_types(),
        )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await shutdown_telegram_bot()
    await spotify_service.shutdown()


@app.get("/healthz", status_code=200)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/spotify/login")
def spotify_login(user_id: int = Query(...)) -> RedirectResponse:
    return RedirectResponse(url=spotify_service.build_auth_url(user_id))


@app.get("/callback")
async def spotify_callback(code: str, state: str) -> dict[str, str]:
    logger.error("CALLBACK RECEIVED")
    logger.error("STATE RECEIVED: %s", state)
    user_id = spotify_service.resolve_user_id_from_state(state)
    logger.error("RESOLVED USER_ID: %s", user_id)
    if user_id is None:
        logger.error("INVALID STATE")
        return {"status": "error", "message": "Invalid state. Use /login novamente."}
    try:
        await spotify_service.exchange_code_for_token(code, user_id)
        logger.error("TOKEN FLOW COMPLETED")
    except Exception as e:
        logger.error("TOKEN FLOW FAILED: %s", e)
        raise
    return {"status": "ok", "message": "Spotify conectado com sucesso!"}


@app.get("/spotify/track")
async def spotify_track(user_id: int) -> dict[str, str | None] | None:
    return await spotify_service.get_current_or_last_played(user_id)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        if bot is None:
            logger.error("Bot não inicializado")
            return {"ok": True}
        logger.warning("WEBHOOK_RECEIVED | update_id=%s", update.update_id)
        try:
            ddx_deleted = await ddx_preprocess_update(bot, update)
        except Exception:
            logger.exception("DDX_PREPROCESS_FAILED | update_id=%s", update.update_id)
            ddx_deleted = False
        if not ddx_deleted:
            try:
                await dispatcher.feed_update(bot, update)
            except Exception:
                logger.exception("DISPATCHER_FAILED | update_id=%s", update.update_id)
        return {"ok": True}
    except Exception:
        logger.exception("WEBHOOK_PARSE_FAILED")
        return {"ok": True}
