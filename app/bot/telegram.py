from __future__ import annotations

import html
import logging
import uuid

from aiogram import Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultPhoto,
    Message,
)

from app.bot.intent import detect_intent
from app.config.settings import LASTFM_API_KEY, OWNER_ID
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.lastfm import lastfm_service
from app.services.likes import likes_service
from app.services.music import music_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service

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


async def _resolve_play_button_count(user_id: int, track_id: str, artist: str | None, track_name: str | None) -> tuple[int, str]:
    if artist and track_name:
        lastfm_count = await lastfm_service.get_user_track_playcount(user_id, artist, track_name)
        if lastfm_count is not None:
            return lastfm_count, "lastfm"
    return await likes_service.get_track_play_count(track_id), "local"


async def build_playing_payload(
    message: Message, track: dict
) -> tuple[str, str, str | None, InlineKeyboardMarkup] | None:
    """Registra o play e monta (track_id, caption HTML, cover_url, keyboard).

    Side effect: chama `likes_service.register_play`. Retorna `None` se faltar
    `from_user` ou `track_id`. Reaproveitado por /playing e /tcanvas pra
    garantir mesma legenda + mesmos botões.
    """
    if not message.from_user:
        return None
    user_id = message.from_user.id
    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        return None

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
    return track_id, caption, cover, keyboard


async def _send_playing(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    if not is_user_connected(user_id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
        return
    track = await music_service.get_current_or_last_played(user_id)
    if not track:
        await message.answer(
            "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo.",
        )
        return

    payload = await build_playing_payload(message, track)
    if not payload:
        await message.answer("Erro ao identificar a música.")
        return
    _track_id, caption, cover, keyboard = payload

    if cover:
        await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


def _register_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "♫ ♥ <b>Bem-vindo ao tigraoRADIO</b>\n\n"
            "Conecte seu Last.fm e o bot acompanha o que você está ouvindo, "
            "gera extratos visuais e monta rankings do grupo.\n\n"
            "<b>Primeiro passo:</b> <code>/lastfm seu_username</code> (sem @)\n"
            "<b>Lista completa de comandos:</b> /help",
            parse_mode="HTML",
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "<b>COMANDOS</b>\n\n"
            "— TOCANDO AGORA —\n\n"
            "♫ /playing\n"
            "Mostra a música que VOCÊ está ouvindo agora (capa + nome + artista + álbum). "
            "Se nada estiver tocando, mostra a última registrada. Tem botões de like/dislike.\n\n"
            "◐ /albnow\n"
            "Foco no <b>álbum</b> da sua música atual: capa do álbum, nome, artista e ano. "
            "Útil quando você quer destacar o disco e não a faixa solta.\n\n"
            "▶ /tcanvas\n"
            "Pega o Spotify Canvas (aquele vídeo curto vertical em loop que toca no app do Spotify) "
            "da sua música atual e manda aqui. Se a faixa não tiver Canvas, cai automaticamente pra capa do álbum.\n\n"
            "◉ /tnow\n"
            "Mosaico ao vivo de quem está ouvindo o quê <b>neste grupo</b> agora.\n\n"
            "— EXTRATOS LAST.FM —\n\n"
            "★ /myself\n"
            "Porta de entrada do seu extrato pessoal. Abre um menu com dois botões: "
            "🟢 <b>Semanal</b>  |  🔴 <b>Mensal</b>. Gera um card visual com top artistas e músicas. "
            "Em grupo, só quem rodou o comando consegue clicar nos botões.\n\n"
            "— INTERAÇÃO —\n\n"
            "☻ /mood &lt;0-10&gt;\n"
            "Compartilha sua música atual com uma nota de humor (0 = horrível, 10 = paraíso). "
            "Ex.: <code>/mood 8</code>. Variante mais provocativa: adiciona <code>c</code> no número, ex.: <code>/mood 9c</code>.\n\n"
            "— CONEXÃO (LAST.FM) —\n\n"
            "↻ /lastfm &lt;username&gt;\n"
            "Conecta seu perfil <b>público</b> do Last.fm ao bot (sem o @). "
            "Ex.: se sua URL é <code>last.fm/user/romastefale</code>, manda <code>/lastfm romastefale</code>. "
            "SEM ISSO você não aparece em /tnow nem usa /playing, /tcanvas, /myself, /mood. "
            "Sem argumento, mostra qual username está salvo.\n\n"
            "⨯ /lastfmoff\n"
            "Remove o vínculo do seu Last.fm com o bot.",
            parse_mode="HTML",
        )

    @dp.message(Command("hidden"))
    async def hidden_command(message: Message) -> None:
        # OWNER-only e silencioso pra outros (mesmo padrão de /manual, /kingplay, /debuguser).
        if not message.from_user or message.from_user.id != OWNER_ID:
            return
        await message.answer(
            "<b>🔒 COMANDOS OCULTOS</b> — só você (dono) vê isso\n\n"
            "— SPOTIFY (uso restrito, &lt;5 pessoas) —\n\n"
            "🎧 /login\n"
            "Inicia o OAuth do Spotify. Só funciona em DM com o bot — em grupo, "
            "ele responde com instrução pra ir pro privado. Gera link de autorização; "
            "depois que autoriza, o Spotify volta como fallback de música pra quem não tem Last.fm.\n\n"
            "🎧 /logout\n"
            "Limpa a sessão Spotify do usuário no banco. Resposta seca: \"Spotify desconectado.\"\n\n"
            "— ATALHOS DOS BOTÕES DO /myself —\n\n"
            "Estes dois comandos existem mas <b>não são documentados publicamente</b>: "
            "a UX canônica é clicar nos botões 🟢 Semanal / 🔴 Mensal dentro do /myself. "
            "Ficam aqui só pra você lembrar que existem e poder digitar direto se quiser.\n\n"
            "◌ /weekfm\n"
            "Atalho direto pro extrato <b>semanal</b> do Last.fm (mesmo card do botão Semanal do /myself). "
            "Aceita data: <code>/weekfm</code>, <code>/weekfm 2026-05-06</code> ou "
            "<code>/weekfm 2026-05-06 2026-05-13</code>.\n\n"
            "◌ /monthfm\n"
            "Atalho direto pro extrato <b>mensal</b> (mesmo card do botão Mensal do /myself). "
            "Aceita: <code>/monthfm</code>, <code>/monthfm 05</code> ou <code>/monthfm 2026-05</code>.\n\n"
            "— MÚSICA ADMIN/OWNER —\n\n"
            "≡ /songcharts\n"
            "Ranking agregado do Last.fm:\n"
            "  • Em <b>grupo</b>: só admin/creator pode rodar. Mostra top 10 artistas + 10 músicas "
            "do grupo (botões pra escolher período). Card vai fixado automaticamente.\n"
            "  • Em <b>DM</b>: SÓ VOCÊ. Vira modo <b>global</b> — agrega TODOS os Last.fm conectados "
            "no bot, independente de grupo.\n\n"
            "♛ /kingplay\n"
            "Força-fixa sua música atual num grupo específico. Dois modos:\n"
            "  1) <code>/kingplay</code> (sem args) → painel com botões dos grupos conhecidos.\n"
            "  2) Multi-linha:\n"
            "     <code>/kingplay\n&lt;chat_id&gt;</code>\n"
            "     → envia direto pro grupo informado.\n"
            "Útil pra \"carimbar\" sua presença musical sem precisar entrar no grupo.\n\n"
            "🔎 /debuguser &lt;user_id&gt;\n"
            "Stats internas de qualquer usuário no banco: plays totais, likes recebidos/enviados "
            "e top 5 músicas dele. Ex.: <code>/debuguser 123456789</code>.\n\n"
            "— MODERAÇÃO / UTILIDADE OWNER —\n\n"
            "⚙ /tigrao\n"
            "Painel completo de moderação (só em DM com você). Menu FSM com: "
            "selecionar grupo (lista os conhecidos ou cola chat_id manual), "
            "ações no usuário (ban, mute, unmute, pin de mensagem), "
            "customizar grupo (título, bio, foto), ver logs, gerar links de convite, "
            "enviar mensagem em nome do bot. Toda interação por botões + estados de espera (texto/mídia).\n\n"
            "🤝 /btb (bot-to-bot)\n"
            "Relay pra controlar OUTROS bots (tipo @MissRose_bot) por dentro do tigraoRADIO. "
            "Você seleciona target bot + grupo alvo, configura modo/opções (cleanup, fallback, wait), "
            "e o tigraoRADIO dispara a sequência de comandos no destino. Tem allowlist por bot "
            "(btb:arm) pra evitar disparo acidental.\n\n"
            "🪪 /manual &lt;user_id&gt; &lt;lastfm_username&gt;\n"
            "Cadastra OUTRA pessoa no Last.fm manualmente (sem ela precisar mandar /lastfm). "
            "Aceita @, URL completa do Last.fm ou só o nome. Limpa registros antigos daquele user_id "
            "antes de gravar (transação atômica). Ex.: <code>/manual 123456789 @romastefale</code>.\n\n"
            "🔒 /hidden\n"
            "Este comando. Silencioso pra qualquer um que não seja você.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @dp.message(Command("login"))
    async def login(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer(
                "🔒 Pra conectar suas contas, fala comigo no privado:\n"
                "1) <code>/login</code> — autoriza o Spotify\n"
                "2) <code>/lastfm seu_username</code> (sem o @) — conecta o Last.fm\n\n"
                "Sem isso você não aparece no /tnow nem usa /monthfm, /weekfm e cia.",
                parse_mode="HTML",
            )
            return
        if not message.from_user:
            return
        auth_url = spotify_service.build_auth_url(message.from_user.id)
        await message.answer(
            "🎧 <b>Conectando suas contas no tigraoRADIO</b>\n\n"
            f"1) <b>Spotify</b> — abre este link e autoriza:\n{auth_url}\n\n"
            "2) <b>Last.fm</b> — manda aqui:\n"
            "<code>/lastfm seu_username</code>  (sem o @)\n\n"
            "Só depois desses dois passos seu nome entra no /tnow e os comandos "
            "/monthfm, /weekfm, /playing funcionam pra você.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

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
                await message.answer(
                    f"{mention}, seu Last.fm salvo é <b>@{html.escape(current)}</b>.\n"
                    "Pra trocar: <code>/lastfm outro_username</code> (sem o @).\n"
                    "Pra desconectar: /lastfmoff",
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    f"{mention}, você ainda não conectou um Last.fm.\n\n"
                    "🎧 <b>Como conectar:</b>\n"
                    "1) Abre seu perfil no Last.fm: https://www.last.fm/\n"
                    "2) Copia só o <b>username</b> (o que vem depois de /user/, <b>sem o @</b>)\n"
                    "3) Manda aqui: <code>/lastfm seu_username</code>\n\n"
                    "Exemplo: se sua URL é <code>last.fm/user/romastefale</code>, "
                    "manda <code>/lastfm romastefale</code>.\n\n"
                    "Sem isso você não aparece no /tnow nem usa /monthfm e /weekfm.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            return
        try:
            username, previous = await lastfm_service.set_username(message.from_user.id, parts[1])
        except ValueError:
            await message.answer(f"{mention}, username Last.fm inválido.", parse_mode="HTML")
            return
        if previous and previous.lower() == username.lower():
            head = f"{mention}, Last.fm reconfirmado: <b>@{html.escape(username)}</b>."
        elif previous:
            head = (
                f"{mention}, atualizei seu Last.fm de <b>@{html.escape(previous)}</b> "
                f"pra <b>@{html.escape(username)}</b>."
            )
        else:
            head = f"{mention}, Last.fm conectado: <b>@{html.escape(username)}</b>."
        if not LASTFM_API_KEY:
            await message.answer(
                f"{head}\n\n"
                "A leitura do Last.fm precisa da variável LASTFM_API_KEY no Railway. "
                "Enquanto ela não existir, o bot continua usando Spotify como fallback.",
                parse_mode="HTML",
            )
            return
        await message.answer(head, parse_mode="HTML")

    @dp.message(Command("manual"))
    async def manual(message: Message) -> None:
        # Comando de dono: cadastra outra pessoa no Last.fm.
        # Sem hipótese: só o OWNER_ID pode rodar; qualquer outro é ignorado em silêncio.
        if not message.from_user or message.from_user.id != OWNER_ID:
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer(
                "Uso: <code>/manual &lt;user_id&gt; &lt;lastfm_username&gt;</code>\n"
                "Aceita @, URL completa do Last.fm ou só o nome.\n"
                "Exemplo: <code>/manual 123456789 @romastefale</code>",
                parse_mode="HTML",
            )
            return
        raw_uid = parts[1].strip()
        try:
            target_uid = int(raw_uid)
        except ValueError:
            await message.answer(
                f"❌ <code>{html.escape(raw_uid)}</code> não é um Telegram user_id válido.",
                parse_mode="HTML",
            )
            return
        raw_username = " ".join(parts[2:]).strip()
        try:
            clean, deleted = await lastfm_service.manual_register(target_uid, raw_username)
        except ValueError:
            await message.answer(
                f"❌ Username Last.fm inválido: <code>{html.escape(raw_username)}</code>",
                parse_mode="HTML",
            )
            return
        except Exception:
            logger.exception("MANUAL_REGISTER_FAILED user_id=%s raw=%r", target_uid, raw_username)
            await message.answer(
                "❌ Erro ao cadastrar — nada foi alterado no banco (transação revertida).",
                parse_mode="HTML",
            )
            return
        cleanup_line = (
            f"🧹 Limpei {deleted} registro(s) antigo(s) desse user_id antes."
            if deleted
            else "🧹 Nenhuma sujeira antiga — slot estava limpo."
        )
        await message.answer(
            "✓ Cadastro manual concluído.\n"
            f"• user_id: <code>{target_uid}</code>\n"
            f"• Last.fm: <b>@{html.escape(clean)}</b>\n"
            f"{cleanup_line}",
            parse_mode="HTML",
        )

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
        if not is_user_connected(message.from_user.id):
            await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
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

    # /myself e /songcharts foram movidos pra `app/bot/myself.py` e
    # `app/bot/songcharts.py`. Os novos comandos usam Last.fm (em vez de
    # likes locais) e renderizam o mesmo card visual dos /weekfm e
    # /monthfm. O ranking de grupo (/songcharts) agrega todos os
    # membros conectados e fixa a mensagem.

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
        total_likes = await likes_service.get_total_likes(track_id, owner_user_id=owner_user_id)
        track_name, artist = await likes_service.get_track_metadata(track_id, owner_user_id=owner_user_id)
        total_plays, plays_source = await _resolve_play_button_count(owner_user_id, track_id, artist, track_name)
        try:
            await query.message.edit_reply_markup(reply_markup=_playing_keyboard(track_id, owner_user_id, total_plays, total_likes, liked, plays_source))  # type: ignore[union-attr]
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

    # IMPORTANTE: o filtro `~F.text.startswith("/")` impede que este handler
    # consuma comandos. Sem isso, qualquer texto começando com "/" (ex.:
    # /weekfm, /monthfm em sub-routers) bateria neste handler primeiro, o
    # `return` cedo devolveria None ao observer (que NÃO é UNHANDLED em
    # aiogram3), e a propagação para sub-routers seria abortada.
    # StateFilter(None) também evita interceptar texto durante FSM.
    @dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def text_aliases(message: Message) -> None:
        text = message.text or ""
        if detect_intent(text) == "play":
            await _send_playing(message)


async def shutdown_telegram_bot() -> None:
    await spotify_service.shutdown()
