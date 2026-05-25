"""/tnow — mosaico ao vivo do que cada membro cadastrado está ouvindo agora.

Critérios:
- Considera todo user_id presente em `spotify_tokens` OU `lastfm_profiles`.
- Quando rodado em grupo/supergrupo, restringe ao subconjunto que é membro
  ATIVO daquele chat (status != "left"/"kicked"). Em DM/privado, lista
  todos os cadastrados.
- Mantém SOMENTE quem está com música em reprodução neste exato momento
  (Spotify currently-playing == 200 AND is_playing=true, OU Last.fm
  @attr.nowplaying == true).
- Quem pausou / parou é ignorado.

Privacidade: o pré-requisito p/ aparecer é o usuário ter conectado
voluntariamente Spotify ou Last.fm ao bot, então o card só revela o que
ele já consentiu em expor por meio dos comandos individuais. Em grupo,
o filtro de membership garante que ninguém de fora apareça no mosaico.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import httpx
from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Chat, Message
from sqlalchemy import select

from app.config.settings import HTTP_TIMEOUT_SECONDS
from app.db.database import SessionLocal
from app.models.lastfm_profile import LastfmProfile
from app.models.spotify_token import SpotifyToken
from app.services.lastfm import lastfm_service
from app.services.spotify import spotify_service
from app.services.tnow_card import TnowEntry, render_tnow_card

logger = logging.getLogger(__name__)
router = Router(name="tnow")

# Limite duro para evitar requests excessivos e cards gigantes. Acima disso
# o serviço ainda funciona mas trunca os primeiros N a responderem.
MAX_USERS = 60
MAX_TILES = 30
COVER_FETCH_TIMEOUT = 8.0


def _registered_user_ids() -> list[int]:
    with SessionLocal() as db:
        spotify_ids = db.execute(select(SpotifyToken.user_id)).scalars().all()
        lastfm_ids = db.execute(select(LastfmProfile.user_id)).scalars().all()
    seen: set[int] = set()
    out: list[int] = []
    for uid in (*spotify_ids, *lastfm_ids):
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out[:MAX_USERS]


async def _fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=COVER_FETCH_TIMEOUT) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.content
    except Exception:
        logger.debug("TNOW_COVER_FETCH_FAILED | url=%s", url, exc_info=True)
    return None


async def _resolve_now_playing(user_id: int) -> dict[str, Any] | None:
    """Tenta Spotify primeiro; só aceita se source == 'spotify_current'.
    Cai pro Last.fm se Spotify não tiver nada tocando NESTE INSTANTE."""
    try:
        sp = await spotify_service.get_current_or_last_played(user_id)
    except Exception:
        logger.exception("TNOW_SPOTIFY_PROBE_FAILED | user_id=%s", user_id)
        sp = None
    if sp and sp.get("source") == "spotify_current" and sp.get("is_playing", True):
        # Spotify devolve 200 + item mesmo quando o usuário pausou
        # (is_playing=false). /tnow só quer quem está com som rolando agora.
        sp["_source_tag"] = "spotify"
        return sp

    try:
        lf = await lastfm_service.get_current_or_last_played(user_id)
    except Exception:
        logger.exception("TNOW_LASTFM_PROBE_FAILED | user_id=%s", user_id)
        lf = None
    if lf and lf.get("source") == "lastfm_current":
        lf["_source_tag"] = "lastfm"
        return lf

    return None


async def _display_name(bot: Any, user_id: int) -> str:
    try:
        chat = await bot.get_chat(user_id)
        name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
        if name:
            return name
        username = getattr(chat, "username", None)
        if username:
            return f"@{username}"
    except Exception:
        logger.debug("TNOW_GET_CHAT_FAILED | user_id=%s", user_id, exc_info=True)
    return f"user {user_id}"


async def _build_entry(bot: Any, user_id: int) -> TnowEntry | None:
    track = await _resolve_now_playing(user_id)
    if not track:
        return None
    cover_bytes = await _fetch_cover(track.get("album_image_url") or track.get("cover"))
    display_name = await _display_name(bot, user_id)
    return TnowEntry(
        user_id=user_id,
        display_name=display_name,
        track_name=str(track.get("track_name") or "—"),
        artist=str(track.get("artist") or "—"),
        cover_bytes=cover_bytes,
        source=str(track.get("_source_tag") or "spotify"),
    )


_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
_MEMBER_OUT_STATUSES = {"left", "kicked"}


async def _is_chat_member(bot: Any, chat_id: int, user_id: int) -> bool:
    """True se o usuário é membro ativo do chat. Erros (ex.: bot sem
    permissão de admin pra consultar) caem em False — preferimos esconder
    do que vazar alguém que talvez não esteja mais no grupo."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status = getattr(member, "status", None)
        status_value = getattr(status, "value", status)
        return str(status_value) not in _MEMBER_OUT_STATUSES
    except Exception:
        logger.debug(
            "TNOW_GET_CHAT_MEMBER_FAILED | chat_id=%s | user_id=%s",
            chat_id, user_id, exc_info=True,
        )
        return False


async def _filter_to_group_members(bot: Any, chat_id: int, user_ids: list[int]) -> list[int]:
    if not user_ids:
        return []
    checks = await asyncio.gather(
        *[_is_chat_member(bot, chat_id, uid) for uid in user_ids],
        return_exceptions=False,
    )
    return [uid for uid, ok in zip(user_ids, checks) if ok]


async def _gather_entries(bot: Any, chat: Chat | None = None) -> list[TnowEntry]:
    user_ids = _registered_user_ids()
    if not user_ids:
        return []
    # Em grupo/supergrupo: corta cedo todo mundo que não é membro daquele
    # chat antes de bater nas APIs do Spotify/Last.fm.
    if chat is not None and chat.type in _GROUP_TYPES:
        user_ids = await _filter_to_group_members(bot, chat.id, user_ids)
        if not user_ids:
            return []
    tasks = [asyncio.create_task(_build_entry(bot, uid)) for uid in user_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    entries: list[TnowEntry] = []
    for item in results:
        if isinstance(item, TnowEntry):
            entries.append(item)
        elif isinstance(item, Exception):
            logger.debug("TNOW_BUILD_ENTRY_RAISED", exc_info=item)
    # Spotify primeiro (mais rico em capa), depois Last.fm; dentro de cada
    # grupo, ordena por nome p/ deixar o mosaico previsível entre execuções.
    entries.sort(key=lambda e: (0 if e.source == "spotify" else 1, e.display_name.lower()))
    return entries[:MAX_TILES]


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("TNOW_STATUS_DELETE_FAILED | message_id=%s", message.message_id, exc_info=True)


async def _finish_tnow(status: Message) -> None:
    try:
        entries = await _gather_entries(status.bot)
        if not entries:
            await status.edit_text(
                "Ninguém cadastrado está com música tocando agora. 🦗\n"
                "Conecta seu Spotify ou Last.fm e bota algo no replay."
            )
            return

        card_bytes = await render_tnow_card(entries)
        caption = f"♫ <b>tocando agora</b> • {len(entries)} pessoa{'s' if len(entries) != 1 else ''}"
        if card_bytes:
            await _safe_delete(status)
            await status.answer_photo(
                photo=BufferedInputFile(card_bytes, filename="tnow.jpg"),
                caption=caption,
                parse_mode="HTML",
            )
            return

        # Fallback textual quando Playwright não está disponível ou falhou.
        lines = [caption]
        for entry in entries:
            safe_name = html.escape(entry.display_name)
            safe_track = html.escape(entry.track_name)
            safe_artist = html.escape(entry.artist)
            lines.append(f"• <b>{safe_name}</b> — {safe_track} <i>({safe_artist})</i>")
        await status.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("TNOW_FAILED")
        try:
            await status.edit_text("Não consegui montar o mosaico agora. Tenta de novo em alguns segundos.")
        except Exception:
            logger.exception("TNOW_FAILURE_MESSAGE_FAILED")


# I4: mantém ref forte das background tasks pra GC não coletar antes do término.
_BG_TASKS: set[asyncio.Task] = set()


@router.message(Command("tnow"))
async def tnow(message: Message) -> None:
    if not message.from_user:
        return
    # Quem manda /tnow sem ter conectado nada não vai aparecer no próprio
    # mosaico — orienta antes pra evitar confusão. Não bloqueia o comando:
    # ele segue mostrando quem mais tá ouvindo.
    from app.services.connection_check import connect_hint_for, is_user_connected
    if not is_user_connected(message.from_user.id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
    # U1: chat_action enquanto resolve playing de todos + monta mosaico.
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass
    status = await message.answer("Vendo quem tá ouvindo o quê agora...")
    task = asyncio.create_task(_finish_tnow(status))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
