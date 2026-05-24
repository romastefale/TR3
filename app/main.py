from __future__ import annotations

import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.bot.monthfm import monthfm as monthfm_command, router as monthfm_router
from app.bot.tcanvas import router as tcanvas_router
from app.bot.tnow import router as tnow_router
from app.bot.weekfm import router as weekfm_router, weekfm as weekfm_command
from app.bot.telegram import _register_handlers, shutdown_telegram_bot, bot_dispatcher
from app.bot.tigraoresponde import handle_tigraoresponde_update
from app.btb import btb_router
from app.btb.keyboards import home_keyboard as btb_home_keyboard
from app.btb.relay import capture_bot_message as btb_capture_bot_message
from app.btb.router import on_text as btb_on_text
from app.btb.state import clear_waiting as btb_clear_waiting, get_session as btb_get_session
from app.btb.storage import ensure_tables as btb_ensure_tables
from app.config.settings import BASE_URL, TELEGRAM_BOT_TOKEN
from app.db.database import engine, init_db, run_migrations
from app.moderation_tigrao import customize_router as tigrao_customize_router, ddx_router as tigrao_ddx_router, member_tag_router as tigrao_member_tag_router, pinned_media_router as tigrao_pinned_media_router, router as tigrao_router
from app.moderation_tigrao.customize_router import tigrao_receive_group_photo
from app.moderation_tigrao.ddx_router import tigrao_ddx_receive_add_words, tigrao_ddx_receive_remove_words
from app.moderation_tigrao.ddx_runtime import tigrao_ddx_preprocess_update
from app.moderation_tigrao.keyboards import home_keyboard
from app.moderation_tigrao.member_tag_router import tigrao_member_tag_receive_text
from app.moderation_tigrao.permissions import is_owner_private_message
from app.moderation_tigrao.router import tigrao_private_text
from app.moderation_tigrao.state import get_session
from app.moderation_tigrao.storage import remember_group
from app.moderation_tigrao.texts import home_text
from app.services.music_proxy import install_music_proxy
from app.services.spotify import spotify_service

app = FastAPI(title="Minimal Backend")
logger = logging.getLogger(__name__)

bot: Bot | None = None
dispatcher: Dispatcher = bot_dispatcher
_telegram_dispatcher_configured = False
TIGRAO_TEXT_WAITING_STATES = {
    "chat_id",
    "outbound_text",
    "message_link",
    "user_id",
    "duration",
    "ddx_add_words",
    "ddx_remove_words",
    "customize_title",
    "customize_bio",
    "member_tag_user_id",
    "member_tag_value",
}


def _first_token(text_value: str | None) -> str:
    if not text_value:
        return ""
    return text_value.strip().split(maxsplit=1)[0]


def _command_name(text_value: str | None) -> str:
    token = _first_token(text_value).lower()
    return token.split("@", 1)[0]


def _is_tigrao_command(text_value: str | None) -> bool:
    return _command_name(text_value) == "/tigrao"


def _is_monthfm_command(text_value: str | None) -> bool:
    return _command_name(text_value) == "/monthfm"


def _is_weekfm_command(text_value: str | None) -> bool:
    return _command_name(text_value) == "/weekfm"


def _is_btb_command(text_value: str | None) -> bool:
    return _command_name(text_value) == "/btb"


BTB_WAITING_STATES = {"command_text", "group_chat_id", "wait_seconds", "add_target_username"}


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


def _remember_group_from_update(update: Update) -> None:
    message = update.message or update.edited_message
    if not message or message.chat.type not in {"group", "supergroup"}:
        return
    title = message.chat.title or str(message.chat.id)
    remember_group(int(message.chat.id), title)
    logger.warning(
        "TIGRAO_GROUP_REMEMBERED | chat_id=%s | title=%s",
        message.chat.id,
        title,
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


async def _handle_monthfm_direct(update: Update) -> bool:
    message = update.message
    if not message or not _is_monthfm_command(message.text):
        return False
    logger.warning(
        "MONTHFM_DIRECT_RECEIVED | update_id=%s | chat_type=%s | chat_id=%s | from_id=%s | token=%s",
        update.update_id,
        getattr(message.chat, "type", None),
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        _first_token(message.text),
    )
    await monthfm_command(message)
    logger.warning("MONTHFM_DIRECT_ANSWER_SENT | update_id=%s", update.update_id)
    return True


async def _handle_weekfm_direct(update: Update) -> bool:
    message = update.message
    if not message or not _is_weekfm_command(message.text):
        return False
    logger.warning(
        "WEEKFM_DIRECT_RECEIVED | update_id=%s | chat_type=%s | chat_id=%s | from_id=%s | token=%s",
        update.update_id,
        getattr(message.chat, "type", None),
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        _first_token(message.text),
    )
    await weekfm_command(message)
    logger.warning("WEEKFM_DIRECT_ANSWER_SENT | update_id=%s", update.update_id)
    return True


async def _handle_btb_direct(update: Update) -> bool:
    message = update.message
    if not message or not _is_btb_command(message.text):
        return False
    logger.warning(
        "BTB_DIRECT_RECEIVED | update_id=%s | chat_type=%s | from_id=%s",
        update.update_id,
        getattr(message.chat, "type", None),
        getattr(message.from_user, "id", None),
    )
    if not is_owner_private_message(message):
        return True
    btb_clear_waiting()
    from app.btb.router import _home_text as btb_home_text
    await message.answer(
        btb_home_text(),
        reply_markup=btb_home_keyboard(btb_get_session()),
        parse_mode="HTML",
    )
    return True


async def _handle_btb_waiting_text_direct(update: Update) -> bool:
    message = update.message
    if not message or not message.text:
        return False
    if not is_owner_private_message(message):
        return False
    if btb_get_session().waiting_for not in BTB_WAITING_STATES:
        return False
    logger.warning(
        "BTB_WAITING_TEXT_DIRECT | update_id=%s | waiting_for=%s",
        update.update_id,
        btb_get_session().waiting_for,
    )
    await btb_on_text(message)
    return True


async def _handle_btb_capture(update: Update) -> None:
    message = update.message
    if not message:
        return
    try:
        await btb_capture_bot_message(message)
    except Exception:
        logger.exception("BTB_CAPTURE_HOOK_FAILED | update_id=%s", update.update_id)


async def _handle_tigrao_waiting_text_direct(update: Update) -> bool:
    message = update.message
    if not message or not message.text:
        return False
    if not is_owner_private_message(message):
        return False
    session = get_session()
    if session.waiting_for not in TIGRAO_TEXT_WAITING_STATES:
        return False
    logger.warning(
        "TIGRAO_WAITING_TEXT_DIRECT | update_id=%s | waiting_for=%s | selected_action=%s | selected_chat_id=%s | from_id=%s | token=%s",
        update.update_id,
        session.waiting_for,
        session.selected_action,
        session.selected_chat_id,
        message.from_user.id if message.from_user else None,
        _first_token(message.text),
    )
    if session.waiting_for == "ddx_add_words":
        await tigrao_ddx_receive_add_words(message)
    elif session.waiting_for == "ddx_remove_words":
        await tigrao_ddx_receive_remove_words(message)
    elif session.waiting_for in {"member_tag_user_id", "member_tag_value"}:
        await tigrao_member_tag_receive_text(message)
    else:
        await tigrao_private_text(message)
    return True


async def _handle_tigrao_waiting_media_direct(update: Update) -> bool:
    message = update.message
    if not message:
        return False
    if not is_owner_private_message(message):
        return False
    session = get_session()
    if session.waiting_for != "customize_photo":
        return False
    logger.warning(
        "TIGRAO_WAITING_MEDIA_DIRECT | update_id=%s | waiting_for=%s | selected_chat_id=%s | from_id=%s | has_photo=%s | has_document=%s",
        update.update_id,
        session.waiting_for,
        session.selected_chat_id,
        message.from_user.id if message.from_user else None,
        bool(message.photo),
        bool(message.document),
    )
    await tigrao_receive_group_photo(message)
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
            dispatcher.include_router(tigrao_customize_router)
            dispatcher.include_router(tigrao_member_tag_router)
            dispatcher.include_router(tigrao_pinned_media_router)
            dispatcher.include_router(tigrao_router)
            dispatcher.include_router(monthfm_router)
            dispatcher.include_router(weekfm_router)
            dispatcher.include_router(tnow_router)
            dispatcher.include_router(tcanvas_router)
            dispatcher.include_router(btb_router)
            _register_handlers(dispatcher)
            _telegram_dispatcher_configured = True
        try:
            btb_ensure_tables()
        except Exception:
            logger.exception("BTB_ENSURE_TABLES_FAILED")
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
        replaced = await spotify_service.exchange_code_for_token(code, user_id)
        logger.error("TOKEN FLOW COMPLETED")
    except Exception as e:
        logger.error("TOKEN FLOW FAILED: %s", e)
        raise
    # Avisa no privado do user se substituiu um login antigo ou se é a 1ª vez.
    if replaced is not None and bot is not None:
        try:
            if replaced:
                msg = (
                    "✓ Spotify <b>atualizado</b> — substituí seu login anterior "
                    "pela nova autorização."
                )
            else:
                msg = "✓ Spotify <b>conectado</b> com sucesso."
            await bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
        except Exception:
            logger.exception("SPOTIFY_CALLBACK_NOTIFY_FAILED user_id=%s", user_id)
    if replaced is True:
        return {"status": "ok", "message": "Spotify atualizado — login anterior substituído."}
    if replaced is False:
        return {"status": "ok", "message": "Spotify conectado com sucesso!"}
    return {"status": "error", "message": "Falha ao conectar com Spotify. Tente /login de novo."}


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
            _remember_group_from_update(update)
        except Exception:
            logger.exception("TIGRAO_GROUP_REMEMBER_FAILED | update_id=%s", update.update_id)
        await _handle_btb_capture(update)
        try:
            tigraoresponde_handled = await handle_tigraoresponde_update(bot, update)
        except Exception:
            logger.exception("TIGRAORESPONDE_FAILED | update_id=%s", update.update_id)
            tigraoresponde_handled = False
        if tigraoresponde_handled:
            return {"ok": True}
        try:
            tigrao_handled = await _handle_tigrao_direct(update)
        except Exception:
            logger.exception("TIGRAO_DIRECT_FAILED | update_id=%s", update.update_id)
            tigrao_handled = False
        if tigrao_handled:
            return {"ok": True}
        try:
            monthfm_handled = await _handle_monthfm_direct(update)
        except Exception:
            logger.exception("MONTHFM_DIRECT_FAILED | update_id=%s", update.update_id)
            monthfm_handled = False
        if monthfm_handled:
            return {"ok": True}
        try:
            weekfm_handled = await _handle_weekfm_direct(update)
        except Exception:
            logger.exception("WEEKFM_DIRECT_FAILED | update_id=%s", update.update_id)
            weekfm_handled = False
        if weekfm_handled:
            return {"ok": True}
        try:
            btb_handled = await _handle_btb_direct(update)
        except Exception:
            logger.exception("BTB_DIRECT_FAILED | update_id=%s", update.update_id)
            btb_handled = False
        if btb_handled:
            return {"ok": True}
        try:
            btb_waiting_handled = await _handle_btb_waiting_text_direct(update)
        except Exception:
            logger.exception("BTB_WAITING_TEXT_DIRECT_FAILED | update_id=%s", update.update_id)
            btb_waiting_handled = False
        if btb_waiting_handled:
            return {"ok": True}
        try:
            tigrao_waiting_media_handled = await _handle_tigrao_waiting_media_direct(update)
        except Exception:
            logger.exception("TIGRAO_WAITING_MEDIA_DIRECT_FAILED | update_id=%s", update.update_id)
            tigrao_waiting_media_handled = False
        if tigrao_waiting_media_handled:
            return {"ok": True}
        try:
            tigrao_waiting_text_handled = await _handle_tigrao_waiting_text_direct(update)
        except Exception:
            logger.exception("TIGRAO_WAITING_TEXT_DIRECT_FAILED | update_id=%s", update.update_id)
            tigrao_waiting_text_handled = False
        if tigrao_waiting_text_handled:
            return {"ok": True}
        try:
            ddx_handled = await tigrao_ddx_preprocess_update(bot, update)
        except Exception:
            logger.exception("TIGRAO_DDX_PREPROCESS_FAILED | update_id=%s", update.update_id)
            ddx_handled = False
        if ddx_handled:
            return {"ok": True}
        await dispatcher.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("WEBHOOK_ERROR")
        return {"ok": True}
