from __future__ import annotations

import os
import tempfile

# Keep the smoke test isolated from Railway volume/local production DB.
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tmp.name}")
os.environ.setdefault("DATA_DIR", tempfile.gettempdir())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

from app.bot import private_tools
from app.bot.intent import detect_intent
from app.bot.music_extras import register_music_extra_handlers
from app.bot.private_tools import ddx_preprocess_update, router as private_router
from app.bot.telegram import _playing_keyboard, bot_dispatcher
from app.db.database import engine, init_db, run_migrations
from app.handlers import lili_rodou
from app.handlers.lili_rodou import router as lili_rodou_router
from app.main import app, dispatcher
from app.services.lastfm import _stable_track_id, lastfm_service
from app.services.music import music_service
from app.services.music_proxy import install_music_proxy
from app.services.spotify import spotify_service


def _assert_private_tools() -> None:
    expected_handlers = [
        "hidden",
        "dx",
        "ddx",
        "mx1",
        "mx2",
        "remember_join_request",
        "joinx",
        "vx",
        "uv",
        "mx",
        "xend",
        "ximg",
    ]
    for name in expected_handlers:
        assert hasattr(private_tools, name), f"missing private_tools.{name}"

    assert private_tools.OWNER_ID == 8505890439
    assert private_tools._parse_chat_id("1001234567890") == -1001234567890
    assert private_tools._parse_chat_id("-1001234567890") == -1001234567890

    assert private_tools._parse_duration("10m").total_seconds() == 600
    assert private_tools._parse_duration("2h").total_seconds() == 7200
    assert private_tools._parse_duration("3d").days == 3
    assert private_tools._parse_duration("i") == "indefinido"
    assert private_tools._parse_duration("x") == "unmute"

    chat_id, message_id = private_tools._parse_message_link("https://t.me/c/1234567890/55")
    assert chat_id == -1001234567890
    assert message_id == 55

    chat_id, message_id = private_tools._parse_message_link("https://t.me/somegroup/77")
    assert chat_id == "@somegroup"
    assert message_id == 77


def _assert_vvv_tools() -> None:
    assert lili_rodou.OWNER_ID == 8505890439
    assert lili_rodou._parse_chat_id("1001234567890") == -1001234567890
    assert lili_rodou._parse_chat_id("-1001234567890") == -1001234567890
    assert lili_rodou._parse_user_id("6059326627") == 6059326627


def main() -> None:
    install_music_proxy()
    init_db()
    run_migrations(engine)

    assert detect_intent("tocando") == "play"
    assert detect_intent("texto qualquer") is None

    lastfm_track_id = _stable_track_id("A Very Long Artist Name", "A Very Long Track Name")
    assert lastfm_track_id.startswith("lfm:")
    assert len(f"like:123456789:{lastfm_track_id}".encode("utf-8")) <= 64

    keyboard = _playing_keyboard(lastfm_track_id, 123456789, 1, 0, False)
    assert keyboard.inline_keyboard

    _assert_private_tools()
    _assert_vvv_tools()

    assert private_router is not None
    assert lili_rodou_router is not None
    assert ddx_preprocess_update is not None
    assert register_music_extra_handlers is not None
    assert bot_dispatcher is dispatcher
    assert app is not None
    assert music_service is not None
    assert spotify_service is not None
    assert lastfm_service is not None

    print("TR3 smoke imports ok")


if __name__ == "__main__":
    main()
