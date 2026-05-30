"""/radiofm — busca uma música por termo livre e envia o card avulso.

Fluxo: `/radiofm <termo>` busca candidatos (Deezer) e mostra uma lista de
botões (título - artista). Ao escolher, o bot envia o card final (capa +
"título - artista") e apaga sozinho as mensagens de processo (o comando e a
lista de escolha), deixando só o card.

Sem contador de play/like e sem registro de reações (diferente de /playing e
/tcanvas). ZERO emojis na interface.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.services.track_search import TrackHit, search_tracks

logger = logging.getLogger(__name__)
router = Router(name="radiofm")

_MAX_RESULTS = 8
_CACHE_BOUND = 500
_AUTOCLEAN_DELAY = 6.0
_PENDING_TTL = 300.0  # 5min: tempo de sobra pra escolher; depois expira.

# Tasks de autolimpeza: ref forte pra o GC não coletar antes de rodar.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


@dataclass
class _Pending:
    hits: list[TrackHit]
    user_id: int
    command_chat_id: int
    command_msg_id: int
    ts: float = field(default_factory=time.monotonic)


# token -> resultados pendentes de escolha. Bound simples (clear ao estourar).
_pending: dict[str, _Pending] = {}


def _purge_expired() -> None:
    now = time.monotonic()
    stale = [k for k, v in _pending.items() if now - v.ts > _PENDING_TTL]
    for k in stale:
        _pending.pop(k, None)


def _card_caption(hit: TrackHit) -> str:
    title = html.escape(hit.title)
    artist = html.escape(hit.artist)
    if hit.url:
        return f'<b><a href="{html.escape(hit.url, quote=True)}">{title}</a></b> - <i>{artist}</i>'
    return f"<b>{title}</b> - <i>{artist}</i>"


async def _autoclean(bot, chat_id: int, message_ids: list[int], delay: float = _AUTOCLEAN_DELAY) -> None:
    await asyncio.sleep(delay)
    for mid in message_ids:
        if mid is None:
            continue
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            logger.debug("RADIOFM_AUTOCLEAN_DELETE_FAILED chat=%s msg=%s", chat_id, mid)


@router.message(Command("radiofm"))
async def radiofm(message: Message, command: CommandObject) -> None:
    if not message.from_user or not message.bot:
        return

    term = (command.args or "").strip()
    if not term:
        await message.answer(
            "Uso: <code>/radiofm nome da música ou artista</code>",
            parse_mode="HTML",
        )
        return

    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    hits = await search_tracks(term, limit=_MAX_RESULTS)
    if not hits:
        sent = await message.answer(f'Nada encontrado para "{html.escape(term)}".')
        _spawn(_autoclean(message.bot, message.chat.id, [message.message_id, sent.message_id]))
        return

    token = uuid.uuid4().hex[:10]
    _purge_expired()
    if len(_pending) >= _CACHE_BOUND:
        _pending.clear()
    _pending[token] = _Pending(
        hits=hits,
        user_id=message.from_user.id,
        command_chat_id=message.chat.id,
        command_msg_id=message.message_id,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{h.title} - {h.artist}", callback_data=f"rfm:{token}:{i}")]
            for i, h in enumerate(hits)
        ]
    )
    await message.answer("Escolha a faixa:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("rfm:"))
async def radiofm_pick(query: CallbackQuery) -> None:
    if not query.data or not query.message or not query.from_user or not query.bot:
        await query.answer()
        return

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, token, raw_idx = parts

    pending = _pending.get(token)
    if pending is None or (time.monotonic() - pending.ts) > _PENDING_TTL:
        _pending.pop(token, None)
        await query.answer("Essa busca expirou. Rode /radiofm de novo.", show_alert=True)
        return
    if query.from_user.id != pending.user_id:
        await query.answer("Essa busca é de outra pessoa. Rode /radiofm você também.", show_alert=True)
        return

    try:
        hit = pending.hits[int(raw_idx)]
    except (ValueError, IndexError):
        await query.answer()
        return

    # Claim atômico ANTES de qualquer await: elimina envio duplicado em
    # duplo-clique (o 2º callback acha o token já removido). asyncio é
    # single-thread, então get->validação->pop roda sem interleaving.
    if _pending.pop(token, None) is None:
        await query.answer()
        return

    await query.answer()

    bot = query.bot
    chat_id = query.message.chat.id
    caption = _card_caption(hit)

    sent = None
    if hit.cover_big:
        try:
            sent = await bot.send_photo(
                chat_id, photo=hit.cover_big, caption=caption, parse_mode="HTML"
            )
        except Exception:
            logger.warning("RADIOFM_SEND_PHOTO_FAILED track=%s", hit.track_id, exc_info=True)
    if sent is None:
        await bot.send_message(
            chat_id, caption, parse_mode="HTML", disable_web_page_preview=True
        )

    # Autolimpeza: apaga a lista de escolha e o comando original, deixa só o card.
    try:
        await query.message.delete()
    except Exception:
        logger.debug("RADIOFM_LIST_DELETE_FAILED chat=%s", chat_id, exc_info=True)
    try:
        await bot.delete_message(pending.command_chat_id, pending.command_msg_id)
    except Exception:
        logger.debug("RADIOFM_COMMAND_DELETE_FAILED chat=%s", pending.command_chat_id, exc_info=True)
