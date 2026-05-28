"""Sprint X9: inline owner-only com confirmação esteganográfica via card de música.

Fluxo:
1. Owner digita `@tigraobot <chat_id> <user_id>` (em SM, DM ou grupo).
2. L1: handler aceita só se `from_user.id == OWNER_ID`. Senão, 0 resultados.
3. L3': checa `getChatMember(chat_id, bot_id)` admin + flags. Filtra ações
   pelas permissões do bot. Owner-admin status NÃO é requisito (dono do
   código comanda; segurança é L1+L2+L5).
4. Devolve cards com `input_message_content` neutro ("·"). result_id leva
   HMAC (L5).
5. Owner escolhe → chosen_inline_result chega.
6. L2 re-valida owner. Parse + verify HMAC. L4 hard-block target=OWNER.
7. Executa ação via `actions.py` (ban/mute/unmute/unban).
8. Posta no grupo alvo o card da faixa que owner está ouvindo agora
   (`build_playing_payload_for_user(OWNER_ID, ...)`) — uso natural do bot,
   confirmação esteganográfica.
9. DM pro owner: ação, grupo, user, status, música enviada/falhou.

Camadas de segurança:
- L1 inline owner check; L2 chosen owner check; L4 hard-block target=OWNER;
  L5 HMAC; L3' bot-admin sanity (não-bloqueante).

Conflito com `inline_play` em `app/bot/telegram.py:759`: o handler dele agora
tem filter explícito `(q.query or "").lower() == "playing"`, então queries
X9 (`<chat_id> <user_id>`) não são interceptadas no root e propagam pra
este router.
"""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import Router
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from app.moderation_tigrao.actions import (
    ban_user,
    mute_user,
    unban_user,
    unmute_user,
)
from app.moderation_tigrao.inline_hmac import make_result_id, parse_result_id
from app.moderation_tigrao.permissions import OWNER_ID, is_moderator_user

logger = logging.getLogger(__name__)

router = Router(name="moderation_tigrao_inline_x9")

# Texto postado no chat onde owner digitou inline. Aparece como
# mensagem do owner via @tigraoRADIObot. Em caso de SUCESSO, é deixado
# visível — owner apaga manualmente quando quiser. Em caso de FALHA
# (ação ou envio da música), `_erase_inline_ack` substitui por
# `_ERASED_TEXT` pra não vazar falso positivo pro grupo.
#
# Nota de UI: sem emojis (política do projeto). Frase curta + ponto.
_ACK_TEXT = "Música enviada."
# Caractere invisível (WORD JOINER U+2060). Usado pra "apagar"
# visualmente a mensagem quando algo falha — Telegram NÃO permite
# deletar mensagem inline (não há chat_id/message_id na ótica do bot),
# só editar via inline_message_id.
_ERASED_TEXT = "\u2060"
# Keyboard dummy obrigatório no card de ação: sem reply_markup no
# result, o Telegram NÃO devolve `inline_message_id` no
# ChosenInlineResult e o fallback de erase em caso de falha fica
# impossível.
_NOOP_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="\u2060", callback_data="x9:noop")]]
)

# (action_id, flag_bot_necessária, label visível no card)
_ACTIONS: list[tuple[str, str, str]] = [
    ("ban", "can_restrict_members", "Banir"),
    ("mute1h", "can_restrict_members", "Mutar 1h"),
    ("unmute", "can_restrict_members", "Desmutar"),
    ("unban", "can_restrict_members", "Desbanir"),
]
_LABELS = {a: lbl for a, _, lbl in _ACTIONS}


def _stub(card_id: str, title: str, description: str) -> InlineQueryResultArticle:
    # Stubs são cards de erro (IDs inválidos, bot não-admin, sem permissões,
    # target=owner). NÃO devem postar "Música enviada." — seria mentira
    # visível pro grupo até o erase do chosen handler rodar. Postam direto
    # com texto invisível (_ERASED_TEXT) e ainda assim ganham _NOOP_KB pra
    # manter `inline_message_id` disponível no chosen handler (que tenta
    # editar de novo por segurança).
    return InlineQueryResultArticle(
        id=card_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=_ERASED_TEXT),
        reply_markup=_NOOP_KB,
    )


async def _bot_perms(bot, chat_id: int) -> dict[str, bool] | None:
    """Retorna flags do BOT no grupo, ou None se bot não for admin."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", None)
        if status not in {"administrator", "creator"}:
            return None
        is_creator = status == "creator"
        return {
            "can_restrict_members": is_creator
            or bool(getattr(member, "can_restrict_members", False)),
            "can_delete_messages": is_creator
            or bool(getattr(member, "can_delete_messages", False)),
        }
    except Exception:
        logger.warning("X9_GET_CHAT_MEMBER_FAILED chat=%s", chat_id, exc_info=True)
        return None


@router.inline_query()
async def x9_inline(query: InlineQuery) -> None:
    """L1: só responde a moderador autorizado (owner ou 2º). Senão 0 resultados."""
    if not query.from_user or not is_moderator_user(query.from_user.id):
        try:
            await query.answer([], cache_time=0, is_personal=True)
        except Exception:
            pass
        return

    raw = (query.query or "").strip()
    parts = raw.split()
    if len(parts) != 2:
        await query.answer(
            [_stub("x9:help", "Tigrão X9", "Digite: <chat_id> <user_id>")],
            cache_time=0,
            is_personal=True,
        )
        return

    try:
        chat_id = int(parts[0])
        target_user_id = int(parts[1])
    except ValueError:
        await query.answer(
            [_stub("x9:bad", "IDs inválidos", "Use números: <chat_id> <user_id>")],
            cache_time=0,
            is_personal=True,
        )
        return

    # L4 (precoce): nem oferece cards se alvo é um moderador autorizado.
    if is_moderator_user(target_user_id):
        await query.answer(
            [_stub("x9:owner", "Bloqueado", "Não posso agir sobre um moderador.")],
            cache_time=0,
            is_personal=True,
        )
        return

    perms = await _bot_perms(query.bot, chat_id)
    if perms is None:
        await query.answer(
            [_stub("x9:notadmin", "Bot não é admin", f"chat_id={chat_id}")],
            cache_time=0,
            is_personal=True,
        )
        return

    results: list[InlineQueryResultArticle] = []
    for action_id, perm_flag, label in _ACTIONS:
        if not perms.get(perm_flag):
            continue
        rid = make_result_id(chat_id, target_user_id, action_id)
        results.append(
            InlineQueryResultArticle(
                id=rid,
                title=label,
                description=f"user {target_user_id} em chat {chat_id}",
                input_message_content=InputTextMessageContent(message_text=_ACK_TEXT),
                reply_markup=_NOOP_KB,
            )
        )

    if not results:
        await query.answer(
            [_stub("x9:noperms", "Sem permissões", "Bot não pode restringir aqui.")],
            cache_time=0,
            is_personal=True,
        )
        return

    await query.answer(results, cache_time=0, is_personal=True)


async def _erase_inline_ack(bot, inline_message_id: str | None) -> None:
    """Esvazia visualmente a frase "Música enviada." em caso de FALHA.

    Telegram NÃO permite deletar mensagem inline (não há chat_id/message_id
    associados na ótica do bot). O caminho disponível é `edit_message_text`
    com `inline_message_id` — substituímos por U+2060 (word joiner, sem
    glifo) e removemos o keyboard dummy. Resultado: linha em branco no
    histórico, sem texto visível mentindo que a música foi enviada.

    Em SUCESSO total, NUNCA é chamado — a frase fica visível e o owner
    apaga manualmente quando quiser.

    Silencioso em qualquer falha de edit: se o Telegram rejeitar (mensagem
    velha, etc.), só loga em debug — não interrompe o fluxo.
    """
    if not inline_message_id:
        return
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=_ERASED_TEXT,
            reply_markup=None,
        )
    except Exception:
        logger.debug(
            "X9_ERASE_ACK_FAILED inline_message_id=%s",
            inline_message_id,
            exc_info=True,
        )


async def _send_music_confirmation(bot, chat_id: int) -> bool:
    """Posta no chat_id alvo o card da faixa que owner está ouvindo agora.

    Confirmação esteganográfica: indistinguível de uso natural do tigraoRADIO.
    Reusa `build_playing_payload_for_user(OWNER_ID, ...)` em vez de postar
    `/playing` (que rodaria com `from_user=bot` e cairia em hint de conexão).

    Import lazy de `build_playing_payload_for_user` evita ciclo no startup
    (telegram.py importa este router via main.py).
    """
    try:
        from app.bot.telegram import build_playing_payload_for_user
        from app.services.music import music_service

        track = await music_service.get_current_or_last_played(OWNER_ID)
        if not track:
            return False
        payload = await build_playing_payload_for_user(OWNER_ID, "tigrão", track)
        if not payload:
            return False
        _track_id, caption, cover, _kb, _emoji = payload
        if cover:
            await bot.send_photo(
                chat_id=chat_id,
                photo=cover,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return True
    except Exception:
        logger.warning(
            "X9_MUSIC_CONFIRMATION_FAILED chat=%s", chat_id, exc_info=True
        )
        return False


@router.chosen_inline_result()
async def x9_chosen(result: ChosenInlineResult) -> None:
    """L2 re-valida moderador. L5 valida HMAC. L4 hard-block target=moderador."""
    if not result.from_user or not is_moderator_user(result.from_user.id):
        logger.warning(
            "X9_CHOSEN_NOT_MOD from=%s",
            getattr(result.from_user, "id", None),
        )
        return

    bot = result.bot
    inline_message_id = result.inline_message_id  # garantido via _NOOP_KB

    parsed = parse_result_id(result.result_id)
    if not parsed:
        # IDs decorativos (x9:help, x9:bad, x9:owner, x9:notadmin, x9:noperms)
        # caem aqui — esvazia o "·" do chat e sai.
        await _erase_inline_ack(bot, inline_message_id)
        return

    chat_id, target_user_id, action = parsed

    if is_moderator_user(target_user_id):
        # L4 redundante (defense in depth — _ACTIONS já filtra na inline).
        logger.warning(
            "X9_CHOSEN_MOD_TARGET_BLOCKED chat=%s action=%s", chat_id, action
        )
        await _erase_inline_ack(bot, inline_message_id)
        return
    label = _LABELS.get(action, action)
    status = "OK"
    action_ok = False
    try:
        if action == "ban":
            await ban_user(bot, chat_id, target_user_id)
        elif action == "unban":
            await unban_user(bot, chat_id, target_user_id)
        elif action == "mute1h":
            await mute_user(bot, chat_id, target_user_id, timedelta(hours=1))
        elif action == "unmute":
            await unmute_user(bot, chat_id, target_user_id)
        else:
            logger.warning("X9_UNKNOWN_ACTION action=%s", action)
            return
        action_ok = True
        logger.warning(
            "X9_ACTION_OK action=%s chat=%s user=%s",
            action,
            chat_id,
            target_user_id,
        )
    except Exception as exc:
        status = f"FALHOU: {type(exc).__name__}: {exc}"
        logger.exception(
            "X9_ACTION_FAILED action=%s chat=%s user=%s",
            action,
            chat_id,
            target_user_id,
        )

    # SÓ envia card de música se a ação foi bem-sucedida. Card pós-falha
    # seria falso positivo operacional e vazaria que algo foi tentado sem
    # ter sido executado.
    music_sent = False
    if action_ok:
        music_sent = await _send_music_confirmation(bot, chat_id)

    # Política da frase "Música enviada." no grupo:
    # - SUCESSO (ação OK + música enviada): mantém visível. Owner apaga
    #   manualmente quando quiser.
    # - FALHA (ação falhou OU música não enviou): esvazia pra invisível
    #   via edit, pra não vazar falso positivo pro grupo.
    if not (action_ok and music_sent):
        await _erase_inline_ack(bot, inline_message_id)

    # DM pro owner — sem emojis, conforme política de UI.
    try:
        text = (
            "<b>Tigrão X9</b>\n"
            f"Ação: <b>{html.escape(label)}</b>\n"
            f"Grupo: <code>{chat_id}</code>\n"
            f"User: <code>{target_user_id}</code>\n"
            f"Status: {html.escape(status)}\n"
            f"Música: {'enviada' if music_sent else 'não enviada'}"
        )
        await bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        logger.exception("X9_DM_FAILED")
