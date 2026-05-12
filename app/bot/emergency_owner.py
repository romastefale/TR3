from __future__ import annotations

from aiogram import Bot

from app.config.settings import OWNER_ID


HIDDEN_TEXT = (
    "COMANDOS OCULTOS\n\n"
    "/dx\n<link_da_mensagem>\n\n"
    "/ddx\n<chat_id>\n<add|remove|list|off|test>\n<palavras ou texto>\n\n"
    "/mx1\n<chat_id>\n\n"
    "/mx2\n<chat_id>\n\n"
    "/joinx\n<chat_id>\n<user_id>\n\n"
    "/vx\n<chat_id>\n<user_id>\n\n"
    "/uv\n<chat_id>\n<user_id>\n\n"
    "/mx\n<chat_id>\n<user_id>\n<10m|2h|3d|i|x>\n\n"
    "/xend <chat_id> — respondendo uma mensagem\n"
    "/xend pin <chat_id> — copia e fixa\n\n"
    "/ximg\n<chat_id> — respondendo uma mídia\n\n"
    "/vvv\n<chat_id>\n<user_id>"
)


def _command_name(text: str | None) -> str | None:
    if not text:
        return None
    first = text.strip().split(maxsplit=1)[0].lower()
    if not first.startswith("/"):
        return None
    first = first[1:]
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


async def handle_emergency_owner_command(bot: Bot, update) -> bool:
    message = getattr(update, "message", None)
    if not message:
        return False

    command = _command_name(getattr(message, "text", None))
    if command not in {"ownercheck", "hidden"}:
        return False

    chat = getattr(message, "chat", None)
    from_user = getattr(message, "from_user", None)
    if not chat or not from_user:
        return True

    if getattr(chat, "type", None) != "private":
        return True

    user_id = int(from_user.id)

    if command == "ownercheck":
        if OWNER_ID == 0:
            await bot.send_message(
                chat_id=chat.id,
                text=(
                    "OWNER CHECK\n\n"
                    f"Seu ID: {user_id}\n"
                    "OWNER_ID atual: não configurado ou inválido\n\n"
                    "Corrija a variável OWNER_ID no Railway para habilitar os comandos moderadores."
                ),
            )
            return True

        if user_id != OWNER_ID:
            await bot.send_message(
                chat_id=chat.id,
                text=(
                    "OWNER CHECK\n\n"
                    f"Seu ID: {user_id}\n"
                    "Status: não autorizado para comandos moderadores."
                ),
            )
            return True

        await bot.send_message(
            chat_id=chat.id,
            text=(
                "OWNER CHECK\n\n"
                f"Seu ID: {user_id}\n"
                "Status: autorizado."
            ),
        )
        return True

    if OWNER_ID == 0:
        await bot.send_message(
            chat_id=chat.id,
            text=(
                "COMANDOS OCULTOS\n\n"
                "Bloqueado: OWNER_ID não está configurado ou está inválido no Railway.\n\n"
                f"Seu ID: {user_id}\n"
                "Defina OWNER_ID com esse número para liberar os comandos moderadores."
            ),
        )
        return True

    if user_id != OWNER_ID:
        await bot.send_message(
            chat_id=chat.id,
            text=(
                "COMANDOS OCULTOS\n\n"
                "Bloqueado: este usuário não corresponde ao OWNER_ID configurado.\n\n"
                f"Seu ID: {user_id}\n"
                "Use /ownercheck para conferir."
            ),
        )
        return True

    await bot.send_message(chat_id=chat.id, text=HIDDEN_TEXT)
    return True
