"""/tstory — gera um "story" vertical (9:16) da música tocando agora.

Fundo = vídeo do Spotify Canvas (mesmo mecanismo do /tcanvas) com o card de
info na frente. Sem Canvas / falha de download / falha de composição, cai no
card estático vertical (fallback). O card estampa o nome do bot atualizado e
usa a foto de perfil atual do bot como ícone (via app/services/bot_identity).

Sem emojis, sem contadores de play/like, sem registro de reações — é um asset
pronto pro usuário publicar como story.
"""
from __future__ import annotations

import html as _html
import logging
import time

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.services.bot_identity import get_bot_identity
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.music import music_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service
from app.services.tstory_card import render_tstory_full, render_tstory_overlay
from app.services.tstory_video import compose_story_video

logger = logging.getLogger(__name__)
router = Router(name="tstory")

# Cooldown por user: render Playwright + ffmpeg é pesado, 8s evita spam sem
# atrapalhar uso legítimo. Mesmo padrão (bound + clear) do /tcanvas.
_COOLDOWN_SECONDS = 8.0
_COOLDOWN_BOUND = 5000
_last_use: dict[int, float] = {}


def _check_cooldown(user_id: int) -> float | None:
    now = time.monotonic()
    last = _last_use.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < _COOLDOWN_SECONDS:
        return _COOLDOWN_SECONDS - elapsed
    if len(_last_use) >= _COOLDOWN_BOUND:
        _last_use.clear()
    _last_use[user_id] = now
    return None


# Capa de álbum raramente passa de ~1MB; 5MB é teto folgado pra abortar
# downloads anômalos sem estourar memória.
_COVER_MAX_BYTES = 5 * 1024 * 1024


async def _download(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _COVER_MAX_BYTES:
                        logger.warning("TSTORY_COVER_OVERSIZE url=%s", url)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks) or None
    except Exception:
        logger.debug("TSTORY_COVER_DL_FAILED url=%s", url, exc_info=True)
    return None


def _caption(user_name: str, title: str, artist: str, url: str) -> str:
    user = _html.escape(user_name or "Usuário")
    track_name = _html.escape(title or "")
    artist_name = _html.escape(artist or "")
    track_part = (
        f'<a href="{_html.escape(url, quote=True)}">{track_name}</a>' if url else track_name
    )
    return f"<b>{user}</b> · {track_part} — <i>{artist_name}</i>"


@router.message(Command("tstory"))
async def tstory(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    if not is_user_connected(user_id):
        await message.answer(
            connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True
        )
        return

    remaining = _check_cooldown(user_id)
    if remaining is not None:
        await message.answer(f"Aguarda {remaining:.0f}s antes de pedir outro story.")
        return

    track = await music_service.get_current_or_last_played(user_id)
    if not track:
        await message.answer(
            "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo."
        )
        return

    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
    except Exception:
        pass

    title = str(track.get("track_name") or "").strip()
    artist = str(track.get("artist") or "").strip()
    cover_url = track.get("album_image_url")
    spotify_url = str(track.get("spotify_url") or "").strip()
    track_id = str(track.get("track_id") or "").strip()
    user_name = message.from_user.full_name or "Usuário"
    listening = f"{user_name} está ouvindo agora"
    caption = _caption(user_name, title, artist, spotify_url)

    cover_bytes = await _download(cover_url)
    identity = await get_bot_identity(message.bot)
    bot_name = identity.name
    bot_logo = identity.photo_bytes

    # Resolve track_id Spotify base62 quando o music_service devolve Last.fm
    # ("lfm:<hash>") — Canvas só aceita id Spotify. Mesma técnica do /tcanvas.
    canvas_track_id = track_id
    if track_id.startswith("lfm:") and artist and title:
        try:
            match = await spotify_service.search_track(artist, title)
            if match and match.get("id"):
                canvas_track_id = match["id"]
        except Exception:
            logger.exception("TSTORY_RESOLVE_ERROR track=%s", track_id)

    # Tentativa principal: vídeo do Canvas (fundo) + card overlay.
    video_bytes: bytes | None = None
    try:
        canvas_url = await spotify_canvas_service.get_canvas_url(canvas_track_id)
        if canvas_url:
            canvas_bytes = await spotify_canvas_service.download_canvas_bytes(canvas_url)
            if canvas_bytes:
                overlay_png = await render_tstory_overlay(
                    cover_bytes=cover_bytes,
                    listening=listening,
                    title=title,
                    artist=artist,
                    bot_name=bot_name,
                    bot_logo_bytes=bot_logo,
                )
                if overlay_png:
                    video_bytes = await compose_story_video(canvas_bytes, overlay_png)
    except Exception:
        logger.exception("TSTORY_VIDEO_FAILED track=%s", track_id)

    if video_bytes:
        try:
            await message.answer_video(
                video=BufferedInputFile(video_bytes, filename=f"tstory-{canvas_track_id}.mp4"),
                caption=caption,
                parse_mode="HTML",
            )
            return
        except Exception:
            logger.exception("TSTORY_VIDEO_SEND_FAILED track=%s", track_id)

    # Fallback: card estático vertical (estilo do repo estudado).
    card = await render_tstory_full(
        cover_bytes=cover_bytes,
        listening=listening,
        title=title,
        artist=artist,
        bot_name=bot_name,
        bot_logo_bytes=bot_logo,
    )
    if card:
        await message.answer_photo(
            photo=BufferedInputFile(card, filename="tstory.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
        return

    # Último recurso (Playwright indisponível): manda só a capa ou o texto.
    if cover_bytes:
        await message.answer_photo(
            photo=BufferedInputFile(cover_bytes, filename="cover.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
    else:
        await message.answer(caption, parse_mode="HTML")
