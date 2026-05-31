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
    dialect_name = engine.dialect.name
    with engine.begin() as conn:
        statements = [
            "ALTER TABLE track_plays ADD COLUMN track_name TEXT",
            "ALTER TABLE track_plays ADD COLUMN artist_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN track_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN artist_name TEXT",
            "ALTER TABLE track_likes ADD COLUMN liked INTEGER DEFAULT 1",
            "ALTER TABLE track_likes ADD COLUMN owner_user_id INTEGER",
        ]
        # Postgres: relaxa refresh_token pra nullable (Spotify nem sempre
        # devolve refresh em /api/token). SQLite ignora — schema legado já
        # convive bem porque o código grava `refresh_token or ""`.
        if dialect_name == "postgresql":
            statements.append(
                "ALTER TABLE spotify_tokens ALTER COLUMN refresh_token DROP NOT NULL"
            )
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception:
                # Sprint 4 (S4.2): falhas esperadas em SQLite (ALTER COLUMN
                # não suportado) ou quando a coluna/constraint já está no
                # estado desejado. DEBUG basta pra investigar quando algo
                # novo aparecer.
                logger.debug("DB migration stmt skipped | stmt=%s", stmt, exc_info=True)

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
            # Sprint 4 (S4.2): CREATE TABLE IF NOT EXISTS deveria ser
            # idempotente — falha aqui sinaliza problema sério (conexão
            # morta, schema corrompido). WARNING com traceback pra Railway
            # logs mostrarem rápido sem mascarar.
            logger.warning("DB lastfm_profiles ensure failed", exc_info=True)

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
            # Sprint 4 (S4.2): migração de track_likes pra UNIQUE composto.
            # Falha aqui PODE deixar schema inconsistente (tabela temp
            # `track_likes_migrated` pendurada). WARNING + traceback no
            # log do Railway pra investigar antes que vire bug em
            # produção (likes duplicados, etc).
            logger.warning("DB track_likes migration failed", exc_info=True)

        # Sprint 12: migra track_likes legado (botão ♥ removido na Sprint 8)
        # pra track_reactions, fonte única de verdade dos likes.
        # - chat_id = -1 + message_id = track_likes.id → IDs sintéticos
        #   únicos por construção, sem colisão com IDs reais do Telegram.
        # - emoji '♥' marca origem legacy (distinto de 🔥/❤/🏆 do bot).
        # - WHERE liked=1 ignora unlikes (toggle off).
        # - ON CONFLICT/INSERT OR IGNORE garante idempotência: rodar 2x
        #   no boot não duplica nada (chat=-1 + msg=id + user + '♥' é UK).
        # - track_reactions precisa existir; init_db() roda DEPOIS de
        #   run_migrations(), então usamos CREATE TABLE IF NOT EXISTS
        #   defensivo antes do INSERT.
        try:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS track_reactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        emoji VARCHAR NOT NULL,
                        track_id VARCHAR,
                        owner_user_id INTEGER,
                        created_at DATETIME NOT NULL,
                        CONSTRAINT uq_card_user_emoji UNIQUE (chat_id, message_id, user_id, emoji)
                    )
                    """
                ) if dialect_name == "sqlite" else text(
                    """
                    CREATE TABLE IF NOT EXISTS track_reactions (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,
                        emoji VARCHAR NOT NULL,
                        track_id VARCHAR,
                        owner_user_id BIGINT,
                        created_at TIMESTAMP NOT NULL,
                        CONSTRAINT uq_card_user_emoji UNIQUE (chat_id, message_id, user_id, emoji)
                    )
                    """
                )
            )
            if dialect_name == "postgresql":
                migrate_sql = """
                    INSERT INTO track_reactions
                        (chat_id, message_id, user_id, emoji, track_id, owner_user_id, created_at)
                    SELECT -1, tl.id, tl.user_id, '♥', tl.track_id, tl.owner_user_id, tl.created_at
                    FROM track_likes tl
                    WHERE COALESCE(tl.liked, 1) = 1
                    ON CONFLICT (chat_id, message_id, user_id, emoji) DO NOTHING
                """
            else:
                migrate_sql = """
                    INSERT OR IGNORE INTO track_reactions
                        (chat_id, message_id, user_id, emoji, track_id, owner_user_id, created_at)
                    SELECT -1, tl.id, tl.user_id, '♥', tl.track_id, tl.owner_user_id, tl.created_at
                    FROM track_likes tl
                    WHERE COALESCE(tl.liked, 1) = 1
                """
            result = conn.execute(text(migrate_sql))
            migrated = getattr(result, "rowcount", -1)
            if migrated > 0:
                logger.info("Sprint 12: migrated %d track_likes → track_reactions", migrated)
        except Exception:
            logger.warning("Sprint 12 likes→reactions migration failed", exc_info=True)

        # Sprint X3: tabela de auditoria de reactions (TTL 24h) usada
        # pelo painel rmod pra listar quem reagiu numa msg sem depender
        # de @username. CREATE TABLE IF NOT EXISTS é idempotente; o
        # Base.metadata.create_all() em init_db() também cria, mas
        # mantemos aqui pra consistência com track_reactions e pra
        # garantir colunas BigInteger explícitas em Postgres.
        try:
            if dialect_name == "postgresql":
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS reaction_audit (
                            id SERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
                            message_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            user_name VARCHAR,
                            user_username VARCHAR,
                            emoji VARCHAR NOT NULL,
                            created_at TIMESTAMP NOT NULL,
                            CONSTRAINT uq_reaction_audit_msg_user_emoji
                                UNIQUE (chat_id, message_id, user_id, emoji)
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_chat_id ON reaction_audit(chat_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_user_id ON reaction_audit(user_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_created_at ON reaction_audit(created_at)"))
            else:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS reaction_audit (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            chat_id INTEGER NOT NULL,
                            message_id INTEGER NOT NULL,
                            user_id INTEGER NOT NULL,
                            user_name VARCHAR,
                            user_username VARCHAR,
                            emoji VARCHAR NOT NULL,
                            created_at DATETIME NOT NULL,
                            CONSTRAINT uq_reaction_audit_msg_user_emoji
                                UNIQUE (chat_id, message_id, user_id, emoji)
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_chat_id ON reaction_audit(chat_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_user_id ON reaction_audit(user_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reaction_audit_created_at ON reaction_audit(created_at)"))
        except Exception:
            logger.warning("Sprint X3 reaction_audit table creation failed", exc_info=True)

        # Sprint X4: tabela de watch de membros novos (TTL 24h). Usada pelo
        # preprocessor `new_member_watch_runtime` pra alertar o owner via
        # DM quando user recém-entrado posta link nas primeiras 5 msgs.
        try:
            if dialect_name == "postgresql":
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS new_member_watch (
                            id SERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            user_name VARCHAR,
                            user_username VARCHAR,
                            joined_at TIMESTAMP NOT NULL,
                            alerts_sent INTEGER NOT NULL DEFAULT 0,
                            CONSTRAINT uq_new_member_watch_chat_user
                                UNIQUE (chat_id, user_id)
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_chat_id ON new_member_watch(chat_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_user_id ON new_member_watch(user_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_joined_at ON new_member_watch(joined_at)"))
            else:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS new_member_watch (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            chat_id INTEGER NOT NULL,
                            user_id INTEGER NOT NULL,
                            user_name VARCHAR,
                            user_username VARCHAR,
                            joined_at DATETIME NOT NULL,
                            alerts_sent INTEGER NOT NULL DEFAULT 0,
                            CONSTRAINT uq_new_member_watch_chat_user
                                UNIQUE (chat_id, user_id)
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_chat_id ON new_member_watch(chat_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_user_id ON new_member_watch(user_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_new_member_watch_joined_at ON new_member_watch(joined_at)"))
        except Exception:
            logger.warning("Sprint X4 new_member_watch table creation failed", exc_info=True)


        # Cache de Canvas por file_id (/tcanvas e /tly). Colunas são todas
        # texto (sem BigInteger), então CREATE TABLE IF NOT EXISTS serve igual
        # pros dois dialetos.
        try:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS canvas_files (
                        track_id VARCHAR PRIMARY KEY,
                        file_id VARCHAR NOT NULL,
                        file_unique_id VARCHAR,
                        created_at """ + ("TIMESTAMP" if dialect_name == "postgresql" else "DATETIME") + """ NOT NULL,
                        updated_at """ + ("TIMESTAMP" if dialect_name == "postgresql" else "DATETIME") + """ NOT NULL
                    )
                    """
                )
            )
        except Exception:
            logger.warning("DB canvas_files table creation failed", exc_info=True)


def init_db() -> None:
    try:
        from app.models.card_message import CardMessage  # noqa: F401  # Sprint 8
        from app.models.canvas_file import CanvasFile  # noqa: F401  # cache file_id
        from app.models.lastfm_profile import LastfmProfile  # noqa: F401
        from app.models.spotify_token import SpotifyToken  # noqa: F401
        from app.models.track_like import TrackLike  # noqa: F401
        from app.models.track_play import TrackPlay  # noqa: F401
        from app.models.track_reaction import TrackReaction  # noqa: F401  # Sprint 8
        from app.models.reaction_audit import ReactionAudit  # noqa: F401  # Sprint X3
        from app.models.new_member_watch import NewMemberWatch  # noqa: F401  # Sprint X4

        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized.")
    except Exception as exc:
        logger.exception("Database initialization failed: %s", exc)
