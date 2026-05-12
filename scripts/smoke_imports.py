from __future__ import annotations

import os
import tempfile

# Keep the smoke test isolated from Railway volume/local production DB.
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tmp.name}")
os.environ.setdefault("DATA_DIR", tempfile.gettempdir())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

from app.bot.intent import detect_intent
from app.bot.private_tools import ddx_preprocess_update, router as private_router
from app.bot.telegram import _playing_keyboard
from app.db.database import init_db
from app.handlers.lili_rodou import router as lili_rodou_router
from app.main import app, dispatcher
from app.services.lastfm import _stable_track_id
from app.services.music import music_service
from app.services.spotify import spotify_service


def main() -> None:
    init_db()

    assert detect_intent("tocando") == "play"
    assert detect_intent("texto qualquer") is None

    lastfm_track_id = _stable_track_id("A Very Long Artist Name", "A Very Long Track Name")
    assert lastfm_track_id.startswith("lfm:")
    assert len(f"like:123456789:{lastfm_track_id}".encode("utf-8")) <= 64

    keyboard = _playing_keyboard(lastfm_track_id, 123456789, 1, 0, False)
    assert keyboard.inline_keyboard

    assert private_router is not None
    assert lili_rodou_router is not None
    assert ddx_preprocess_update is not None
    assert app is not None
    assert dispatcher is not None
    assert music_service is not None
    assert spotify_service is not None

    print("TR3 smoke imports ok")


if __name__ == "__main__":
    main()
