from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import ChatJoinRequest, ChatPermissions, Message
from sqlalchemy import text

from app.config.settings import OWNER_ID
from app.db.database import engine

logger = logging.getLogger(__name__)
router = Router(name="private_tools")
APPROVAL_WINDOW = timedelta(hours=2)
SINGLE_USE_EXPIRY = timedelta(minutes=5)


def _is_owner_private_message(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID and message.chat.type == "private")


def _lines(message: Message) -> list[str]:
    return [line.strip() for line in (message.text or "").splitlines() if line.strip()]


def _parse_chat_id(value: str) -> int:
    return int(value.strip())


def _parse_user_id(value: str) -> int:
    return int(value.strip())


def _error_text(reason: str, fix: str) -> str:
    return f"Erro:\nMotivo: {reason}\nComo corrigir: {fix}"


def _success_text(title: str, details: str) -> str:
    return f"Sucesso.\n\n{title}\n{details}"


def _parse_duration(value: str):
    value = value.strip().lower()
    if value == "i":
        return "indefinido"
    if value == "x":
        return "unmute"
    if value.isdigit():
        return timedelta(minutes=int(value))
    if value.endswith("m"):
        return timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=int(value[:-1]))
    if value.endswith("d"):
        return timedelta(days=int(value[:-1]))
    raise ValueError("duração inválida")


def _parse_created_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


async def _execute_action(bot, chat_id: int, user_id: int, action: str) -> None:
    if not user_id:
        return
    if action == "vanish":
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        return
    if action == "unvanish":
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        return
    raise ValueError(f"ação inválida: {action}")


def _ensure_join_requests_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS join_requests (user_id INTEGER, chat_id INTEGER, created_at DATETIME);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_join_requests_chat_user ON join_requests (chat_id, user_id, created_at);"))


def _ensure_known_groups_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS known_groups (chat_id INTEGER PRIMARY KEY, title TEXT, updated_at DATETIME);"))


def _remember_group(chat_id: int, title: str | None) -> None:
    _ensure_known_groups_table()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO known_groups (chat_id, title, updated_at)
                VALUES (:chat_id, :title, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at
            """),
            {"chat_id": chat_id, "title": title or str(chat_id), "updated_at": datetime.now(timezone.utc)},
        )


def _ensure_ddx_rules_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS ddx_rules (chat_id INTEGER PRIMARY KEY, words TEXT, enabled INTEGER, updated_at DATETIME);"))


def _ddx_normalize_spaced(value: str) -> str:
    value = value.lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _ddx_normalize_compact(value: str) -> str:
    value = value.lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def _ddx_parse_words(raw: str) -> list[str]:
    words = [item.strip() for item in re.split(r"[,;\n]", raw) if item.strip()]
    normalized: list[str] = []
    seen: set[str] = set()
    for word in words:
        clean = _ddx_normalize_spaced(word)
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _ddx_save(chat_id: int, words: list[str], enabled: bool = True) -> None:
    _ensure_ddx_rules_table()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ddx_rules (chat_id, words, enabled, updated_at)
                VALUES (:chat_id, :words, :enabled, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET words = excluded.words, enabled = excluded.enabled, updated_at = excluded.updated_at
            """),
            {"chat_id": chat_id, "words": json.dumps(words, ensure_ascii=False), "enabled": 1 if enabled else 0, "updated_at": datetime.now(timezone.utc)},
        )


def _ddx_get(chat_id: int) -> dict[str, object] | None:
    _ensure_ddx_rules_table()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT words, enabled FROM ddx_rules WHERE chat_id = :chat_id"), {"chat_id": chat_id}).mappings().first()
    if not row:
        return None
    try:
        words = json.loads(str(row["words"] or "[]"))
        if not isinstance(words, list):
            words = []
    except Exception:
        logger.exception("DDX_LOAD_FAILED | chat_id=%s", chat_id)
        words = []
    return {"words": words, "enabled": bool(row["enabled"])}


def _ddx_match(text_value: str, words: list[str]) -> bool:
    spaced_text = _ddx_normalize_spaced(text_value)
    compact_text = _ddx_normalize_compact(text_value)
    for word in words:
        spaced_word = _ddx_normalize_spaced(str(word))
        compact_word = _ddx_normalize_compact(str(word))
        if not spaced_word or not compact_word:
            continue
        if " " in spaced_word:
            if spaced_word in spaced_text:
                return True
        elif spaced_word in spaced_text or compact_word in compact_text:
            return True
    return False


def _extract_message_links(text_value: str | None) -> list[str]:
    if not text_value:
        return []
    without_command = re.sub(r"^\s*/dx(?:@\w+)?\b", "", text_value.strip(), count=1, flags=re.IGNORECASE)
    links = re.findall(r"(?:https?://)?(?:www\.)?t\.me/[^\s<>]+", without_command)
    cleaned: list[str] = []
    seen: set[str] = set()
    for link in links:
        item = link.strip().strip("<>()[]{}\"'").rstrip(".,;:")
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


def _parse_message_link(link: str) -> tuple[int | str, int]:
    cleaned = link.strip().strip("<>()[]{}\"'").rstrip(".,;:")
    if cleaned.startswith("t.me/") or cleaned.startswith("www.t.me/"):
        cleaned = "https://" + cleaned
    match = re.match(r"https?://(?:www\.)?t\.me/([^/?#]+)/([^?#]+)", cleaned, flags=re.IGNORECASE)
    if not match:
        raise ValueError("link inválido")
    chat_part = match.group(1)
    path = match.group(2).strip("/")
    parts = [part for part in path.split("/") if part]
    if chat_part.lower() == "c":
        if len(parts) < 2 or not parts[0].isdigit():
            raise ValueError("link privado inválido")
        message_candidates = [int(part) for part in parts[1:] if part.isdigit()]
        if not message_candidates:
            raise ValueError("message_id não encontrado")
        return int("-100" + parts[0]), message_candidates[-1]
    message_candidates = [int(part) for part in parts if part.isdigit()]
    if not message_candidates:
        raise ValueError("message_id não encontrado")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", chat_part):
        raise ValueError("username de chat inválido")
    return f"@{chat_part}", message_candidates[-1]


async def _delete_linked_message(bot, link: str) -> tuple[bool, str]:
    chat_id, message_id = _parse_message_link(link)
    await bot.delete_message(chat_id=chat_id, message_id=message_id)
    return True, f"{chat_id}/{message_id}"


async def ddx_preprocess_update(bot, update) -> bool:
    message = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if not message or message.chat.type not in {"group", "supergroup"}:
        return False
    text_value = message.text or message.caption
    if not text_value or not message.from_user or message.from_user.is_bot:
        return False
    payload = _ddx_get(message.chat.id)
    if not payload or not payload.get("enabled"):
        return False
    words = payload.get("words", [])
    if not isinstance(words, list) or not words or not _ddx_match(text_value, words):
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in {"administrator", "creator"}:
            logger.warning("DDX_SKIP_ADMIN | chat_id=%s | user_id=%s | message_id=%s", message.chat.id, message.from_user.id, message.message_id)
            return False
    except Exception:
        logger.exception("DDX_ADMIN_CHECK_FAILED | chat_id=%s | user_id=%s | message_id=%s", message.chat.id, getattr(message.from_user, "id", None), message.message_id)
        return False
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        logger.warning("DDX_DELETED | chat_id=%s | user_id=%s | message_id=%s", message.chat.id, message.from_user.id, message.message_id)
        return True
    except Exception:
        logger.exception("DDX_DELETE_FAILED | chat_id=%s | user_id=%s | message_id=%s", message.chat.id, getattr(message.from_user, "id", None), message.message_id)
        return False


@router.message(Command("hidden"))
async def hidden(message: Message) -> None:
    logger.warning("HIDDEN_COMMAND_RECEIVED | user_id=%s | chat_type=%s | owner_id=%s", getattr(message.from_user, "id", None), message.chat.type, OWNER_ID)
    if not _is_owner_private_message(message):
        return
    await message.answer("COMANDOS OCULTOS\n\n/dx\n/ddx\n/mx1\n/mx2\n/joinx\n/vx\n/uv\n/mx\n/xend\n/ximg\n/vvv")


@router.message(Command("dx"))
async def dx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    links = _extract_message_links(message.text)
    if not links:
        await message.answer("MODERAÇÃO — APAGAR POR LINK\n\nUse:\n/dx\n<link_da_mensagem>\n[outros links opcionais]\n\nExemplos aceitos:\nhttps://t.me/c/1234567890/55\nhttps://t.me/username_do_grupo/55")
        return
    deleted: list[str] = []
    failed: list[str] = []
    for link in links:
        try:
            _, target = await _delete_linked_message(message.bot, link)
            deleted.append(f"{link} -> {target}")
        except TelegramForbiddenError:
            failed.append(f"{link} -> sem permissão para apagar")
        except Exception as exc:
            logger.exception("DX_DELETE_FAILED | link=%s", link)
            failed.append(f"{link} -> {type(exc).__name__}")
    response = ["DX FINALIZADO", "", f"Apagadas: {len(deleted)}", f"Falhas: {len(failed)}"]
    if deleted:
        response.extend(["", "APAGADAS:"])
        response.extend(f"- {item}" for item in deleted[:15])
    if failed:
        response.extend(["", "FALHAS:"])
        response.extend(f"- {item}" for item in failed[:15])
    await message.answer("\n".join(response))


@router.message(Command("ddx"))
async def ddx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 3:
        await message.answer("Use:\n/ddx\n<chat_id>\n<add|remove|list|off|test>\n<palavras ou texto>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        mode = lines[2].strip().lower()
        current = _ddx_get(chat_id) or {"words": [], "enabled": True}
        current_words = current.get("words", [])
        if not isinstance(current_words, list):
            current_words = []
        if mode == "list":
            await message.answer("DDX\n" f"Grupo: {chat_id}\n" f"Status: {'ativo' if current.get('enabled') else 'inativo'}\n" f"Palavras: {', '.join(str(word) for word in current_words) if current_words else 'nenhuma'}")
            return
        if mode == "off":
            _ddx_save(chat_id, [str(w) for w in current_words], enabled=False)
            await message.answer(f"DDX desligado.\nGrupo: {chat_id}")
            return
        if mode == "add":
            if len(lines) < 4:
                await message.answer("Informe as palavras para adicionar.")
                return
            incoming = _ddx_parse_words("\n".join(lines[3:]))
            if not incoming:
                await message.answer("Nenhuma palavra válida informada.")
                return
            final_words = list(dict.fromkeys([str(w) for w in current_words] + incoming))
            _ddx_save(chat_id, final_words, enabled=True)
            await message.answer("DDX atualizado.\n" f"Grupo: {chat_id}\n" "Status: ativo\n" f"Total de palavras: {len(final_words)}")
            return
        if mode == "remove":
            if len(lines) < 4:
                await message.answer("Informe as palavras para remover.")
                return
            remove_words = set(_ddx_parse_words("\n".join(lines[3:])))
            final_words = [str(w) for w in current_words if str(w) not in remove_words]
            _ddx_save(chat_id, final_words, enabled=True)
            await message.answer("DDX atualizado.\n" f"Grupo: {chat_id}\n" "Status: ativo\n" f"Total de palavras: {len(final_words)}")
            return
        if mode == "test":
            if len(lines) < 4:
                await message.answer("Informe o texto para teste.")
                return
            matched = _ddx_match("\n".join(lines[3:]), [str(w) for w in current_words])
            await message.answer("DDX TESTE\n" f"Grupo: {chat_id}\n" f"Resultado: {'detectado' if matched else 'não detectado'}")
            return
        await message.answer("Modo inválido. Use add, remove, list, off ou test.")
    except Exception:
        logger.exception("DDX_COMMAND_FAILED")
        await message.answer("Erro ao processar /ddx.")


@router.message(Command("mx1"))
async def mx1(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 2:
        await message.answer("Título: Link direto\nDescrição: Gera link de entrada imediata, uso único e expiração curta.\n\nUse:\n/mx1\n<chat_id>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        invite = await message.bot.create_chat_invite_link(chat_id=chat_id, creates_join_request=False, member_limit=1, expire_date=datetime.now(timezone.utc) + SINGLE_USE_EXPIRY)
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Link de entrada direta gerado.", f"Grupo: {chat_id}\nLink:\n{invite.invite_link}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode gerar links"))
    except Exception:
        logger.exception("Falha ao criar link direto")
        await message.answer(_error_text("falha ao criar link", "verifique o chat_id, permissões do bot e tente novamente"))


@router.message(Command("mx2"))
async def mx2(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 2:
        await message.answer("Título: Link com aprovação\nDescrição: Gera link onde a entrada depende de aprovação.\n\nUse:\n/mx2\n<chat_id>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        invite = await message.bot.create_chat_invite_link(chat_id=chat_id, creates_join_request=True)
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Link de solicitação de entrada gerado.", f"Grupo: {chat_id}\nLink:\n{invite.invite_link}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode gerar links"))
    except Exception:
        logger.exception("Falha ao criar link com aprovação")
        await message.answer(_error_text("falha ao criar link", "verifique o chat_id, permissões do bot e tente novamente"))


@router.chat_join_request()
async def remember_join_request(request: ChatJoinRequest) -> None:
    _ensure_join_requests_table()
    _remember_group(request.chat.id, request.chat.title or str(request.chat.id))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM join_requests WHERE user_id = :user_id AND chat_id = :chat_id"), {"user_id": request.from_user.id, "chat_id": request.chat.id})
        conn.execute(text("INSERT INTO join_requests (user_id, chat_id, created_at) VALUES (:user_id, :chat_id, :created_at)"), {"user_id": request.from_user.id, "chat_id": request.chat.id, "created_at": datetime.now(timezone.utc)})
    logger.warning("JOIN_REQUEST_REGISTERED | chat_id=%s | user_id=%s", request.chat.id, request.from_user.id)


@router.message(Command("joinx"))
async def joinx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    _ensure_join_requests_table()
    lines = _lines(message)
    if len(lines) < 3:
        await message.answer("ACESSO — APROVAR SOLICITAÇÃO\n\nUse:\n/joinx\n<chat_id>\n<user_id>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        user_id = _parse_user_id(lines[2])
    except Exception:
        await message.answer(_error_text("chat_id ou user_id inválido", "envie apenas números nas linhas 2 e 3"))
        return
    cutoff = datetime.now(timezone.utc) - APPROVAL_WINDOW
    local_status = "sem registro local recente"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM join_requests WHERE created_at < :cutoff"), {"cutoff": cutoff})
        row = conn.execute(text("SELECT user_id, chat_id, created_at FROM join_requests WHERE user_id = :user_id AND chat_id = :chat_id ORDER BY created_at DESC LIMIT 1"), {"user_id": user_id, "chat_id": chat_id}).mappings().first()
    if row:
        created_at = _parse_created_at(row["created_at"])
        if created_at is not None and created_at >= cutoff:
            local_status = "registro local encontrado"
    try:
        await message.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode aprovar solicitações"))
        return
    except Exception:
        logger.exception("JOINX_APPROVAL_FAILED | chat_id=%s | user_id=%s | local_status=%s", chat_id, user_id, local_status)
        await message.answer(_error_text("falha na aprovação", "confirme se o usuário ainda possui solicitação pendente e se o bot pode aprovar entradas"))
        return
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM join_requests WHERE user_id = :user_id AND chat_id = :chat_id"), {"user_id": user_id, "chat_id": chat_id})
    _remember_group(chat_id, str(chat_id))
    await message.answer(_success_text("Usuário aprovado.", f"Grupo: {chat_id}\nUsuário: {user_id}\nRegistro: {local_status}"))


@router.message(Command("vx"))
async def vx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 3:
        await message.answer("Título: Vanish\nDescrição: Remove usuário imediatamente do grupo.\n\nUse:\n/vx\n<chat_id>\n<user_id>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        user_id = _parse_user_id(lines[2])
        await _execute_action(message.bot, chat_id, user_id, "vanish")
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Vanish executado.", f"Grupo: {chat_id}\nUsuário: {user_id}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode banir usuários"))
    except Exception:
        logger.exception("Falha no vanish")
        await message.answer(_error_text("falha na execução", "verifique chat_id, user_id e permissões do bot"))


@router.message(Command("uv"))
async def uv(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 3:
        await message.answer("Título: Unvanish\nDescrição: Restaura acesso de usuário removido.\n\nUse:\n/uv\n<chat_id>\n<user_id>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        user_id = _parse_user_id(lines[2])
        await _execute_action(message.bot, chat_id, user_id, "unvanish")
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Unvanish executado.", f"Grupo: {chat_id}\nUsuário: {user_id}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode desbanir usuários"))
    except Exception:
        logger.exception("Falha no unvanish")
        await message.answer(_error_text("falha na execução", "verifique chat_id, user_id e permissões do bot"))


@router.message(Command("mx"))
async def mx(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    lines = _lines(message)
    if len(lines) < 4:
        await message.answer("Título: Restrição\nDescrição: Muta ou desmuta usuário.\n\nUse:\n/mx\n<chat_id>\n<user_id>\n<10m|2h|3d|i|x>")
        return
    try:
        chat_id = _parse_chat_id(lines[1])
        user_id = _parse_user_id(lines[2])
        duration = _parse_duration(lines[3])
        if duration == "unmute":
            await message.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
            action_text = "Usuário desmutado."
        else:
            until_date = None if duration == "indefinido" else datetime.now(timezone.utc) + duration
            await message.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until_date)
            action_text = "Usuário mutado."
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text(action_text, f"Grupo: {chat_id}\nUsuário: {user_id}\nDuração: {lines[3]}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot é administrador do grupo e pode restringir usuários"))
    except ValueError:
        await message.answer(_error_text("duração inválida", "use valores como 10m, 2h, 3d, i ou x"))
    except Exception:
        logger.exception("Falha no /mx")
        await message.answer(_error_text("falha na execução", "verifique chat_id, user_id, duração e permissões do bot"))


@router.message(Command("xend"))
async def xend(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    if not message.reply_to_message:
        await message.answer("Use /xend <chat_id> ou /xend pin <chat_id> respondendo à mensagem que deseja copiar.")
        return
    parts = (message.text or "").split()
    should_pin = len(parts) >= 2 and parts[1].lower() == "pin"
    chat_id_index = 2 if should_pin else 1
    if len(parts) <= chat_id_index:
        await message.answer("Use /xend <chat_id> ou /xend pin <chat_id> respondendo à mensagem que deseja copiar.")
        return
    try:
        chat_id = int(parts[chat_id_index])
    except ValueError:
        await message.answer(_error_text("chat_id inválido", "envie o chat_id numérico após /xend"))
        return
    try:
        copied = await message.bot.copy_message(chat_id=chat_id, from_chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
        if should_pin:
            await message.bot.pin_chat_message(chat_id=chat_id, message_id=copied.message_id, disable_notification=True)
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Mensagem copiada." if not should_pin else "Mensagem copiada e fixada.", f"Grupo: {chat_id}\nMensagem: {copied.message_id}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot pode enviar e fixar mensagens no grupo de destino"))
    except Exception:
        logger.exception("Falha no /xend")
        await message.answer(_error_text("falha ao copiar mensagem", "verifique chat_id, permissões e tente novamente"))


@router.message(Command("ximg"))
async def ximg(message: Message) -> None:
    if not _is_owner_private_message(message):
        return
    if not message.reply_to_message:
        await message.answer("Use /ximg <chat_id> respondendo à mídia que deseja enviar.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Use /ximg <chat_id> respondendo à mídia que deseja enviar.")
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.answer(_error_text("chat_id inválido", "envie o chat_id numérico após /ximg"))
        return
    try:
        sent = await message.bot.copy_message(chat_id=chat_id, from_chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
        _remember_group(chat_id, str(chat_id))
        await message.answer(_success_text("Mídia enviada.", f"Grupo: {chat_id}\nMensagem: {sent.message_id}"))
    except TelegramForbiddenError:
        await message.answer(_error_text("operação não permitida", "verifique se o bot pode enviar mídia no grupo de destino"))
    except Exception:
        logger.exception("Falha no /ximg")
        await message.answer(_error_text("falha ao enviar mídia", "verifique chat_id, permissões e tente novamente"))
