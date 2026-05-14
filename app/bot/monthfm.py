from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.services.lastfm_capsule import lastfm_capsule_service
from app.services.monthfm_card import CardArtist, CardTrack, MonthfmCardData, render_monthfm_card

logger = logging.getLogger(__name__)
router = Router(name="monthfm")

TAG_RE = re.compile(r"<[^>]+>")


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.warning("monthfm status delete failed | message_id=%s", message.message_id, exc_info=True)


def _strip_html(value: str) -> str:
    return html.unescape(TAG_RE.sub("", value or "")).strip()


def _format_caption(display_name: str, user_id: int, raw_month: str | None) -> str:
    safe_name = html.escape(display_name or "Usuário")
    # Keep caption short because Telegram photo captions have a stricter limit.
    if raw_month:
        return f'♫ Extrato do mês de <a href="tg://user?id={user_id}">{safe_name}</a>'
    return f'♫ Extrato mensal de <a href="tg://user?id={user_id}">{safe_name}</a>'


def _parse_month_card_data(text: str, fallback_image: bytes | None = None) -> MonthfmCardData | None:
    lines = [_strip_html(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None

    header = lines[0]
    if "Extrato de " in header:
        title = "Extrato de " + header.split("Extrato de ", 1)[1].strip()
    elif "· ♫" in header:
        title = "Extrato de " + header.split("· ♫", 1)[1].strip()
    else:
        title = "Extrato mensal"

    artists: list[CardArtist] = []
    tracks: list[CardTrack] = []
    album_name = "Sem disco identificado"
    album_artist = "Last.fm"
    album_count = 0
    total_scrobbles = 0
    minutes: int | None = None
    section = ""

    for line in lines[1:]:
        if line.startswith("✦"):
            section = "artists"
            continue
        if line.startswith("♫"):
            section = "tracks"
            continue
        if line.startswith("◌"):
            section = "album"
            continue
        if line.startswith("⌁"):
            section = "total"
            continue

        if section == "artists":
            match = re.match(r"^\d+\.\s+(.+?)\s+—\s+([\d.]+)\s+scrobbles", line)
            if match:
                artists.append(CardArtist(name=match.group(1), count=int(match.group(2).replace(".", ""))))
        elif section == "tracks":
            match = re.match(r"^\d+\.\s+(.+?)\s+—\s+(.+?)\s+([\d.]+)\s+plays", line)
            if match:
                tracks.append(CardTrack(title=match.group(1), artist=match.group(2), plays=int(match.group(3).replace(".", ""))))
        elif section == "album":
            if album_name == "Sem disco identificado":
                album_name = line
            else:
                match = re.match(r"^(.+?)\s+·\s+([\d.]+)\s+scrobbles", line)
                if match:
                    album_artist = match.group(1)
                    album_count = int(match.group(2).replace(".", ""))
        elif section == "total":
            scrobble_match = re.match(r"^([\d.]+)\s+scrobbles", line)
            minute_match = re.search(r"([\d.]+)\s+minutos", line)
            if scrobble_match:
                total_scrobbles = int(scrobble_match.group(1).replace(".", ""))
            elif minute_match:
                minutes = int(minute_match.group(1).replace(".", ""))

    if not artists and not tracks:
        return None

    return MonthfmCardData(
        title=title,
        theme="dark",
        top_artists=tuple(artists[:5]),
        top_tracks=tuple(tracks[:5]),
        album_name=album_name,
        album_artist=album_artist,
        album_count=album_count,
        total_scrobbles=total_scrobbles,
        minutes=minutes,
        # The current capsule service returns collage bytes, not a reusable URL.
        # The visual card therefore uses its built-in gradient hero until the data layer exposes image URLs.
        hero_image_url=None,
    )


async def _try_render_visual_card(text: str) -> bytes | None:
    data = _parse_month_card_data(text)
    if data is None:
        return None
    return await render_monthfm_card(data)


async def _finish_monthfm(message: Message, user_id: int, display_name: str, raw_month: str | None) -> None:
    try:
        result = await lastfm_capsule_service.build_capsule(
            user_id=user_id,
            display_name=display_name,
            raw_month=raw_month,
        )
        text = result.text
        card_bytes = await _try_render_visual_card(text)
        if card_bytes:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(card_bytes, filename="monthfm-card.jpg"),
                caption=_format_caption(display_name, user_id, raw_month),
                parse_mode="HTML",
            )
            await message.answer(text, parse_mode="HTML")
            return
        if result.photo_bytes and len(text) <= 1024:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption=text,
                parse_mode="HTML",
            )
            return
        if result.photo_bytes:
            await _safe_delete(message)
            await message.answer_photo(
                photo=BufferedInputFile(result.photo_bytes, filename="monthfm.jpg"),
                caption="♫ Extrato mensal",
                parse_mode="HTML",
            )
            await message.answer(text, parse_mode="HTML")
            return
        if len(text) <= 3900:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.edit_text(text[:3900], parse_mode="HTML")
            await message.answer(text[3900:], parse_mode="HTML")
    except Exception:
        logger.exception("monthfm generation failed | user_id=%s | raw_month=%s", user_id, raw_month)
        try:
            await message.edit_text("Não consegui gerar a cápsula mensal agora. Tente novamente em alguns instantes.")
        except Exception:
            logger.exception("monthfm failure message failed | user_id=%s", user_id)


@router.message(Command("monthfm"))
async def monthfm(message: Message) -> None:
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_month = parts[1].strip() if len(parts) > 1 else None
    status = await message.answer("Gerando extrato mensal do Last.fm...")
    asyncio.create_task(
        _finish_monthfm(
            status,
            user_id=message.from_user.id,
            display_name=message.from_user.full_name or "Usuário",
            raw_month=raw_month,
        )
    )
