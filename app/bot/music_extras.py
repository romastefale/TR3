from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import text

from app.bot.filters import IsOwner
from app.db.database import SessionLocal
from app.moderation_tigrao.storage import list_groups
from app.services.likes import likes_service
from app.services.music import music_service

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
            # Sprint 4 (S4.2): aiogram pode não suportar `style=` (depende
            # da versão / API do Telegram). Detecção em runtime + fallback
            # silencioso por design — DEBUG basta pra investigar se for
            # preciso (em prod usa fallback toda vez sem ruído no log).
            logger.debug("InlineKeyboardButton style fallback | text=%s", text, exc_info=True)
    return InlineKeyboardButton(text=text, callback_data=callback_data)


_NOWP_MEMBER_CHECK_CONCURRENCY = 10


async def _list_common_groups(bot, user_id: int) -> list[dict]:
    """Grupos conhecidos onde o user TAMBÉM é membro (bot já é, por estar registrado).

    Faz `get_chat_member` por grupo em paralelo (Semaphore evita flood). Filtra
    status 'left'/'kicked' e 'restricted' sem is_member. Antes era serial e
    levava 5-15s pra 50 grupos; agora <1s.
    """
    groups = list_groups(50)
    sem = asyncio.Semaphore(_NOWP_MEMBER_CHECK_CONCURRENCY)

    async def _check(group: dict) -> dict | None:
        try:
            chat_id = int(group["chat_id"])
        except Exception:
            return None
        async with sem:
            try:
                member = await bot.get_chat_member(chat_id, user_id)
            except Exception:
                return None
        status = getattr(member, "status", None)
        if status in ("left", "kicked"):
            return None
        if status == "restricted" and not getattr(member, "is_member", True):
            return None
        return group

    checked = await asyncio.gather(*(_check(g) for g in groups))
    return [g for g in checked if g is not None]


def _nowp_groups_keyboard(requester_id: int, groups: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups[:10]:
        try:
            chat_id = int(group["chat_id"])
        except Exception:
            continue
        # Telegram caps callback_data em 64 bytes. requester_id (Telegram ID,
        # até 19 dígitos no futuro) + chat_id de supergrupo negativo longo
        # (ex: -1001234567890123) podem estourar em edge cases. Se estourar,
        # pula o botão (mensagem do picker ainda funciona, só esse grupo fica
        # de fora). Sem fallback complexo de índice — simplicidade > 0.1% de cobertura.
        callback_data = f"nowp:send:{requester_id}:{chat_id}"
        if len(callback_data.encode("utf-8")) > 64:
            logger.warning(
                "NOWP_CALLBACK_TOO_LONG | requester=%s chat_id=%s len=%s",
                requester_id, chat_id, len(callback_data.encode("utf-8")),
            )
            continue
        title = str(group.get("title") or chat_id)
        label = title if len(title) <= 40 else title[:37] + "..."
        rows.append([_safe_button(label, callback_data, "primary")])
    rows.append([_safe_button("Fechar", f"nowp:close:{requester_id}", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def _send_kingplay(message: Message, target_chat_id: int, owner_user_id: int, owner_display_name: str) -> bool:
    try:
        chat = await message.bot.get_chat(target_chat_id)
        group_name_raw = _normalize_optional_text(chat.title)
    except Exception:
        group_name_raw = _normalize_optional_text(str(target_chat_id))

    try:
        # Sprint 3.5: music_service (Last.fm-first) substitui o caminho
        # via spotify_service que dependia do monkey-patch do music_proxy.
        track = await music_service.get_current_or_last_played(owner_user_id)
    except Exception:
        logger.exception("Falha no /kingplay | owner_user_id=%s", owner_user_id)
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

    safe_owner = html.escape(owner_display_name or str(owner_user_id))
    # Sprint 5 (S5.01): removido `target_chat_id` e `sent.message_id` da
    # resposta — antes vazava topologia de grupos pra quem visse o print
    # ou log. Confirmação fica enxuta; quem precisa do ID consulta logs
    # do bot (que mantêm contexto completo via logger.exception em falha).
    await message.answer(
        f"Kingplay enviado.\nDono: {safe_owner}",
        parse_mode="HTML",
    )
    return True


def register_music_extra_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("albnow"))
    async def albnow(message: Message) -> None:
        if not message.from_user:
            return
        from app.services.connection_check import connect_hint_for, is_user_connected
        if not is_user_connected(message.from_user.id):
            await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
            return
        # Sprint 3.5: usa music_service (Last.fm-first) pra manter o
        # comportamento do antigo music_proxy.
        data = await music_service.get_current_or_last_played(message.from_user.id)
        if not data:
            await message.answer("Nada tocando agora.")
            return
        caption = _format_albnow(message.from_user.full_name, data)
        cover = data.get("album_image_url") or data.get("cover_url")
        if cover:
            await message.answer_photo(photo=str(cover), caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    @dp.message(Command("nowp"))
    async def nowp(message: Message) -> None:
        # Público: qualquer user pode rodar. Mostra picker dos grupos em comum
        # (bot + user). Envio efetivo via callback nowp:send:<uid>:<chat_id>.
        if not message.from_user or not message.bot:
            return
        from app.services.connection_check import connect_hint_for, is_user_connected
        if not is_user_connected(message.from_user.id):
            await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
            return
        status_msg = await message.answer("Procurando grupos em comum...")
        common = await _list_common_groups(message.bot, message.from_user.id)
        if not common:
            await status_msg.edit_text(
                "Nenhum grupo em comum encontrado.\n"
                "(O bot precisa estar nos mesmos grupos que você e ter recebido pelo menos uma mensagem lá.)"
            )
            return
        await status_msg.edit_text(
            "♫ Pra qual grupo enviar sua música atual?",
            reply_markup=_nowp_groups_keyboard(message.from_user.id, common),
        )

    @dp.callback_query(F.data.startswith("nowp:send:"))
    async def nowp_send_callback(query: CallbackQuery) -> None:
        from app.bot.telegram import build_playing_payload_for_user
        if not query.from_user or not query.data or not query.message or not query.bot:
            await query.answer()
            return
        parts = query.data.split(":")
        if len(parts) != 4:
            await query.answer()
            return
        try:
            requester_id = int(parts[2])
            target_chat_id = int(parts[3])
        except ValueError:
            await query.answer()
            return
        if query.from_user.id != requester_id:
            await query.answer("Esse menu não é seu.", show_alert=True)
            return

        # Invalida o teclado IMEDIATAMENTE pra evitar double-send em duplo-clique.
        # Qualquer segundo clique cai num callback sem botões -> Telegram ignora.
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Safety: confirma que user ainda é membro do grupo antes de enviar.
        try:
            member = await query.bot.get_chat_member(target_chat_id, requester_id)
            status = getattr(member, "status", None)
            if status in ("left", "kicked") or (
                status == "restricted" and not getattr(member, "is_member", True)
            ):
                try:
                    await query.message.edit_text("Você não está mais nesse grupo. Use /nowp de novo.")
                except Exception:
                    # Sprint 5 (D5.02): se feedback ao user falhar, ele
                    # fica sem saber por quê — logamos pra investigar.
                    logger.warning(
                        "NOWP_EDIT_FAILED | branch=not_member | requester=%s | target=%s",
                        requester_id, target_chat_id, exc_info=True,
                    )
                await query.answer()
                return
        except Exception:
            try:
                await query.message.edit_text("Erro ao verificar membro do grupo.")
            except Exception:
                logger.warning(
                    "NOWP_EDIT_FAILED | branch=member_check_error | requester=%s | target=%s",
                    requester_id, target_chat_id, exc_info=True,
                )
            await query.answer()
            return

        try:
            chat = await query.bot.get_chat(target_chat_id)
            group_title = _normalize_optional_text(chat.title) or str(target_chat_id)
        except Exception:
            group_title = str(target_chat_id)

        track = await music_service.get_current_or_last_played(requester_id)
        if not track:
            try:
                await query.message.edit_text(
                    "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo."
                )
            except Exception:
                logger.warning(
                    "NOWP_EDIT_FAILED | branch=no_track | requester=%s | target=%s",
                    requester_id, target_chat_id, exc_info=True,
                )
            await query.answer()
            return

        payload = await build_playing_payload_for_user(
            requester_id, query.from_user.full_name or "Usuário", track
        )
        if not payload:
            try:
                await query.message.edit_text("Erro ao identificar a música.")
            except Exception:
                logger.warning(
                    "NOWP_EDIT_FAILED | branch=no_payload | requester=%s | target=%s",
                    requester_id, target_chat_id, exc_info=True,
                )
            await query.answer()
            return
        _track_id, caption, cover, keyboard = payload

        # ACK cedo: callback queries têm janela curta (~30s) e o fluxo abaixo
        # faz 2 envios pesados (grupo + DM). Sem ACK cedo, query.answer no
        # fim pode falhar com "query is too old". Confirmação final vai
        # como send_message no DM (mais confiável que toast tardio).
        await query.answer("Enviando...")

        # 1) Envia pro grupo alvo (como se /playing tivesse rodado lá dentro).
        try:
            if cover:
                await query.bot.send_photo(
                    chat_id=target_chat_id, photo=str(cover),
                    caption=caption, parse_mode="HTML", reply_markup=keyboard,
                )
            else:
                await query.bot.send_message(
                    chat_id=target_chat_id, text=caption,
                    parse_mode="HTML", reply_markup=keyboard,
                )
        except Exception:
            logger.exception("NOWP_SEND_GROUP_FAILED chat_id=%s user=%s", target_chat_id, requester_id)
            try:
                await query.message.edit_text(
                    f"Erro ao enviar a mensagem no grupo <b>{html.escape(group_title)}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # 2) Substitui o picker no DM pelo próprio /playing (mesma legenda + capa).
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            if cover:
                await query.bot.send_photo(
                    chat_id=query.from_user.id, photo=str(cover),
                    caption=caption, parse_mode="HTML", reply_markup=keyboard,
                )
            else:
                await query.bot.send_message(
                    chat_id=query.from_user.id, text=caption,
                    parse_mode="HTML", reply_markup=keyboard,
                )
        except Exception:
            logger.exception("NOWP_SEND_DM_FAILED user=%s", requester_id)

        # 3) Confirmação final com nome do grupo.
        try:
            await query.bot.send_message(
                chat_id=query.from_user.id,
                text=f"✓ Enviado para <b>{html.escape(group_title)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("NOWP_CONFIRM_FAILED user=%s", requester_id)

    @dp.callback_query(F.data.startswith("nowp:close:"))
    async def nowp_close_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data:
            await query.answer()
            return
        parts = query.data.split(":")
        if len(parts) != 3:
            await query.answer()
            return
        try:
            requester_id = int(parts[2])
        except ValueError:
            await query.answer()
            return
        if query.from_user.id != requester_id:
            await query.answer("Esse menu não é seu.", show_alert=True)
            return
        if query.message:
            try:
                await query.message.edit_text("/nowp fechado.")
            except Exception:
                pass
        await query.answer()

    @dp.message(Command("kingplay"), IsOwner())
    async def kingplay(message: Message) -> None:
        # S3: OWNER-only via filter IsOwner.
        parts = (message.text or "").splitlines()
        if len(parts) >= 2:
            try:
                target_chat_id = int(parts[1].strip())
            except Exception:
                await message.answer("chat_id inválido")
                return
            await _send_kingplay(message, target_chat_id, message.from_user.id, message.from_user.full_name)
            return

        groups = list_groups()
        if not groups:
            await message.answer("Nenhum grupo conhecido. Envie qualquer mensagem no grupo com o bot ativo ou use:\n/kingplay\n<chat_id>")
            return
        await message.answer("Kingplay — escolha o grupo:", reply_markup=_kingplay_groups_keyboard())

    @dp.callback_query(F.data.startswith("kingplay:send:"), IsOwner())
    async def kingplay_send_callback(query: CallbackQuery) -> None:
        # S3: OWNER-only via filter IsOwner (silencioso — não-owners não veem nada).
        if not query.message or not query.data:
            await query.answer()
            return
        try:
            target_chat_id = int(query.data.rsplit(":", 1)[-1])
        except Exception:
            await query.answer("chat_id inválido", show_alert=True)
            return
        await query.answer("Enviando...")
        await _send_kingplay(query.message, target_chat_id, query.from_user.id, query.from_user.full_name)

    @dp.callback_query(F.data == "kingplay:close", IsOwner())
    async def kingplay_close_callback(query: CallbackQuery) -> None:
        # S3: OWNER-only via filter IsOwner.
        if query.message:
            await query.message.edit_text("Kingplay fechado.")
        await query.answer()

    @dp.message(Command("debuguser"), IsOwner())
    async def debug_user(message: Message) -> None:
        # S3: OWNER-only via filter IsOwner.
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
