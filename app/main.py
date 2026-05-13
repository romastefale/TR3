from __future__ import annotations

import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.bot.telegram import _register_handlers, shutdown_telegram_bot, bot_dispatcher
from app.config.settings import BASE_URL, TELEGRAM_BOT_TOKEN
from app.db.database import engine, init_db, run_migrations
from app.moderation_tigrao import ddx_router as tigrao_ddx_router, router as tigrao_router
from app.moderation_tigrao.ddx_runtime import tigrao_ddx_preprocess_update
from app.moderation_tigrao.keyboards import home_keyboard
from app.moderation_tigrao.permissions import is_owner_private_message
from app.moderation_tigrao.texts import home_text
from app.services.music_proxy import install_music_proxy
from app.services.spotify import spotify_service

app = FastAPI(title="Minimal Backend")
logger = logging.getLogger(__name__)

bot: Bot | None = None
dispatcher: Dispatcher = bot_dispatcher
_telegram_dispatcher_configured = False


def _first_token(text_value: str | None) -> str:
    if not text_value:
        return ""
    return text_value.strip().split(maxsplit=1)[0]


def _is_tigrao_command(text_value: str | None) -> bool:
    token = _first_token(text_value).lower()
    command = token.split("@", 1)[0]
    return command == "/tigrao"


def _log_message_update(update: Update) -> None:
    message = update.message
    if not message:
        logger.warning("TG_UPDATE_NO_MESSAGE | update_id=%s", update.update_id)
        return
    token = _first_token(message.text)
    logger.warning(
        "TG_MESSAGE | update_id=%s | chat_type=%s | chat_id=%s | from_id=%s | token=%s",
        update.update_id,
        getattr(message.chat, "type", None),
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        token or "-",
    )


async def _handle_tigrao_direct(update: Update) -> bool:
    message = update.message
    if not message:
        logger.warning("TIGRAO_DIRECT_SKIP | reason=no_message | update_id=%s", update.update_id)
        return False
    if not _is_tigrao_command(message.text):
        return False
    logger.warning(
        "TIGRAO_DIRECT_RECEIVED | update_id=%s | chat_type=%s | chat_id=%s | from_id=%s | token=%s",
        update.update_id,
        getattr(message.chat, "type", None),
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        _first_token(message.text),
    )
    if not is_owner_private_message(message):
        logger.warning(
            "TIGRAO_DIRECT_DENIED | update_id=%s | chat_type=%s | from_id=%s",
            update.update_id,
            getattr(message.chat, "type", None),
            getattr(message.from_user, "id", None),
        )
        return True
    logger.warning(
        "TIGRAO_DIRECT_ALLOWED | update_id=%s | chat_type=%s | from_id=%s",
        update.update_id,
        message.chat.type,
        message.from_user.id if message.from_user else None,
    )
    await message.answer(home_text(), reply_markup=home_keyboard())
    logger.warning("TIGRAO_DIRECT_ANSWER_SENT | update_id=%s", update.update_id)
    return True


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
            dispatcher.include_router(tigrao_ddx_router)
            dispatcher.include_router(tigrao_router)
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
        _log_message_update(update)
        try:
            tigrao_handled = await _handle_tigrao_direct(update)
        except Exception:
            logger.exception("TIGRAO_DIRECT_FAILED | update_id=%s", update.update_id)
            tigrao_handled = False
        if tigrao_handled:
            return {"ok": True}
        try:
            ddx_deleted = await tigrao_ddx_preprocess_update(bot, update)
        except Exception:
            logger.exception("TIGRAO_DDX_PREPROCESS_FAILED | update_id=%s", update.update_id)
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
