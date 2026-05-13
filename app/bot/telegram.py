from __future__ import annotations

import html
import logging
import uuid

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultPhoto,
    Message,
)

from app.bot.intent import detect_intent
from app.config.settings import LASTFM_API_KEY
from app.services.lastfm import lastfm_service
from app.services.likes import likes_service
from app.services.music import music_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)
bot_dispatcher: Dispatcher = Dispatcher()

MOOD_PHRASES_NORMAL = {
    0: "☹︎ <i>Acho que <b>{name}</b> está no fundo de um abismo, onde até o silêncio pesa.</i>",
    1: "⍨ <i>Acho que <b>{name}</b> está preso em uma melancolia que drena até o que resta.</i>",
    2: "❃ <i>Acho que <b>{name}</b> está vagando em incertezas, tentando se reconhecer.</i>",
    3: "⚲ <i>Acho que <b>{name}</b> está lutando para manter acesa uma esperança.</i>",
    4: "✧ <i>Acho que <b>{name}</b> está começando a enxergar luz onde antes só havia peso.</i>",
    5: "ꕤ <i>Acho que <b>{name}</b> está em equilíbrio, sustentando o próprio centro.</i>",
    6: "✦ <i>Acho que <b>{name}</b> está retomando o controle e sentindo a força voltar.</i>",
    7: "❀ <i>Acho que <b>{name}</b> está florescendo, em paz com o presente.</i>",
    8: "✶ <i>Acho que <b>{name}</b> está irradiando energia que aquece tudo ao redor.</i>",
    9: "✵ <i>Acho que <b>{name}</b> está em êxtase, vibrando acima de tudo.</i>",
    10: "☻ <i>Acho que <b>{name}</b> está radiante, tomado por uma felicidade que transborda.</i>",
}
MOOD_PHRASES_CUNTY = {
    0: "☹︎ <i>Infelizmente <b>{name}</b> não está mal — queria nem existir mesmo.</i>",
    1: "⍨ <i>Dessa vez <b>{name}</b> está se arrastando por um dia que nem deveria ter existido.</i>",
    2: "❃ <i>Acho que <b>{name}</b> está fudido, mas sabe que vai dar um jeito.</i>",
    3: "⚲ <i>Acho que <b>{name}</b> está cansado de muito e de muitos, mas ainda não desistiu — vai ter volta.</i>",
    4: "✧ <i>Felizmente <b>{name}</b> está começando a reagir, o fim de alguns está previsto.</i>",
    5: "ꕤ <i>Acho que <b>{name}</b> está acordando — não por acaso, mas porque é uma gostosa resiliente.</i>",
    6: "✦ <i>Boatos que <b>{name}</b> está voltando, gostosas são assim, como uma fênix.</i>",
    7: "❀ <i>Soube que <b>{name}</b> está bem — e dessa vez, não haverá paz.</i>",
    8: "✶ <i>O <b>{name}</b> está brilhando de um jeito que incomoda, e quem tem inveja se queima.</i>",
    9: "✵ <i>Hoje <b>{name}</b> vai destruir alguém.</i>",
    10: "☻ <i>Tenho certeza que <b>{name}</b> tem poder para iniciar o novo apocalipse — apenas tome cuidado.</i>",
}


def _safe_button(text: str, callback: str, style: str | None = None) -> InlineKeyboardButton:
    try:
        if style:
            return InlineKeyboardButton(text=text, callback_data=callback, style=style)  # type: ignore[call-arg]
    except Exception:
        pass
    return InlineKeyboardButton(text=text, callback_data=callback)


def _playing_keyboard(
    track_id: str,
    owner_user_id: int,
    total_plays: int,
    total_likes: int,
    liked: bool,
    plays_source: str = "local",
) -> InlineKeyboardMarkup:
    heart = "♥" if liked else "♡"
    plays_style = "primary" if plays_source == "lastfm" else "success"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _safe_button(f"♫ {total_plays}", f"plays:{owner_user_id}:{plays_source}:{track_id}", style=plays_style),
                _safe_button(f"{heart} {total_likes}", f"like:{owner_user_id}:{track_id}", style="danger"),
            ]
        ]
    )


def _track_label(track: dict) -> tuple[str, str, str, str | None]:
    track_name = html.escape(str(track.get("track_name") or ""))
    artist = html.escape(str(track.get("artist") or ""))
    url = html.escape(str(track.get("spotify_url") or ""), quote=True)
    cover = track.get("album_image_url")
    return track_name, artist, url, str(cover) if cover else None


def _user_mention(message: Message) -> str:
    if not message.from_user:
        return "Usuário"
    display_name = html.escape(message.from_user.full_name or "Usuário")
    return f'<a href="tg://user?id={message.from_user.id}">{display_name}</a>'


async def _resolve_play_button_count(user_id: int, track_id: str, artist: str, track_name: str) -> tuple[int, str]:
    lastfm_count = await lastfm_service.get_user_track_playcount(user_id, artist, track_name)
    if lastfm_count is not None:
        return lastfm_count, "lastfm"
    return await likes_service.get_track_play_count(track_id), "local"


async def _send_playing(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    track = await music_service.get_current_or_last_played(user_id)
    if not track:
        await message.answer("Nada está tocando agora. Use /login para Spotify ou /lastfm <username> para Last.fm.")
        return

    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        await message.answer("Erro ao identificar a música.")
        return

    track_name_raw = str(track.get("track_name") or "").strip()
    artist_raw = str(track.get("artist") or "").strip()
    await likes_service.register_play(user_id, track_id, track_name=track_name_raw, artist_name=artist_raw)

    total_plays, plays_source = await _resolve_play_button_count(user_id, track_id, artist_raw, track_name_raw)
    total_likes = await likes_service.get_total_likes(track_id, owner_user_id=user_id)
    user_total_likes = await likes_service.get_user_received_likes(user_id)
    liked = await likes_service.is_track_liked(user_id, track_id, owner_user_id=user_id)

    display_name = html.escape(message.from_user.full_name or "Usuário")
    user_link = f"tg://user?id={user_id}"
    track_name, artist, track_url, cover = _track_label(track)
    track_part = f'<a href="{track_url}">{track_name}</a>' if track_url else track_name
    caption = (
        f"<b><a href=\"{html.escape(user_link)}\">{display_name}</a></b> · ♥ <code>{user_total_likes}</code>\n\n"
        f"♫ <b>{track_part}</b> — <i>{artist}</i>"
    )
    keyboard = _playing_keyboard(track_id, user_id, total_plays, total_likes, liked, plays_source)

    if cover:
        await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


def _register_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "♫ ♥ Bem-vindo ao tigraoRADIO\n\n"
            "Conecte sua conta e acompanhe o que você está ouvindo.\n\n"
            "Comandos principais:\n"
            "/playing — mostrar música atual\n"
            "/mood — analisar o clima da faixa atual\n"
            "/myself — ver seu perfil musical\n"
            "/songcharts — ver ranking do grupo\n\n"
            "Conexão:\n"
            "/login — conectar Spotify\n"
            "/lastfm <username> — conectar Last.fm\n"
            "/logout — desconectar Spotify\n"
            "/lastfmoff — desconectar Last.fm"
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "COMANDOS\n\n"
            "♫ /playing\n"
            "Mostra a música que você está ouvindo agora ou a última música encontrada.\n\n"
            "★ /myself\n"
            "Mostra seu perfil musical com top músicas, top artistas e total de curtidas.\n\n"
            "≡ /songcharts\n"
            "Mostra o ranking do grupo com músicas, artistas e faixas mais curtidas.\n\n"
            "☻ /mood <nota de 0 a 10>\n"
            "Manda a música e conte como está se sentindo.\n\n"
            "↻ /login\n"
            "Conecte sua conta do Spotify.\n\n"
            "↻ /lastfm <username>\n"
            "Conecte seu Last.fm público.\n\n"
            "⨯ /logout\n"
            "Desconecte sua conta Spotify.\n\n"
            "⨯ /lastfmoff\n"
            "Remove o Last.fm vinculado."
        )

    @dp.message(Command("login"))
    async def login(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("🔒 Use /login no privado para conectar seu Spotify.")
            return
        if not message.from_user:
            return
        await message.answer(f"Authorize Spotify access: {spotify_service.build_auth_url(message.from_user.id)}")

    @dp.message(Command("logout"))
    async def logout(message: Message) -> None:
        if not message.from_user:
            return
        await spotify_service.clear_user_session(message.from_user.id)
        await message.answer("Spotify desconectado.")

    @dp.message(Command("lastfm"))
    async def lastfm(message: Message) -> None:
        if not message.from_user:
            return
        mention = _user_mention(message)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            current = await lastfm_service.get_username(message.from_user.id)
            if current:
                await message.answer(f"{mention}, seu Last.fm salvo é @{html.escape(current)}.", parse_mode="HTML")
            else:
                await message.answer(f"{mention}, use: /lastfm <username>", parse_mode="HTML")
            return
        try:
            username = await lastfm_service.set_username(message.from_user.id, parts[1])
        except ValueError:
            await message.answer(f"{mention}, username Last.fm inválido.", parse_mode="HTML")
            return
        if not LASTFM_API_KEY:
            await message.answer(
                f"{mention}, Last.fm salvo: @{html.escape(username)}\n\n"
                "A leitura do Last.fm precisa da variável LASTFM_API_KEY no Railway. Enquanto ela não existir, o bot continua usando Spotify como fallback.",
                parse_mode="HTML",
            )
            return
        await message.answer(f"{mention}, Last.fm salvo: @{html.escape(username)}", parse_mode="HTML")

    @dp.message(Command("lastfmoff"))
    async def lastfmoff(message: Message) -> None:
        if not message.from_user:
            return
        mention = _user_mention(message)
        removed = await lastfm_service.clear_username(message.from_user.id)
        await message.answer(
            f"{mention}, Last.fm removido." if removed else f"{mention}, nenhum Last.fm estava conectado.",
            parse_mode="HTML",
        )

    @dp.message(Command("playing"))
    async def playing(message: Message) -> None:
        await _send_playing(message)

    @dp.message(Command("mood"))
    async def mood(message: Message) -> None:
        if not message.from_user:
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Erro: valor inválido.\nUse: /mood <0-10>")
            return
        raw = parts[1]
        mode = "cunty" if raw.endswith("c") else "normal"
        raw = raw[:-1] if raw.endswith("c") else raw
        try:
            score = int(raw)
        except ValueError:
            await message.answer("Erro: valor inválido.\nUse: /mood <0-10>")
            return
        if score < 0 or score > 10:
            await message.answer("Erro: valor inválido.\nUse: /mood <0-10>")
            return
        track = await music_service.get_current_or_last_played(message.from_user.id)
        if not track:
            return
        display_name = html.escape(message.from_user.full_name or "Usuário")
        track_name, artist, _, cover = _track_label(track)
        phrase = (MOOD_PHRASES_CUNTY if mode == "cunty" else MOOD_PHRASES_NORMAL)[score].format(name=display_name)
        caption = f'<a href="tg://user?id={message.from_user.id}">{display_name}</a> · ♫ {track_name} — {artist}\n\n{phrase}'
        if cover:
            await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    @dp.message(Command("myself"))
    async def myself(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        safe_name = html.escape(message.from_user.full_name or "Usuário")
        total_likes = await likes_service.get_user_total_likes(user_id)
        top_tracks = await likes_service.get_user_top_tracks(user_id, limit=5)
        top_artists = await likes_service.get_user_top_artists(user_id, limit=5)
        tracks = ["♫ Músicas"] + [f"♫ {i}. {name} — {plays}" for i, (name, plays) in enumerate(top_tracks, 1)]
        artists = ["★ Artistas"] + [f"★ {i}. {name} — {plays}" for i, (name, plays) in enumerate(top_artists, 1)]
        await message.answer(
            f"<a href='tg://user?id={user_id}'>{safe_name}</a> · ♥ {total_likes} curtidas\n\n"
            f"{chr(10).join(tracks)}\n\n{chr(10).join(artists)}",
            parse_mode="HTML",
        )

    @dp.message(Command("songcharts"))
    async def songcharts(message: Message) -> None:
        top_tracks = await likes_service.get_top_tracks(limit=5)
        top_artists = await likes_service.get_top_artists(limit=5)
        liked = await likes_service.get_most_liked_tracks(limit=5)
        tracks = ["♫ Músicas"] + [f"♫ {i}. {name} — {plays}" for i, (name, plays) in enumerate(top_tracks, 1)]
        artists = ["★ Artistas"] + [f"★ {i}. {name} — {plays}" for i, (name, plays) in enumerate(top_artists, 1)]
        likes = ["♥ Mais curtidas"] + [f"♥ {i}. {name} — {count}" for i, (name, count) in enumerate(liked, 1)]
        await message.answer("♫ Ranking do grupo\n\n" + "\n\n".join(["\n".join(tracks), "\n".join(artists), "\n".join(likes)]))

    @dp.callback_query(F.data.startswith("plays:"))
    async def plays_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data:
            return
        parts = query.data.split(":", 3)
        if len(parts) == 4:
            try:
                owner_user_id = int(parts[1])
            except ValueError:
                owner_user_id = query.from_user.id
            plays_source = parts[2]
            track_id = parts[3]
        elif len(parts) == 3:
            try:
                owner_user_id = int(parts[1])
            except ValueError:
                owner_user_id = query.from_user.id
            plays_source = "local"
            track_id = parts[2]
        else:
            owner_user_id = query.from_user.id
            plays_source = "local"
            track_id = query.data.split(":", 1)[1]
        count = await likes_service.get_user_play_count(owner_user_id, track_id)
        if plays_source == "lastfm":
            await query.answer("O número azul é o total do Last.fm.\nPelo bot: " + str(count) + " vez" + ("" if count == 1 else "es") + ".", show_alert=True)
        else:
            await query.answer(f"O dono já ouviu {count} vez" + ("" if count == 1 else "es") + " pelo bot.", show_alert=True)

    @dp.callback_query(F.data.startswith("like:"))
    async def like_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data:
            return
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            await query.answer()
            return
        try:
            owner_user_id = int(parts[1])
        except ValueError:
            await query.answer()
            return
        track_id = parts[2]
        liked = await likes_service.toggle_track_like(query.from_user.id, owner_user_id, track_id)
        local_plays = await likes_service.get_track_play_count(track_id)
        total_likes = await likes_service.get_total_likes(track_id, owner_user_id=owner_user_id)
        try:
            await query.message.edit_reply_markup(reply_markup=_playing_keyboard(track_id, owner_user_id, local_plays, total_likes, liked, "local"))  # type: ignore[union-attr]
        except Exception:
            logger.exception("Failed to edit like markup")
        await query.answer()

    @dp.inline_query()
    async def inline_play(query: InlineQuery) -> None:
        if (query.query or "").strip().lower() != "playing":
            return
        track = await music_service.get_current_or_last_played(query.from_user.id)
        if not track:
            await query.answer([], cache_time=1, is_personal=True)
            return
        track_name, artist, track_url, cover = _track_label(track)
        if not cover:
            await query.answer([], cache_time=1, is_personal=True)
            return
        caption = f"<i>{html.escape(query.from_user.full_name or 'Usuário')} · ♫ <a href=\"{track_url}\">{track_name}</a> - {artist}</i>"
        result = InlineQueryResultPhoto(
            id=str(uuid.uuid4()),
            photo_url=cover,
            thumbnail_url=cover,
            caption=caption,
            parse_mode="HTML",
        )
        await query.answer([result], cache_time=2, is_personal=True)

    @dp.message(F.text)
    async def text_aliases(message: Message) -> None:
        text = message.text or ""
        if text.lstrip().startswith("/"):
            return
        if detect_intent(text) == "play":
            await _send_playing(message)


async def shutdown_telegram_bot() -> None:
    await spotify_service.shutdown()
