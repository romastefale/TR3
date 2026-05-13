from __future__ import annotations

import html
import logging

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import text

from app.config.settings import OWNER_ID
from app.db.database import SessionLocal
from app.moderation_tigrao.storage import list_groups
from app.services.likes import likes_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)


def _normalize_optional_text(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if value is None:
        return None
    try:
        cleaned = str(value).strip()
    except Exception:
        return None
    return cleaned or None


def _safe_button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    if style:
        try:
            return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)  # type: ignore[call-arg]
        except Exception:
            pass
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _kingplay_groups_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    groups = list_groups()
    for group in groups[:10]:
        chat_id = int(group["chat_id"])
        title = str(group.get("title") or chat_id)
        label = title if len(title) <= 40 else title[:37] + "..."
        rows.append([_safe_button(label, f"kingplay:send:{chat_id}", "primary")])
    rows.append([_safe_button("Fechar", "kingplay:close", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_albnow(user_name: str, data: dict) -> str:
    safe_user = html.escape(user_name or "Usuário")
    album = html.escape(str(data.get("album_name") or ""))
    artist = html.escape(str(data.get("artist") or ""))
    track = html.escape(str(data.get("track_name") or ""))
    album_url = html.escape(str(data.get("album_url") or data.get("spotify_url") or ""), quote=True)

    if album_url:
        title = album or track or "Música"
        return f"{safe_user} · <i>♪ <b><a href=\"{album_url}\">{title}</a></b> — {artist}</i>"
    if track and artist:
        return f"{safe_user} · <i>♬ {track} — {artist}</i>"
    return f"{safe_user} · <i>nada tocando agora</i>"


async def _send_kingplay(message: Message, target_chat_id: int) -> bool:
    if not message.from_user:
        return False

    try:
        chat = await message.bot.get_chat(target_chat_id)
        group_name_raw = _normalize_optional_text(chat.title)
    except Exception:
        group_name_raw = _normalize_optional_text(str(target_chat_id))

    try:
        track = await spotify_service.get_current_or_last_played(message.from_user.id)
    except Exception:
        logger.exception("Falha no /kingplay")
        await message.answer("Erro ao obter música.")
        return False

    if not track:
        await message.answer("Nada tocando.")
        return False

    track_name = html.escape(_normalize_optional_text(track.get("track_name")) or "")
    artist_name = html.escape(_normalize_optional_text(track.get("artist")) or "")
    group_name = html.escape(group_name_raw or "")
    track_url = html.escape(str(track.get("spotify_url") or ""), quote=True)
    caption = f'<b><i>♫ {group_name} está ouvindo </i></b><a href="{track_url}"><b>{track_name}</b></a><b><i> — {artist_name}</i></b>'

    try:
        cover = track.get("album_image_url")
        if cover:
            sent = await message.bot.send_photo(chat_id=target_chat_id, photo=str(cover), caption=caption, parse_mode="HTML")
        else:
            sent = await message.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Falha de envio no /kingplay", exc_info=exc)
        await message.answer("Erro ao enviar mensagem no grupo.")
        return False

    try:
        await message.bot.pin_chat_message(chat_id=target_chat_id, message_id=sent.message_id)
    except Exception:
        logger.exception("Falha ao fixar /kingplay")

    await message.answer(f"Kingplay enviado.\nGrupo: {target_chat_id}\nMensagem: {sent.message_id}")
    return True


def register_music_extra_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("albnow"))
    async def albnow(message: Message) -> None:
        if not message.from_user:
            return
        data = await spotify_service.get_current_or_last_played(message.from_user.id)
        if not data:
            await message.answer("Nada tocando agora.")
            return
        caption = _format_albnow(message.from_user.full_name, data)
        cover = data.get("album_image_url") or data.get("cover_url")
        if cover:
            await message.answer_photo(photo=str(cover), caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    @dp.message(Command("kingplay"))
    async def kingplay(message: Message) -> None:
        if not message.from_user or message.from_user.id != OWNER_ID:
            return
        parts = (message.text or "").splitlines()
        if len(parts) >= 2:
            try:
                target_chat_id = int(parts[1].strip())
            except Exception:
                await message.answer("chat_id inválido")
                return
            await _send_kingplay(message, target_chat_id)
            return

        groups = list_groups()
        if not groups:
            await message.answer("Nenhum grupo conhecido. Envie qualquer mensagem no grupo com o bot ativo ou use:\n/kingplay\n<chat_id>")
            return
        await message.answer("Kingplay — escolha o grupo:", reply_markup=_kingplay_groups_keyboard())

    @dp.callback_query(F.data.startswith("kingplay:send:"))
    async def kingplay_send_callback(query: CallbackQuery) -> None:
        if not query.from_user or query.from_user.id != OWNER_ID:
            await query.answer("Acesso negado.", show_alert=True)
            return
        if not query.message or not query.data:
            await query.answer()
            return
        try:
            target_chat_id = int(query.data.rsplit(":", 1)[-1])
        except Exception:
            await query.answer("chat_id inválido", show_alert=True)
            return
        await query.answer("Enviando...")
        await _send_kingplay(query.message, target_chat_id)

    @dp.callback_query(F.data == "kingplay:close")
    async def kingplay_close_callback(query: CallbackQuery) -> None:
        if not query.from_user or query.from_user.id != OWNER_ID:
            await query.answer("Acesso negado.", show_alert=True)
            return
        if query.message:
            await query.message.edit_text("Kingplay fechado.")
        await query.answer()

    @dp.message(Command("debuguser"))
    async def debug_user(message: Message) -> None:
        if not message.from_user or message.from_user.id != OWNER_ID:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Uso: /debuguser <user_id>")
            return
        try:
            target_user_id = int(parts[1].strip())
        except ValueError:
            await message.answer("user_id inválido")
            return

        with SessionLocal() as db:
            total_plays = db.execute(text("SELECT COUNT(*) FROM track_plays WHERE user_id = :uid"), {"uid": target_user_id}).scalar() or 0
            likes_sent = db.execute(text("SELECT COUNT(*) FROM track_likes WHERE user_id = :uid AND COALESCE(liked, 1) = 1"), {"uid": target_user_id}).scalar() or 0
            likes_received = db.execute(text("SELECT COUNT(*) FROM track_likes WHERE owner_user_id = :uid AND COALESCE(liked, 1) = 1"), {"uid": target_user_id}).scalar() or 0

        top_tracks = await likes_service.get_user_top_tracks(target_user_id, limit=5)
        top_lines = [f"{name} → {plays}" for name, plays in top_tracks] or ["Nenhum dado encontrado."]
        await message.answer(
            "DEBUG USER\n\n"
            f"user_id: {target_user_id}\n\n"
            f"plays totais: {total_plays}\n"
            f"likes recebidos: {likes_received}\n"
            f"likes enviados: {likes_sent}\n\n"
            "TOP MÚSICAS:\n"
            + "\n".join(top_lines)
        )
