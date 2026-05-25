from __future__ import annotations

import html
import logging
import uuid

from aiogram import Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultPhoto,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    MessageReactionUpdated,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot.intent import detect_intent
from app.bot.filters import IsOwner
from app.config.settings import LASTFM_API_KEY
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.lastfm import lastfm_service
from app.services.likes import likes_service
from app.services.music import music_service
from app.services.reactions import reactions_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)
bot_dispatcher: Dispatcher = Dispatcher()


# Sprint 9 (#8): IDs públicos de Message Effects (Premium / Bot API 7.7+).
# Telegram só aplica em chats privados; em grupos é silenciosamente
# ignorado. Wrap em try/except no caller pra cair pra send normal se
# o ID for inválido pra esse user/região (ex: Premium-only effects).
_EFFECT_FIRE = "5104841245755180586"      # 🔥
_EFFECT_PARTY = "5046509860389126442"     # 🎉
_EFFECT_THUMBS_UP = "5107584321108051014"  # 👍

# Sprint 9 (#5): request_id estável pro botão RequestUsers do /manual.
_MANUAL_REQUEST_USER_ID = 1001

# Sprint 10: emojis pra bot reagir nos próprios cards de música.
# Telegram restringe reactions de bots não-Premium à lista oficial
# (👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 ⚡ 💯 🏆 ❤‍🔥 etc) —
# 🎵 e 🎶 NÃO entram. 🔥 é o melhor proxy "musical/energético"; ❤
# marca milestone a cada 5 plays Last.fm.
_CARD_EMOJI_DEFAULT = "🔥"
_CARD_EMOJI_LOVED = "❤"
_LOVED_PLAYS_THRESHOLD = 5


def _pick_card_emoji(total_plays: int, plays_source: str) -> str:
    """Decide emoji do bot pro card. Múltiplo de 5 plays Last.fm = ❤; resto = 🔥."""
    if (
        plays_source == "lastfm"
        and total_plays > 0
        and total_plays % _LOVED_PLAYS_THRESHOLD == 0
    ):
        return _CARD_EMOJI_LOVED
    return _CARD_EMOJI_DEFAULT


async def _react_to_own_card(bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Sprint 10: bot reage no card que ele mesmo enviou. Silencioso em falha
    (bot pode não ter permissão de reagir, ou emoji rejeitado pela região)."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        logger.debug(
            "OWN_CARD_REACT_FAILED chat=%s msg=%s emoji=%s",
            chat_id, message_id, emoji, exc_info=True,
        )


async def _answer_with_effect(message: Message, text: str, effect_id: str, **kwargs) -> Message:
    """Sprint 9 (#8): tenta enviar com message_effect_id; cai pra send normal em falha.

    Effects só funcionam em DM. Em grupo o Telegram costuma ignorar
    silenciosamente, mas algumas versões rejeitam — try/except garante
    que o user sempre recebe a mensagem.
    """
    if message.chat.type == "private":
        try:
            return await message.answer(text, message_effect_id=effect_id, **kwargs)
        except Exception:
            logger.debug("EFFECT_SEND_FAILED effect_id=%s", effect_id, exc_info=True)
    return await message.answer(text, **kwargs)


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


async def build_playing_payload_for_user(
    user_id: int, display_name_raw: str, track: dict
) -> tuple[str, str, str | None, None, str] | None:
    """Variante que aceita user_id/display_name explícitos.

    Usada por /nowp (envio remoto via callback, onde `message.from_user` seria
    o bot e não o user real). Mesma lógica/saída de `build_playing_payload`.

    Registra a play ANTES de resolver counts/likes pra que o counter exibido
    no botão reflita o estado atualizado (inclui a play recém-registrada).
    Em caso de falha no envio posterior, ficaria um "phantom play" no DB —
    trade-off aceito porque o oposto (counter N-1 visível) é pior pro user.
    """
    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        return None

    track_name_raw = str(track.get("track_name") or "").strip()
    artist_raw = str(track.get("artist") or "").strip()
    try:
        await likes_service.register_play(user_id, track_id, track_name=track_name_raw, artist_name=artist_raw)
    except Exception:
        logger.exception("REGISTER_PLAY_FAILED user=%s track=%s", user_id, track_id)

    total_plays, plays_source = await _resolve_play_button_count(user_id, track_id, artist_raw, track_name_raw)
    total_likes = await likes_service.get_total_likes(track_id, owner_user_id=user_id)
    user_total_likes = await likes_service.get_user_received_likes(user_id)
    liked = await likes_service.is_track_liked(user_id, track_id, owner_user_id=user_id)

    display_name = html.escape(display_name_raw or "Usuário")
    user_link = f"tg://user?id={user_id}"
    track_name, artist, track_url, cover = _track_label(track)
    track_part = f'<a href="{track_url}">{track_name}</a>' if track_url else track_name
    # Sprint 8: caption agora mostra "♫ N · track — artist" onde N é o
    # playcount per-user (Last.fm ou local). Botões ♫ plays e ♥ likes
    # removidos — substituídos por reactions nativas do Telegram (count
    # via @dp.message_reaction + tabela track_reactions). Layout +clean.
    # NOTA: total_likes/liked/plays_source ainda calculados acima pra
    # preservar compatibilidade com `register_play` (side effect) e o
    # ♥ user_total_likes da linha 1 (legacy, dados históricos).
    _ = (total_likes, liked)  # mantém vars pra clareza/grep
    caption = (
        f"<b><a href=\"{html.escape(user_link)}\">{display_name}</a></b> · ♥ <code>{user_total_likes}</code>\n\n"
        f"♫ <code>{total_plays}</code> · <b>{track_part}</b> — <i>{artist}</i>"
    )
    # Sprint 10: emoji vai pro 5º slot do tuple — callers usam pra
    # set_message_reaction depois de enviar o card.
    card_emoji = _pick_card_emoji(total_plays, plays_source)
    return track_id, caption, cover, None, card_emoji


async def build_playing_payload(
    message: Message, track: dict
) -> tuple[str, str, str | None, None, str] | None:
    """Registra o play e monta (track_id, caption HTML, cover_url, keyboard).

    Side effect: chama `likes_service.register_play`. Retorna `None` se faltar
    `from_user` ou `track_id`. Reaproveitado por /playing e /tcanvas pra
    garantir mesma legenda + mesmos botões.
    """
    if not message.from_user:
        return None
    return await build_playing_payload_for_user(
        message.from_user.id,
        message.from_user.full_name or "Usuário",
        track,
    )


async def _safe_delete(message: Message) -> None:
    """Tenta deletar a mensagem. Falha em silêncio se bot não tem permissão
    (Forbidden/BadRequest). Usado pra limpar comandos/gatilhos em grupos."""
    try:
        await message.delete()
    except Exception:
        logger.debug("SAFE_DELETE_FAILED chat=%s msg=%s", message.chat.id, message.message_id, exc_info=True)


async def _send_playing(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    if not is_user_connected(user_id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
        return

    # U3: em grupo, apaga o comando/gatilho pra não poluir a conversa.
    # O card da música vira a única mensagem visível (igual /nowp).
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        await _safe_delete(message)

    # U1: feedback nativo "enviando foto..." enquanto resolve track + monta payload.
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass

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
    track_id, caption, cover, keyboard, card_emoji = payload

    if cover:
        sent = await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        sent = await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)

    # Sprint 8: registra (chat_id, message_id) -> track pra que reactions
    # nativas dos users virem stats. Fire-and-forget interno (service
    # captura exceções), nunca quebra o envio do card.
    await reactions_service.register_card(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        track_id=track_id,
        owner_user_id=user_id,
        track_name=str(track.get("track_name") or "").strip() or None,
        artist_name=str(track.get("artist") or "").strip() or None,
    )
    # Sprint 10: bot reage no próprio card (🔥 padrão, ❤ a cada 5 plays Last.fm).
    await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)


def _register_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        # Sprint 9 (#3): deep links via /start <payload>.
        # Payloads suportados:
        #   - lastfm_<username> → tenta conectar Last.fm direto
        #     (URL: t.me/<bot>?start=lastfm_romastefale)
        #   - connect           → mostra fluxo de conexão (Last.fm + Spotify)
        #   - help              → atalho pra /help
        # Sem payload (ou payload desconhecido) → greeting padrão.
        # Validação: payload é alfanumérico + underscore, max 64 chars
        # (limite do próprio start_parameter do Telegram).
        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) >= 2 else ""

        if payload.startswith("lastfm_") and message.from_user:
            raw_username = payload[len("lastfm_"):]
            mention = _user_mention(message)
            if not raw_username:
                await message.answer(
                    f"{mention}, link inválido (faltou o username).",
                    parse_mode="HTML",
                )
                return
            try:
                username, previous = await lastfm_service.set_username(
                    message.from_user.id, raw_username
                )
            except ValueError:
                await message.answer(
                    f"{mention}, username Last.fm inválido: "
                    f"<code>{html.escape(raw_username)}</code>",
                    parse_mode="HTML",
                )
                return
            if previous and previous.lower() == username.lower():
                head = f"{mention}, Last.fm reconfirmado: <b>@{html.escape(username)}</b>."
            elif previous:
                head = (
                    f"{mention}, atualizei seu Last.fm de "
                    f"<b>@{html.escape(previous)}</b> pra <b>@{html.escape(username)}</b>."
                )
            else:
                head = f"{mention}, Last.fm conectado: <b>@{html.escape(username)}</b>."
            await _answer_with_effect(message, head, _EFFECT_FIRE, parse_mode="HTML")
            return

        if payload == "connect":
            await message.answer(
                "🎧 <b>Conectar suas contas no tigraoRADIO</b>\n\n"
                "<b>Last.fm</b> (obrigatório pra extratos):\n"
                "<code>/lastfm seu_username</code> (sem @)\n\n"
                "<b>Spotify</b> (opcional, fallback):\n"
                "<code>/login</code>\n\n"
                "Depois disso, <code>/playing</code> mostra o que você está ouvindo.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        if payload == "help":
            # Redireciona pro mesmo conteúdo do /help.
            await help_command(message)  # type: ignore[name-defined]
            return

        # Sprint 9 (#8): greeting padrão ganha efeito 🎉 (DM only, fallback OK).
        await _answer_with_effect(
            message,
            "♫ ♥ <b>Bem-vindo ao tigraoRADIO</b>\n\n"
            "Conecte seu Last.fm e o bot acompanha o que você está ouvindo, "
            "gera extratos visuais e monta rankings do grupo.\n\n"
            "<b>Primeiro passo:</b> <code>/lastfm seu_username</code> (sem @)\n"
            "<b>Lista completa de comandos:</b> /help",
            _EFFECT_PARTY,
            parse_mode="HTML",
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        # Sprint 10: effect FIRE em DM (toda vez), graceful fallback em grupo.
        await _answer_with_effect(
            message,
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
            "✈ /nowp\n"
            "Envia sua música atual (mesmo formato do /playing) pra um grupo onde "
            "você e o bot estão juntos, <b>sem precisar entrar no grupo</b>. "
            "Roda no privado, mostra a lista dos grupos em comum, você escolhe e o bot publica lá. "
            "Confirma no privado com o nome do grupo onde foi enviado.\n\n"
            "— EXTRATOS LAST.FM —\n\n"
            "★ /myself\n"
            "Porta de entrada do seu extrato pessoal. Abre um menu com dois botões: "
            "🟢 <b>Semanal</b>  |  🔴 <b>Mensal</b>. Gera um card visual com top artistas e músicas. "
            "Em grupo, só quem rodou o comando consegue clicar nos botões.\n\n"
            "— CONEXÃO (LAST.FM) —\n\n"
            "↻ /lastfm &lt;username&gt;\n"
            "Conecta seu perfil <b>público</b> do Last.fm ao bot (sem o @). "
            "Ex.: se sua URL é <code>last.fm/user/romastefale</code>, manda <code>/lastfm romastefale</code>. "
            "SEM ISSO você não aparece em /tnow nem usa /playing, /tcanvas, /myself. "
            "Sem argumento, mostra qual username está salvo.\n\n"
            "⨯ /lastfmoff\n"
            "Remove o vínculo do seu Last.fm com o bot.",
            _EFFECT_FIRE,
            parse_mode="HTML",
        )

    @dp.message(Command("hidden"), IsOwner())
    async def hidden_command(message: Message) -> None:
        # S3: OWNER-only via filter IsOwner (silencioso pra não-owners).
        # Mesmo padrão de /manual, /kingplay, /debuguser.
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
        # Sprint 9 (#8): conexão bem-sucedida ganha efeito 🔥 (DM only, fallback OK).
        await _answer_with_effect(message, head, _EFFECT_FIRE, parse_mode="HTML")

    @dp.message(Command("manual"), IsOwner())
    async def manual(message: Message) -> None:
        # Comando de dono: cadastra outra pessoa no Last.fm.
        # S3: OWNER-only via filter IsOwner (silencioso pra não-owners).
        parts = (message.text or "").split()
        if len(parts) < 3:
            # Sprint 9 (#5): sem args → keyboard RequestUsers (DM only)
            # captura user_id nativamente. Em grupo, mantém comportamento
            # antigo (texto). Falha graceful pra versões aiogram antigas.
            if message.chat.type == "private":
                try:
                    request_btn = KeyboardButton(
                        text="👤 Escolher usuário",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=_MANUAL_REQUEST_USER_ID,
                            user_is_bot=False,
                            max_quantity=1,
                        ),
                    )
                    kb = ReplyKeyboardMarkup(
                        keyboard=[[request_btn]],
                        resize_keyboard=True,
                        one_time_keyboard=True,
                    )
                    await message.answer(
                        "Uso completo: <code>/manual &lt;user_id&gt; &lt;lastfm_username&gt;</code>\n"
                        "Aceita @, URL completa do Last.fm ou só o nome.\n"
                        "Exemplo: <code>/manual 123456789 @romastefale</code>\n\n"
                        "Ou clica abaixo pra escolher o usuário nativamente:",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    return
                except Exception:
                    logger.debug("MANUAL_REQUEST_USERS_KB_FAILED", exc_info=True)
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
        # Sprint 10: effect PARTY em DM (toda vez owner cadastra manual).
        await _answer_with_effect(
            message,
            "✓ Cadastro manual concluído.\n"
            f"• user_id: <code>{target_uid}</code>\n"
            f"• Last.fm: <b>@{html.escape(clean)}</b>\n"
            f"{cleanup_line}",
            _EFFECT_PARTY,
            parse_mode="HTML",
        )

    @dp.message(F.users_shared, IsOwner())
    async def on_users_shared(message: Message) -> None:
        """Sprint 9 (#5): captura user_id do botão RequestUsers do /manual.

        Owner clica "Escolher usuário" no keyboard → Telegram envia
        message com users_shared. Validamos request_id pra garantir
        que veio do nosso botão (não de outro keyboard). Owner-only
        via filter (silencioso pra não-owners). Não armazena estado:
        owner copia o ID e roda /manual completo manualmente.
        """
        shared = message.users_shared
        if not shared or shared.request_id != _MANUAL_REQUEST_USER_ID:
            return
        # aiogram3 expõe .users (list[SharedUser]) em versões novas e
        # .user_ids (list[int]) em versões antigas. Tratamento defensivo.
        target_id: int | None = None
        users_attr = getattr(shared, "users", None)
        if users_attr:
            try:
                target_id = int(users_attr[0].user_id)
            except (AttributeError, IndexError, ValueError, TypeError):
                target_id = None
        if target_id is None:
            ids_attr = getattr(shared, "user_ids", None)
            if ids_attr:
                try:
                    target_id = int(ids_attr[0])
                except (IndexError, ValueError, TypeError):
                    target_id = None
        if target_id is None:
            await message.answer(
                "Não consegui ler o usuário escolhido.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        await message.answer(
            f"✓ User ID capturado: <code>{target_id}</code>\n\n"
            f"Agora roda:\n<code>/manual {target_id} &lt;lastfm_username&gt;</code>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("lastfmoff"))
    async def lastfmoff(message: Message) -> None:
        if not message.from_user:
            return
        mention = _user_mention(message)
        removed = await lastfm_service.clear_username(message.from_user.id)
        # Sprint 10: effect THUMBS_UP em DM (toda vez), graceful em grupo.
        await _answer_with_effect(
            message,
            f"{mention}, Last.fm removido." if removed else f"{mention}, nenhum Last.fm estava conectado.",
            _EFFECT_THUMBS_UP,
            parse_mode="HTML",
        )

    @dp.message(Command("playing"))
    async def playing(message: Message) -> None:
        await _send_playing(message)

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
        # Sprint 8: botão ♥ likes foi removido — substituído por reactions
        # nativas. Stub silencioso pra mensagens ANTIGAS (cards postados
        # antes do deploy) que ainda têm o botão pendurado. Sem isso, o
        # clique falharia com "callback expired" e o user veria erro.
        await query.answer("Agora curtir é só reagir na mensagem 👀")

    @dp.message_reaction()
    async def on_message_reaction(event: MessageReactionUpdated) -> None:
        """Sprint 8: tracking de reactions nos cards /playing.

        Telegram envia este update toda vez que um user adiciona/remove
        reaction numa mensagem em grupo onde o bot é admin. Se a mensagem
        for um card trackado (existe em card_messages), grava o diff em
        track_reactions. Caso contrário, ignora silenciosamente.
        """
        if not event.user:
            return  # reaction anônima (rara) ou de canal — ignora
        old_emojis = [
            r.emoji for r in (event.old_reaction or [])
            if hasattr(r, "emoji") and getattr(r, "emoji", None)
        ]
        new_emojis = [
            r.emoji for r in (event.new_reaction or [])
            if hasattr(r, "emoji") and getattr(r, "emoji", None)
        ]
        try:
            await reactions_service.apply_reaction_change(
                chat_id=event.chat.id,
                message_id=event.message_id,
                user_id=event.user.id,
                old_emojis=old_emojis,
                new_emojis=new_emojis,
            )
        except Exception:
            logger.exception(
                "MESSAGE_REACTION_FAILED chat=%s msg=%s user=%s",
                event.chat.id, event.message_id, event.user.id,
            )

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
            # U3: _send_playing já deleta a mensagem em grupo (gatilho some
            # antes do card aparecer). Em DM, mantém o texto do user.
            await _send_playing(message)


async def shutdown_telegram_bot() -> None:
    await spotify_service.shutdown()
