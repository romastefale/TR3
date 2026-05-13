from __future__ import annotations

import calendar
import html
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config.settings import LASTFM_API_BASE_URL, LASTFM_API_KEY
from app.services.lastfm import lastfm_service

logger = logging.getLogger(__name__)

RECENT_LIMIT = 1000
MAX_RECENT_PAGES = 10
MAX_DURATION_LOOKUPS = 200
HTTP_TIMEOUT_SECONDS = 8.0

MONTH_NAMES_PT = {
    1: "janeiro",
    2: "fevereiro",
    3: "março",
    4: "abril",
    5: "maio",
    6: "junho",
    7: "julho",
    8: "agosto",
    9: "setembro",
    10: "outubro",
    11: "novembro",
    12: "dezembro",
}


@dataclass(frozen=True)
class MonthSpec:
    year: int
    month: int
    label: str
    start_ts: int
    end_ts: int


def parse_month_spec(raw: str | None, now: datetime | None = None) -> MonthSpec:
    current = now or datetime.now(timezone.utc)
    value = (raw or "").strip()

    if not value:
        year = current.year
        month = current.month
    elif "-" in value:
        year_part, month_part = value.split("-", 1)
        year = int(year_part)
        month = int(month_part)
    else:
        year = current.year
        month = int(value)

    if year < 2002 or year > current.year + 1:
        raise ValueError("ano inválido")
    if month < 1 or month > 12:
        raise ValueError("mês inválido")

    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    label = f"{MONTH_NAMES_PT[month].capitalize()} {year}"
    return MonthSpec(year=year, month=month, label=label, start_ts=int(start.timestamp()), end_ts=int(end.timestamp()))


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("#text") or value.get("name") or "").strip()
    return str(value or "").strip()


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _format_number(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _shorten(value: str, limit: int = 42) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _track_key(artist: str, track: str) -> tuple[str, str]:
    return (artist.strip(), track.strip())


class LastfmCapsuleService:
    async def _api_get(self, client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any] | None:
        if not LASTFM_API_KEY:
            return None
        full_params = {**params, "api_key": LASTFM_API_KEY, "format": "json"}
        try:
            response = await client.get(LASTFM_API_BASE_URL, params=full_params)
        except Exception:
            logger.exception("Last.fm capsule request failed")
            return None
        if response.status_code != 200:
            logger.warning("Last.fm capsule status=%s body=%s", response.status_code, response.text[:300])
            return None
        try:
            data = response.json()
        except Exception:
            logger.exception("Last.fm capsule invalid json")
            return None
        if isinstance(data, dict) and data.get("error"):
            logger.warning("Last.fm capsule API error=%s message=%s", data.get("error"), data.get("message"))
            return None
        return data if isinstance(data, dict) else None

    async def _recent_tracks(self, username: str, spec: MonthSpec) -> tuple[list[dict[str, Any]], int, bool]:
        tracks: list[dict[str, Any]] = []
        total_reported = 0
        capped = False
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for page in range(1, MAX_RECENT_PAGES + 1):
                data = await self._api_get(
                    client,
                    {
                        "method": "user.getrecenttracks",
                        "user": username,
                        "from": str(spec.start_ts),
                        "to": str(spec.end_ts - 1),
                        "limit": str(RECENT_LIMIT),
                        "page": str(page),
                        "extended": "1",
                    },
                )
                if not data:
                    break
                recent = data.get("recenttracks") or {}
                attr = recent.get("@attr") or {}
                total_reported = _safe_int(attr.get("total")) or total_reported
                total_pages = _safe_int(attr.get("totalPages")) or 1
                page_items = recent.get("track") or []
                if isinstance(page_items, dict):
                    page_items = [page_items]
                if isinstance(page_items, list):
                    tracks.extend(item for item in page_items if isinstance(item, dict))
                if page >= total_pages:
                    break
                if page == MAX_RECENT_PAGES and total_pages > MAX_RECENT_PAGES:
                    capped = True
        return tracks, total_reported or len(tracks), capped

    async def _track_duration_seconds(self, client: httpx.AsyncClient, artist: str, track: str) -> int | None:
        data = await self._api_get(
            client,
            {
                "method": "track.getInfo",
                "artist": artist,
                "track": track,
                "autocorrect": "1",
            },
        )
        if not data:
            return None
        track_data = data.get("track")
        if not isinstance(track_data, dict):
            return None
        duration_ms = _safe_int(track_data.get("duration"))
        if not duration_ms or duration_ms < 10_000:
            return None
        return max(1, int(duration_ms / 1000))

    async def _estimate_minutes(self, track_counts: Counter[tuple[str, str]]) -> tuple[int | None, int, int]:
        if not track_counts:
            return None, 0, 0
        looked_up = 0
        covered_plays = 0
        total_seconds = 0
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for (artist, track), plays in track_counts.most_common(MAX_DURATION_LOOKUPS):
                duration = await self._track_duration_seconds(client, artist, track)
                looked_up += 1
                if duration:
                    covered_plays += int(plays)
                    total_seconds += int(plays) * duration
        if total_seconds <= 0:
            return None, looked_up, covered_plays
        return round(total_seconds / 60), looked_up, covered_plays

    async def build_capsule_text(self, user_id: int, display_name: str, raw_month: str | None = None) -> str:
        username = await lastfm_service.get_username(user_id)
        if not username:
            return "Use /lastfm <username> antes de gerar a cápsula mensal."
        if not LASTFM_API_KEY:
            return "LASTFM_API_KEY ausente no Railway. Não consigo consultar o Last.fm."

        try:
            spec = parse_month_spec(raw_month)
        except Exception:
            return "Mês inválido. Use /monthfm, /monthfm 05 ou /monthfm 2026-05."

        recent_items, total_reported, capped = await self._recent_tracks(username, spec)
        if not recent_items:
            return f"♫ <b>{html.escape(spec.label)} Capsule</b>\n\nNenhum scrobble encontrado para @{html.escape(username)} nesse mês."

        track_counts: Counter[tuple[str, str]] = Counter()
        artist_counts: Counter[str] = Counter()
        album_counts: Counter[tuple[str, str]] = Counter()

        for item in recent_items:
            track = _text(item.get("name"))
            artist = _text(item.get("artist"))
            album = _text(item.get("album"))
            if track and artist:
                track_counts[_track_key(artist, track)] += 1
                artist_counts[artist] += 1
            if album and artist:
                album_counts[(artist, album)] += 1

        minutes, duration_lookups, covered_plays = await self._estimate_minutes(track_counts)

        safe_name = html.escape(display_name or username)
        lines: list[str] = [
            f"♫ <b>{html.escape(spec.label)} Sound Capsule</b>",
            "",
            f"<b>{safe_name}</b> · Last.fm mensal",
            "",
            "★ <b>Top artistas</b>",
        ]

        for idx, (artist, count) in enumerate(artist_counts.most_common(5), 1):
            lines.append(f"{idx}. {html.escape(_shorten(artist))} — <code>{_format_number(count)}</code>")

        lines.extend(["", "♫ <b>Top músicas</b>"])
        for idx, ((artist, track), count) in enumerate(track_counts.most_common(5), 1):
            label = f"{track} — {artist}"
            lines.append(f"{idx}. {html.escape(_shorten(label, 48))} — <code>{_format_number(count)}</code>")

        lines.extend(["", "◎ <b>Disco mais ouvido</b>"])
        if album_counts:
            (album_artist, album_name), album_count = album_counts.most_common(1)[0]
            lines.append(f"{html.escape(_shorten(album_name, 44))} — {html.escape(_shorten(album_artist, 30))}")
            lines.append(f"<code>{_format_number(album_count)}</code> scrobbles")
        else:
            lines.append("Sem álbum identificado nos scrobbles do mês.")

        lines.extend(["", "⌁ <b>Total do mês</b>"])
        lines.append(f"<code>{_format_number(total_reported)}</code> scrobbles")
        if minutes is not None:
            coverage_note = ""
            if covered_plays and total_reported:
                coverage = round((covered_plays / max(total_reported, 1)) * 100)
                coverage_note = f" · cobertura {coverage}%"
            lines.append(f"aprox. <code>{_format_number(minutes)}</code> minutos ouvidos{coverage_note}")
        else:
            lines.append("minutos ouvidos indisponíveis: o Last.fm não retornou duração suficiente das faixas")

        if capped:
            lines.extend(["", "⚠️ Resultado parcial: o mês tem mais scrobbles do que o limite seguro de leitura do bot."])
        elif minutes is not None and duration_lookups >= MAX_DURATION_LOOKUPS:
            lines.extend(["", "ℹ️ Minutos estimados por duração das faixas consultadas no Last.fm."])
        else:
            lines.extend(["", "ℹ️ Scrobbles calculados pelo histórico mensal do Last.fm; minutos são estimados por duração de faixas."])

        return "\n".join(lines)


lastfm_capsule_service = LastfmCapsuleService()
