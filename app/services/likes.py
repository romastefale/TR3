from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.track_like import TrackLike
from app.models.track_play import TrackPlay


class LikesService:
    def _new_session(self) -> Session:
        return SessionLocal()

    def _normalize_optional_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    async def register_play(
        self,
        user_id: int,
        track_id: str,
        track_name: str | None = None,
        artist_name: str | None = None,
    ) -> None:
        with self._new_session() as db:
            db.add(
                TrackPlay(
                    user_id=user_id,
                    track_id=track_id,
                    track_name=self._normalize_optional_text(track_name),
                    artist_name=self._normalize_optional_text(artist_name),
                )
            )
            db.commit()

    async def get_track_metadata(self, track_id: str, owner_user_id: int | None = None) -> tuple[str | None, str | None]:
        with self._new_session() as db:
            query = db.query(TrackPlay.track_name, TrackPlay.artist_name).filter(TrackPlay.track_id == track_id)
            if owner_user_id is not None:
                query = query.filter(TrackPlay.user_id == owner_user_id)
            row = query.order_by(TrackPlay.id.desc()).first()
            if row:
                return self._normalize_optional_text(row[0]), self._normalize_optional_text(row[1])

            like_query = db.query(TrackLike.track_name, TrackLike.artist_name).filter(TrackLike.track_id == track_id)
            if owner_user_id is not None:
                like_query = like_query.filter(TrackLike.owner_user_id == owner_user_id)
            like_row = like_query.order_by(TrackLike.id.desc()).first()
            if like_row:
                return self._normalize_optional_text(like_row[0]), self._normalize_optional_text(like_row[1])

        return None, None

    async def get_track_play_count(self, track_id: str) -> int:
        with self._new_session() as db:
            return int(db.execute(select(func.count(TrackPlay.id)).where(TrackPlay.track_id == track_id)).scalar_one())

    async def get_user_play_count(self, user_id: int, track_id: str) -> int:
        with self._new_session() as db:
            return int(
                db.execute(
                    select(func.count(TrackPlay.id)).where(
                        TrackPlay.user_id == user_id,
                        TrackPlay.track_id == track_id,
                    )
                ).scalar_one()
            )

    async def is_track_liked(self, user_id: int, track_id: str, owner_user_id: int | None = None) -> bool:
        with self._new_session() as db:
            stmt = (
                select(TrackLike.id)
                .where(
                    TrackLike.user_id == user_id,
                    TrackLike.owner_user_id == owner_user_id,
                    TrackLike.track_id == track_id,
                    func.coalesce(TrackLike.liked, 1) == 1,
                )
                .limit(1)
            )
            return db.execute(stmt).first() is not None

    async def get_total_likes(self, track_id: str, owner_user_id: int | None = None) -> int:
        with self._new_session() as db:
            stmt = select(func.count(TrackLike.id)).where(
                TrackLike.track_id == track_id,
                TrackLike.owner_user_id == owner_user_id,
                func.coalesce(TrackLike.liked, 1) == 1,
            )
            return int(db.execute(stmt).scalar_one())

    async def get_user_received_likes(self, user_id: int) -> int:
        with self._new_session() as db:
            stmt = select(func.count(TrackLike.id)).where(
                TrackLike.owner_user_id == user_id,
                func.coalesce(TrackLike.liked, 1) == 1,
            )
            return int(db.execute(stmt).scalar_one())

    async def get_user_total_likes(self, user_id: int) -> int:
        with self._new_session() as db:
            stmt = select(func.count(TrackLike.id)).where(
                TrackLike.user_id == user_id,
                func.coalesce(TrackLike.liked, 1) == 1,
            )
            return int(db.execute(stmt).scalar_one())

    async def get_user_top_tracks(self, user_id: int, limit: int = 5) -> list[tuple[str, int]]:
        with self._new_session() as db:
            rows = db.query(
                TrackPlay.track_id,
                func.coalesce(func.max(TrackPlay.track_name), TrackPlay.track_id),
                func.count(TrackPlay.id),
            ).filter(TrackPlay.user_id == user_id).group_by(TrackPlay.track_id).order_by(func.count(TrackPlay.id).desc()).limit(limit).all()
            return [(str(row[1]), int(row[2])) for row in rows]

    async def get_user_top_artists(self, user_id: int, limit: int = 5) -> list[tuple[str, int]]:
        with self._new_session() as db:
            rows = db.query(
                TrackPlay.artist_name,
                func.count(TrackPlay.id),
            ).filter(TrackPlay.user_id == user_id, TrackPlay.artist_name.isnot(None)).group_by(TrackPlay.artist_name).order_by(func.count(TrackPlay.id).desc()).limit(limit).all()
            return [(str(row[0]), int(row[1])) for row in rows]

    async def get_top_tracks(self, limit: int = 5) -> list[tuple[str, int]]:
        with self._new_session() as db:
            rows = db.query(
                TrackPlay.track_id,
                func.coalesce(func.max(TrackPlay.track_name), TrackPlay.track_id),
                func.count(TrackPlay.id),
            ).group_by(TrackPlay.track_id).order_by(func.count(TrackPlay.id).desc()).limit(limit).all()
            return [(str(row[1]), int(row[2])) for row in rows]

    async def get_top_artists(self, limit: int = 5) -> list[tuple[str, int]]:
        with self._new_session() as db:
            rows = db.query(
                TrackPlay.artist_name,
                func.count(TrackPlay.id),
            ).filter(TrackPlay.artist_name.isnot(None)).group_by(TrackPlay.artist_name).order_by(func.count(TrackPlay.id).desc()).limit(limit).all()
            return [(str(row[0]), int(row[1])) for row in rows]

    async def get_most_liked_tracks(self, limit: int = 5) -> list[tuple[str, int]]:
        with self._new_session() as db:
            rows = db.query(
                TrackLike.track_id,
                func.coalesce(func.max(TrackLike.track_name), TrackLike.track_id),
                func.count(TrackLike.id),
            ).filter(func.coalesce(TrackLike.liked, 1) == 1).group_by(TrackLike.track_id).order_by(func.count(TrackLike.id).desc()).limit(limit).all()
            return [(str(row[1]), int(row[2])) for row in rows]

    async def toggle_track_like(
        self,
        user_id: int,
        owner_user_id: int | None,
        track_id: str,
        track_name: str | None = None,
        artist_name: str | None = None,
    ) -> bool:
        with self._new_session() as db:
            try:
                existing = db.execute(
                    select(TrackLike)
                    .where(
                        TrackLike.user_id == user_id,
                        TrackLike.owner_user_id == owner_user_id,
                        TrackLike.track_id == track_id,
                    )
                    .order_by(TrackLike.id.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if existing:
                    current_liked = 1 if existing.liked is None else int(existing.liked)
                    existing.liked = 0 if current_liked == 1 else 1
                    db.commit()
                    return bool(existing.liked == 1)

                db.add(
                    TrackLike(
                        user_id=user_id,
                        owner_user_id=owner_user_id,
                        track_id=track_id,
                        track_name=self._normalize_optional_text(track_name),
                        artist_name=self._normalize_optional_text(artist_name),
                        liked=1,
                    )
                )
                db.commit()
                return True
            except IntegrityError:
                db.rollback()

        return await self.is_track_liked(user_id, track_id, owner_user_id=owner_user_id)


likes_service = LikesService()
