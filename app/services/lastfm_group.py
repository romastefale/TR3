from __future__ import annotations

import asyncio
import html
import logging
from collections import Counter
from typing import Literal

import httpx

from app.config.settings import LASTFM_API_KEY
from app.services.lastfm_capsule import (
    CapsuleResult,
    HTTP_TIMEOUT_SECONDS,
    LastfmCapsuleService,
    _best_image_url,
    _fetch_image_bytes,
    _format_number,
    _plain,
    _text,
    _track_key,
    parse_month_spec,
)
from app.services.lastfm_weekly import parse_week_spec
from app.services.monthfm_card import CardArtist, CardTrack, MonthfmCardData

logger = logging.getLogger(__name__)

# Concorrência ao bater no Last.fm: 4 usuários em paralelo. Cada user pode
# disparar até MAX_RECENT_PAGES (20) chamadas em série; com 4 simultâneos
# o pico fica bem abaixo do limite prático de 5 req/s por API key.
GROUP_FETCH_CONCURRENCY = 4
GROUP_LIST_SIZE = 10
PeriodKind = Literal["week", "month"]


class LastfmGroupService(LastfmCapsuleService):
    """Agrega scrobbles de múltiplos Last.fm pra montar o ranking do grupo.

    Reaproveita `_recent_tracks` (paginado, com cap), `_estimate_minutes`,
    `_track_image_url` e `_fetch_image_bytes` da classe pai. O que muda
    aqui é só a etapa de coleta multi-usuário (concorrência limitada,
    falhas isoladas) e a agregação somando todos os scrobbles num único
    `Counter`.
    """

    async def build_group_capsule(
        self,
        *,
        chat_title: str,
        members: list[tuple[int, str]],
        period_kind: PeriodKind,
    ) -> CapsuleResult:
        if not LASTFM_API_KEY:
            return CapsuleResult("LASTFM_API_KEY ausente no Railway. Não consigo consultar o Last.fm.")
        if not members:
            return CapsuleResult(
                "Nenhum membro com Last.fm conectado pra esse ranking. "
                "Peça pra galera rodar <code>/lastfm seu_username</code>."
            )

        if period_kind == "week":
            spec = parse_week_spec(None)
            period_label = "RANKING SEMANAL"
        else:
            spec = parse_month_spec(None)
            period_label = "RANKING MENSAL"

        semaphore = asyncio.Semaphore(GROUP_FETCH_CONCURRENCY)

        async def _one(user_id: int, username: str) -> tuple[int, str, list, int, bool] | None:
            async with semaphore:
                try:
                    items, total, capped = await self._recent_tracks(username, spec)
                except Exception:
                    logger.exception(
                        "GROUP_FETCH_FAILED | user_id=%s | username=%s", user_id, username
                    )
                    return None
                return user_id, username, items, total, capped

        gathered = await asyncio.gather(
            *(_one(uid, uname) for uid, uname in members),
            return_exceptions=False,
        )

        track_counts: Counter[tuple[str, str]] = Counter()
        artist_counts: Counter[str] = Counter()
        image_urls: dict[tuple[str, str], str] = {}
        total_scrobbles_sum = 0
        successful = 0
        contributors = 0

        for entry in gathered:
            if entry is None:
                continue
            _uid, _uname, items, total, _capped = entry
            successful += 1
            if not items:
                continue
            contributors += 1
            total_scrobbles_sum += int(total or len(items))
            for item in items:
                track = _text(item.get("name"))
                artist = _text(item.get("artist"))
                if not track or not artist:
                    continue
                key = _track_key(artist, track)
                track_counts[key] += 1
                artist_counts[artist] += 1
                image_url = _best_image_url(item.get("image"))
                if image_url and key not in image_urls:
                    image_urls[key] = image_url

        if not track_counts:
            return CapsuleResult(
                f"♫ {html.escape(chat_title)}\n\n"
                f"Ninguém com scrobbles registrados no período. "
                f"({successful} de {len(members)} membros consultados)"
            )

        minutes, _, _ = await self._estimate_minutes(track_counts)

        top_artists = artist_counts.most_common(GROUP_LIST_SIZE)
        top_tracks = track_counts.most_common(GROUP_LIST_SIZE)

        hero_key = top_tracks[0][0] if top_tracks else None
        hero_image = image_urls.get(hero_key) if hero_key else None
        hero_artist_name = top_tracks[0][0][0] if top_tracks else ""
        hero_track_title = top_tracks[0][0][1] if top_tracks else ""
        hero_plays_count = top_tracks[0][1] if top_tracks else 0
        hero_image_bytes: bytes | None = None
        if hero_key:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                upgraded_url = await self._track_image_url(client, hero_artist_name, hero_track_title)
            best_url = upgraded_url or hero_image
            if best_url:
                hero_image = best_url
                hero_image_bytes = await _fetch_image_bytes(best_url)

        card_data = MonthfmCardData(
            title=chat_title,
            theme="dark",
            period_label=period_label,
            period_value=spec.label.upper(),
            hero_image_url=hero_image,
            hero_image_bytes=hero_image_bytes,
            hero_track=hero_track_title,
            hero_artist=hero_artist_name,
            hero_plays=hero_plays_count,
            top_artists=tuple(CardArtist(name=a, count=c) for a, c in top_artists),
            top_tracks=tuple(CardTrack(title=t, artist=a, plays=c) for (a, t), c in top_tracks),
            total_scrobbles=total_scrobbles_sum,
            minutes=minutes,
            list_size=GROUP_LIST_SIZE,
        )

        # Texto fallback (caso o card não renderize) — descritivo, com top 10.
        safe_title = _plain(chat_title)
        lines = [
            f"♫ Top 10 de {safe_title}",
            _plain(spec.label),
            f"{contributors} de {len(members)} membros com scrobbles no período",
            "",
            "✦ Top 10 artistas",
        ]
        for idx, (artist, count) in enumerate(top_artists, 1):
            lines.append(f"{idx:02d}. {_plain(artist[:30])} — {_format_number(count)} plays")
        lines.extend(["", "♫ Top 10 músicas"])
        for idx, ((artist, track), count) in enumerate(top_tracks, 1):
            lines.append(
                f"{idx:02d}. {_plain(track[:36])} — {_plain(artist[:22])} {_format_number(count)} plays"
            )
        lines.extend(["", f"⌁ Total agregado: {_format_number(total_scrobbles_sum)} scrobbles"])
        if minutes:
            lines.append(f"aprox. {_format_number(minutes)} minutos de música no grupo")

        return CapsuleResult("\n".join(lines), card_data=card_data)


lastfm_group_service = LastfmGroupService()
