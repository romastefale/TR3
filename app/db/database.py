from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

try:
    os.makedirs("/data", exist_ok=True)
    logger.info("Database directory /data ready.")
except Exception as exc:
    logger.warning("Could not prepare /data: %s", exc)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def run_migrations(engine) -> None:
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

        try:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS lastfm_profiles (
                        user_id INTEGER PRIMARY KEY,
                        username VARCHAR NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
        except Exception:
            pass

        try:
            index_rows = conn.execute(text("PRAGMA index_list(track_likes)")).all()
            has_new_unique = any(str(row[1]) == "uq_user_owner_track_like" for row in index_rows)
            if not has_new_unique:
                conn.execute(text("DROP TABLE IF EXISTS track_likes_migrated"))
                conn.execute(
                    text(
                        """
                        CREATE TABLE track_likes_migrated (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL,
                            owner_user_id INTEGER,
                            track_id VARCHAR NOT NULL,
                            track_name VARCHAR,
                            artist_name VARCHAR,
                            liked INTEGER DEFAULT 1,
                            created_at DATETIME NOT NULL
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO track_likes_migrated (
                            id, user_id, owner_user_id, track_id, track_name, artist_name, liked, created_at
                        )
                        SELECT id, user_id, owner_user_id, track_id, track_name, artist_name, liked, created_at
                        FROM track_likes
                        """
                    )
                )
                conn.execute(text("DROP TABLE track_likes"))
                conn.execute(text("ALTER TABLE track_likes_migrated RENAME TO track_likes"))
                conn.execute(text("CREATE INDEX ix_track_likes_user_id ON track_likes(user_id)"))
                conn.execute(text("CREATE INDEX ix_track_likes_owner_user_id ON track_likes(owner_user_id)"))
                conn.execute(text("CREATE INDEX ix_track_likes_track_id ON track_likes(track_id)"))
                conn.execute(
                    text("CREATE UNIQUE INDEX uq_user_owner_track_like ON track_likes(user_id, owner_user_id, track_id)")
                )
        except Exception:
            pass


def init_db() -> None:
    try:
        from app.models.lastfm_profile import LastfmProfile  # noqa: F401
        from app.models.spotify_token import SpotifyToken  # noqa: F401
        from app.models.track_like import TrackLike  # noqa: F401
        from app.models.track_play import TrackPlay  # noqa: F401

        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized.")
    except Exception as exc:
        logger.exception("Database initialization failed: %s", exc)
