"""Sprint X9: HMAC pra result_id do inline_query owner-only.

result_id carrega chat_id|user_id|action e é validado antes de qualquer ação.
Sem o HMAC, um terceiro que descubra o formato poderia tentar forjar o
result_id (apesar de L1+L2 já checarem owner em from_user). HMAC é defesa
adicional contra reuso/forge.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from app.config.settings import TELEGRAM_BOT_TOKEN


def _secret() -> bytes:
    return (TELEGRAM_BOT_TOKEN or "x9-dev-secret-tigrao").encode()


def sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:8]).decode().rstrip("=")


def make_result_id(chat_id: int, user_id: int, action: str) -> str:
    payload = f"x9|{chat_id}|{user_id}|{action}"
    return f"{payload}|{sign(payload)}"


def parse_result_id(result_id: str) -> tuple[int, int, str] | None:
    parts = result_id.split("|")
    if len(parts) != 5 or parts[0] != "x9":
        return None
    try:
        chat_id = int(parts[1])
        target_user_id = int(parts[2])
    except ValueError:
        return None
    action = parts[3]
    sig = parts[4]
    payload = f"x9|{chat_id}|{target_user_id}|{action}"
    if not hmac.compare_digest(sig, sign(payload)):
        return None
    return chat_id, target_user_id, action
