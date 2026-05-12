from __future__ import annotations

from typing import Any

from app.services.spotify import spotify_service


class MusicService:
    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        return await spotify_service.get_current_or_last_played(user_id)


music_service = MusicService()
