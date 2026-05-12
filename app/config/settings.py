from __future__ import annotations

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


OWNER_ID = _int_env("OWNER_ID", 8505890439)

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

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
LASTFM_API_BASE_URL = os.getenv("LASTFM_API_BASE_URL", "https://ws.audioscrobbler.com/2.0/")
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", str(SPOTIFY_HTTP_TIMEOUT_SECONDS)))

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DATA_DIR / 'app.db'}"
