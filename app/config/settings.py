from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


OWNER_ID = _int_env("OWNER_ID", 8505890439)

# Co-moderador autorizado. Decisão explícita do owner: este é o ÚNICO outro
# usuário que recebe as MESMAS permissões do dono (principalmente moderação)
# e pode operar o /tigrao simultaneamente com o owner. Hardcoded de propósito
# (NÃO é env var) — não deve ser configurável nem ampliável sem pedido
# explícito. Ambos compartilham autorização e hard-block (nenhum pode ser
# alvo de moderação).
SECOND_MODERATOR_ID = 6834269386

# Conjunto de moderadores autorizados (owner + co-moderador). Usado por
# is_moderator_user / filtros de acesso / hard-blocks. OWNER_ID continua
# sendo a fonte única pra alvo de notificações DM (ddx soft, new-member-watch,
# inline) — co-moderação NÃO redireciona DMs, só concede poder de ação.
MODERATOR_IDS: tuple[int, ...] = (OWNER_ID, SECOND_MODERATOR_ID)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SPOTIFY_REDIRECT_URI = f"{BASE_URL}/callback"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-recently-played"

SPOTIFY_HTTP_TIMEOUT_SECONDS = float(os.getenv("SPOTIFY_HTTP_TIMEOUT_SECONDS", "10"))
SPOTIFY_MAX_CONCURRENT_REQUESTS = int(os.getenv("SPOTIFY_MAX_CONCURRENT_REQUESTS", "10"))
SPOTIFY_CACHE_TTL_SECONDS = float(os.getenv("SPOTIFY_CACHE_TTL_SECONDS", "5"))
SPOTIFY_CACHE_MAX_ENTRIES = int(os.getenv("SPOTIFY_CACHE_MAX_ENTRIES", "500"))
SPOTIFY_PER_USER_RATE_LIMIT = int(os.getenv("SPOTIFY_PER_USER_RATE_LIMIT", "10"))
SPOTIFY_RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("SPOTIFY_RATE_LIMIT_WINDOW_SECONDS", "5"))
SPOTIFY_CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("SPOTIFY_CIRCUIT_BREAKER_THRESHOLD", "3"))
SPOTIFY_CIRCUIT_BREAKER_COOLDOWN_SECONDS = float(os.getenv("SPOTIFY_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "8"))
SPOTIFY_CANVAS_ENABLED = _bool_env("SPOTIFY_CANVAS_ENABLED", True)
SPOTIFY_CANVAS_SP_DC = os.getenv("SPOTIFY_CANVAS_SP_DC", "").strip()
SPOTIFY_CANVAS_TIMEOUT_SECONDS = float(os.getenv("SPOTIFY_CANVAS_TIMEOUT_SECONDS", "4"))

# Cache de Canvas via file_id do Telegram (/tcanvas e /tly).
# O Telegram guarda cada arquivo enviado e devolve um file_id reusável entre
# chats (mesmo bot) — então depois do 1º envio de uma faixa, os próximos vão
# por file_id (sem rebaixar do CDN nem re-subir). Persistido em DB.
CANVAS_CACHE_ENABLED = _bool_env("CANVAS_CACHE_ENABLED", True)
# Canal privado (arquivo) onde cada Canvas é subido UMA vez. 0 = sem canal:
# nesse modo o file_id é capturado do próprio envio no grupo (ainda economiza
# a partir do 2º envio). Com canal setado, o bot precisa ser ADMIN nele.
# Channel id é tipo -100xxxxxxxxxx (negativo).
CANVAS_CACHE_CHANNEL_ID = _int_env("CANVAS_CACHE_CHANNEL_ID", 0)

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
LASTFM_API_BASE_URL = os.getenv("LASTFM_API_BASE_URL", "https://ws.audioscrobbler.com/2.0/")
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", str(SPOTIFY_HTTP_TIMEOUT_SECONDS)))

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DATA_DIR / 'app.db'}"


# Variáveis críticas: ausência delas faz o bot crashar silenciosamente em
# runtime (sem login Spotify, sem capas Last.fm, sem dispatch Telegram).
# validate_required_env() é chamado no startup e loga WARNING explícito
# pra cada faltante — não interrompe boot pra permitir dev local parcial.
REQUIRED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID),
    ("SPOTIFY_CLIENT_SECRET", SPOTIFY_CLIENT_SECRET),
    ("LASTFM_API_KEY", LASTFM_API_KEY),
    ("BASE_URL", BASE_URL if BASE_URL != "http://localhost:8000" else ""),
)


def validate_required_env() -> list[str]:
    """Retorna lista de env vars críticas faltantes. Vazia = tudo ok."""
    return [name for name, value in REQUIRED_ENV_VARS if not value]


# Sprint 4 (S4.4): secret_token do webhook Telegram derivado por HMAC do
# TELEGRAM_BOT_TOKEN. Mesma estratégia da Sprint 1 OAuth state — evita
# precisar de uma env var nova e mantém os dois lados (set_webhook +
# validação no handler /webhook) sincronizados deterministicamente.
# Sem TELEGRAM_BOT_TOKEN, retorna None — set_webhook sem secret e o
# handler não exige header. Em prod com token, o secret existe sempre.
# Telegram secret_token aceita 1-256 chars [A-Za-z0-9_-]; hex SHA256 cabe.
_WEBHOOK_SECRET_PURPOSE = b"tr3-webhook-v1"


def telegram_webhook_secret() -> str | None:
    if not TELEGRAM_BOT_TOKEN:
        return None
    return hmac.new(
        TELEGRAM_BOT_TOKEN.encode(),
        _WEBHOOK_SECRET_PURPOSE,
        hashlib.sha256,
    ).hexdigest()
