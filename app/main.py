from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

from app.bot.monthfm import monthfm as monthfm_command, router as monthfm_router
from app.bot.myself import router as myself_router
from app.bot.songcharts import router as songcharts_router
from app.bot.tcanvas import router as tcanvas_router
from app.bot.tnow import router as tnow_router
from app.bot.tstory import router as tstory_router
from app.bot.tly import router as tly_router
from app.bot.radiofm import router as radiofm_router
from app.bot.weekfm import router as weekfm_router, weekfm as weekfm_command
from app.bot.mention_reactor import react_if_mention  # Sprint 8
from app.bot.setup_commands import setup_bot_commands  # Sprint 9 (#4)
from app.bot.telegram import _register_handlers, shutdown_telegram_bot, bot_dispatcher
from app.bot.tigraoresponde import handle_tigraoresponde_update
from app.btb import btb_router
from app.btb.keyboards import home_keyboard as btb_home_keyboard
from app.btb.relay import capture_bot_message as btb_capture_bot_message
from app.btb.router import on_text as btb_on_text
from app.btb.state import clear_waiting as btb_clear_waiting, get_session as btb_get_session
from app.btb.storage import ensure_tables as btb_ensure_tables
from app.config.settings import BASE_URL, TELEGRAM_BOT_TOKEN, telegram_webhook_secret, validate_required_env
from app.db.database import engine, init_db, run_migrations
from app.moderation_tigrao import customize_router as tigrao_customize_router, ddx_router as tigrao_ddx_router, ddx_soft_router as tigrao_ddx_soft_router, member_tag_router as tigrao_member_tag_router, pinned_media_router as tigrao_pinned_media_router, router as tigrao_router
from app.moderation_tigrao.customize_router import tigrao_receive_group_photo
from app.moderation_tigrao.ddx_router import tigrao_ddx_receive_add_words, tigrao_ddx_receive_remove_words
from app.moderation_tigrao.ddx_runtime import tigrao_ddx_preprocess_update
from app.moderation_tigrao.ddx_soft_runtime import tigrao_ddx_soft_preprocess_update
from app.moderation_tigrao.new_member_watch_router import router as tigrao_new_member_watch_router  # Sprint X4
from app.moderation_tigrao.inline_router import router as tigrao_inline_x9_router  # Sprint X9
from app.bot.new_member_watch_runtime import tigrao_new_member_watch_preprocess_update  # Sprint X4
from app.moderation_tigrao.keyboards import home_keyboard
from app.moderation_tigrao.member_tag_router import tigrao_member_tag_receive_text
from app.moderation_tigrao.permissions import is_owner_private_message
from app.moderation_tigrao.router import tigrao_private_text
from app.moderation_tigrao.state import get_session, set_current_user as tigrao_set_current_user
from app.btb.state import set_current_user as btb_set_current_user
from app.moderation_tigrao.storage import remember_group
from app.moderation_tigrao.texts import home_text
from app.bot.music_extras import register_music_extra_handlers
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


def _extract_update_user_id(update: Update) -> int | None:
    """Extrai o from_user.id do update pra propagar a sessão FSM correta.

    Cobre os tipos de update que carregam ator humano (mensagem, callback,
    inline, etc.). Updates sem from_user (ex.: poll) retornam None — caem no
    bucket 0 das sessões, inofensivo.
    """
    for attr in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
    ):
        event = getattr(update, attr, None)
        if event is not None:
            user = getattr(event, "from_user", None)
            if user is not None:
                return user.id
    return None


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
    missing_env = validate_required_env()
    if missing_env:
        logger.warning(
            "STARTUP_MISSING_ENV_VARS vars=%s — features dependentes vão "
            "falhar silenciosamente até serem configuradas",
            ",".join(missing_env),
        )
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
        # S4: DefaultBotProperties define parse_mode HTML como padrão. Os
        # handlers continuam passando parse_mode="HTML" explicitamente (são
        # redundantes mas inofensivos) — a melhoria principal é eliminar a
        # chance de esquecer parse_mode num novo handler.
        bot = Bot(
            token=TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        if not _telegram_dispatcher_configured:
            dispatcher.include_router(tigrao_ddx_router)
            dispatcher.include_router(tigrao_ddx_soft_router)
            dispatcher.include_router(tigrao_customize_router)
            dispatcher.include_router(tigrao_member_tag_router)
            dispatcher.include_router(tigrao_pinned_media_router)
            dispatcher.include_router(tigrao_new_member_watch_router)  # Sprint X4
            dispatcher.include_router(tigrao_inline_x9_router)  # Sprint X9
            dispatcher.include_router(tigrao_router)
            dispatcher.include_router(monthfm_router)
            dispatcher.include_router(weekfm_router)
            dispatcher.include_router(tnow_router)
            dispatcher.include_router(tcanvas_router)
            dispatcher.include_router(tstory_router)
            dispatcher.include_router(tly_router)
            dispatcher.include_router(radiofm_router)
            dispatcher.include_router(myself_router)
            dispatcher.include_router(songcharts_router)
            dispatcher.include_router(btb_router)
            # Sprint 3.5: register_music_extra_handlers usa decorators
            # dinâmicos (@dp.message) em vez de Router, por isso é chamada
            # explícita aqui em vez de include_router. Antes vinha via
            # music_proxy.install_music_proxy() — agora explícito.
            register_music_extra_handlers(dispatcher)
            _register_handlers(dispatcher)
            _telegram_dispatcher_configured = True
        try:
            btb_ensure_tables()
        except Exception:
            logger.exception("BTB_ENSURE_TABLES_FAILED")
        # Sprint 4 (S4.4): registra secret_token derivado por HMAC do
        # TELEGRAM_BOT_TOKEN. Telegram passa esse valor no header
        # X-Telegram-Bot-Api-Secret-Token em cada update; o handler
        # /webhook rejeita 403 quando ausente/diverge. Bloqueia atacante
        # que descobre BASE_URL e tenta POSTAR update forjado (ex.: forjar
        # from_user.id = OWNER_ID pra bypass do IsOwner filter).
        webhook_secret = telegram_webhook_secret()
        await bot.set_webhook(
            f"{BASE_URL}/webhook",
            allowed_updates=dispatcher.resolve_used_update_types(),
            secret_token=webhook_secret,
        )
        # Sprint 9 (#4): popula menu nativo de comandos do Telegram.
        # Owner-only commands (hidden, manual, kingplay, etc) NÃO entram
        # — ficam invisíveis pro público (per goal: owner-only NUNCA público).
        await setup_bot_commands(bot)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    from app.services.lastfm import lastfm_service  # import local: serviço só usado pra fechar o pool
    from app.services.lastfm_capsule import lastfm_capsule_service  # idem (S5.01 / R5.01)
    from app.services.lyrics import lyrics_service  # idem (/tly)

    await shutdown_telegram_bot()
    await spotify_service.shutdown()
    # Sprint 4 (S4.1): fecha pool httpx do Last.fm/Deezer. Importado
    # localmente pra não poluir o topo do módulo (não há outro uso).
    await lastfm_service.shutdown()
    # Sprint 5 (R5.01): fecha pool httpx do capsule (/monthfm).
    await lastfm_capsule_service.shutdown()
    # Fecha pool httpx do lyrics.ovh (/tly).
    await lyrics_service.shutdown()


@app.get("/healthz", status_code=200)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/callback")
async def spotify_callback(code: str, state: str) -> dict[str, str]:
    user_id = spotify_service.resolve_user_id_from_state(state)
    if user_id is None:
        logger.warning("SPOTIFY_CALLBACK_INVALID_STATE")
        return {"status": "error", "message": "Invalid state. Use /login novamente."}
    try:
        replaced = await spotify_service.exchange_code_for_token(code, user_id)
    except Exception:
        logger.exception("SPOTIFY_CALLBACK_TOKEN_FLOW_FAILED user_id=%s", user_id)
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


# Sprint 4 (S4.3): endpoint GET /spotify/track removido. Era código morto
# (zero refs em todo o repo, nenhum consumidor interno ou externo). Pior:
# aceitava qualquer user_id como query string e devolvia o que essa pessoa
# estava ouvindo, sem auth — vazamento contínuo de listening habits pra
# quem descobrisse a URL pública do Railway.


@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Sprint 4 (S4.4): valida secret_token do Telegram. Sem header válido,
    # rejeita 403 (Telegram não tenta de novo em 4xx, evita loop). Se a
    # função telegram_webhook_secret() devolve None (TELEGRAM_BOT_TOKEN
    # vazio em dev), pula a verificação — mesmo cenário em que o
    # set_webhook não registrou secret nenhum.
    expected_secret = telegram_webhook_secret()
    if expected_secret is not None:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if not hmac.compare_digest(provided, expected_secret):
            logger.warning(
                "WEBHOOK_INVALID_SECRET | client=%s",
                request.client.host if request.client else "?",
            )
            return Response(status_code=403)
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        # Correção do FSM (co-moderação): propaga o user_id corrente pros
        # ContextVars das sessões ANTES de qualquer handler (diretos +
        # dispatcher). Garante que cada moderador opere na própria sessão
        # FSM, sem sobrescrever a do outro quando moderam ao mesmo tempo.
        _current_uid = _extract_update_user_id(update)
        tigrao_set_current_user(_current_uid)
        btb_set_current_user(_current_uid)
        if bot is None:
            logger.error("Bot não inicializado")
            return {"ok": True}
        logger.warning("WEBHOOK_RECEIVED | update_id=%s", update.update_id)
        _log_message_update(update)
        try:
            _remember_group_from_update(update)
        except Exception:
            logger.exception("TIGRAO_GROUP_REMEMBER_FAILED | update_id=%s", update.update_id)
        # Sprint 8: reage 👀 a termos-gatilho em grupos. NUNCA consome
        # update (react_if_mention sempre retorna None), só dispara
        # paralelo aos demais handlers.
        try:
            await react_if_mention(bot, update)
        except Exception:
            logger.exception("MENTION_REACT_HOOK_FAILED | update_id=%s", update.update_id)
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
        # Sprint X4: observa membros novos + msgs com link e dispara DM ao
        # owner ANTES do DDX. Nunca consome o update (sempre retorna False);
        # falhas silenciosas pra não atrapalhar o pipeline normal.
        try:
            await tigrao_new_member_watch_preprocess_update(bot, update)
        except Exception:
            logger.exception("TIGRAO_NMW_PREPROCESS_FAILED | update_id=%s", update.update_id)
        try:
            ddx_handled = await tigrao_ddx_preprocess_update(bot, update)
        except Exception:
            logger.exception("TIGRAO_DDX_PREPROCESS_FAILED | update_id=%s", update.update_id)
            ddx_handled = False
        if ddx_handled:
            return {"ok": True}
        # DDX Soft (lei dos 10 minutos): roda DEPOIS do hard. Nunca consome
        # o update (sempre retorna False) — apenas agenda delete em 600s.
        try:
            await tigrao_ddx_soft_preprocess_update(bot, update)
        except Exception:
            logger.exception("TIGRAO_DDX_SOFT_PREPROCESS_FAILED | update_id=%s", update.update_id)
        await dispatcher.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("WEBHOOK_ERROR")
        return {"ok": True}
