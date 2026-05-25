"""Sprint 8: reage 👀 a mensagens em grupo contendo termos-gatilho.

Termos: pierinho, pe, tigrao, tigrão, pi, π, pipi, pipizinho, pedro, p,
pidro, romastefale, santepho, pidrao, nuapp — case-insensitive, word
boundary, aceita @prefix.

Chamado direto pelo webhook handler em main.py (fora do dispatcher) pra
NUNCA consumir o update — outros handlers (tigraoresponde, commands,
text_aliases) continuam processando normalmente.
"""
from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.types import ReactionTypeEmoji, Update

logger = logging.getLogger(__name__)

_TRIGGER_TERMS = (
    "pierinho",
    "pipizinho",
    "romastefale",
    "santepho",
    "pidrao",
    "tigrão",
    "tigrao",
    "pidro",
    "pedro",
    "pipi",
    "nuapp",
    "pe",
    "pi",
    "π",
    "p",
)
# \b em ambos lados pra word boundary; @ é \W então @pedro tem boundary
# natural entre @ e pedro. Termos longos primeiro pra não importar (regex
# alterna na ordem mas como cada um tem \b delimitando, não há overlap).
_TRIGGER_RE = re.compile(
    r"\b@?(?:" + "|".join(_TRIGGER_TERMS) + r")\b",
    re.IGNORECASE,
)
_REACTION_EMOJI = "👀"


async def react_if_mention(bot: Bot, update: Update) -> None:
    """Reage 👀 se a mensagem é de grupo e contém termo-gatilho.

    Falha silenciosamente em qualquer erro (rate limit, permissão).
    NUNCA levanta exceção — main.py wrappa em try/except mas evitamos
    poluir o log com casos esperados (mensagem deletada, bot sem
    permissão de reagir naquele grupo etc).
    """
    message = update.message
    if not message:
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    if message.from_user and message.from_user.is_bot:
        return
    text = message.text or message.caption or ""
    if not text:
        return
    if not _TRIGGER_RE.search(text):
        return
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=_REACTION_EMOJI)],
        )
    except Exception:
        logger.debug(
            "MENTION_REACT_FAILED chat=%s msg=%s",
            message.chat.id,
            message.message_id,
            exc_info=True,
        )
