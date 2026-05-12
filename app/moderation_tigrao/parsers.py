from __future__ import annotations

import re
from datetime import timedelta


def parse_chat_id(value: str) -> int:
    raw = str(value).strip().replace(" ", "")
    if not re.fullmatch(r"-?\d+", raw):
        raise ValueError("chat_id deve ser numérico")
    chat_id = int(raw)
    if chat_id > 0:
        chat_id = -chat_id
    return chat_id


def parse_user_id(value: str) -> int:
    raw = str(value).strip().replace(" ", "")
    if not re.fullmatch(r"\d+", raw):
        raise ValueError("user_id deve ser numérico")
    return int(raw)


def parse_duration(value: str):
    raw = str(value).strip().lower().replace(" ", "")
    if raw == "i":
        return "indefinido"
    if raw == "x":
        return "desmutar"
    if raw.isdigit():
        return timedelta(minutes=int(raw))
    if raw.endswith("m") and raw[:-1].isdigit():
        return timedelta(minutes=int(raw[:-1]))
    if raw.endswith("h") and raw[:-1].isdigit():
        return timedelta(hours=int(raw[:-1]))
    if raw.endswith("d") and raw[:-1].isdigit():
        return timedelta(days=int(raw[:-1]))
    raise ValueError("duração inválida")


def parse_message_link(value: str) -> tuple[int | str, int]:
    cleaned = str(value).strip().strip("<>()[]{}\"'").rstrip(".,;:")
    if cleaned.startswith("t.me/") or cleaned.startswith("www.t.me/"):
        cleaned = "https://" + cleaned

    match = re.match(
        r"https?://(?:www\.)?t\.me/([^/?#]+)/([^?#]+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("link de mensagem inválido")

    chat_part = match.group(1)
    parts = [part for part in match.group(2).strip("/").split("/") if part]

    if chat_part.lower() == "c":
        if len(parts) < 2 or not parts[0].isdigit():
            raise ValueError("link privado inválido")
        message_ids = [int(part) for part in parts[1:] if part.isdigit()]
        if not message_ids:
            raise ValueError("message_id não encontrado")
        return int("-100" + parts[0]), message_ids[-1]

    message_ids = [int(part) for part in parts if part.isdigit()]
    if not message_ids:
        raise ValueError("message_id não encontrado")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", chat_part):
        raise ValueError("username do grupo inválido")
    return f"@{chat_part}", message_ids[-1]
