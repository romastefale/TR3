"""Sprint 9 (#4): popula o menu nativo de comandos do Telegram client.

set_my_commands faz aparecer o painel "/" no Telegram com sugestões.
SÓ comandos públicos entram — owner-only (hidden, manual, kingplay,
debuguser, tigrao, btb) ficam invisíveis per regra do projeto.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand

logger = logging.getLogger(__name__)


_PUBLIC_COMMANDS: list[tuple[str, str]] = [
    ("playing", "Música tocando agora"),
    ("albnow", "Foco no álbum atual"),
    ("tcanvas", "Canvas do Spotify (vídeo)"),
    ("tstory", "Story da música tocando"),
    ("tnow", "Mosaico do grupo"),
    ("nowp", "Enviar sua música pra um grupo"),
    ("myself", "Seu extrato pessoal Last.fm"),
    ("lastfm", "Conectar Last.fm"),
    ("lastfmoff", "Desconectar Last.fm"),
    ("help", "Lista de comandos"),
    ("start", "Boas-vindas e instruções"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Registra comandos públicos. Falha silenciosa (não bloqueia startup)."""
    try:
        commands = [BotCommand(command=c, description=d) for c, d in _PUBLIC_COMMANDS]
        await bot.set_my_commands(commands)
        logger.info("BOT_COMMANDS_SET | count=%s", len(commands))
    except Exception:
        # Falha aqui não impacta funcionamento — só o menu fica vazio.
        # Log WARNING pra ver no Railway sem mascarar o problema.
        logger.warning("BOT_COMMANDS_SET_FAILED", exc_info=True)
