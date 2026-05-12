from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    try:
        from app.models.lastfm_profile import LastfmProfile  # noqa: F401
        from app.models.spotify_token import SpotifyToken  # noqa: F401
        from app.models.track_like import TrackLike  # noqa: F401
        from app.models.track_play import TrackPlay  # noqa: F401

        Base.metadata.create_all(bind=engine)
        run_migrations()
        logger.info("Database initialized.")
    except Exception as exc:
        logger.exception("Database initialization failed: %s", exc)


def run_migrations() -> None:
    with engine.begin() as conn:
        statements = [
            "ALTER TABLE track_plays ADD COLUMN track_name TEXT",
            "ALTER TABLE track_plays ADD COLUMN artist_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN track_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN artist_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN liked INTEGER DEFAULT 1",
            "ALTER TABLE track_likes ADD COLUMN owner_user_id INTEGER",
        ]
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
