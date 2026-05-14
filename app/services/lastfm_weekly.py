from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.settings import LASTFM_API_KEY
from app.services.lastfm import lastfm_service
from app.services.lastfm_capsule import (
    CapsuleResult,
    LastfmCapsuleService,
    MIN_COLLAGE_COVERS,
    MONTH_NAMES_PT,
    _bold,
    _format_number,
    _italic,
    _plain,
    _shorten,
    _text,
    _track_key,
    _best_image_url,
)


@dataclass(frozen=True)
class WeekSpec:
    label: str
    start_ts: int
    end_ts: int


def _date_label(value: datetime) -> str:
    return f"{value.day:02d} {MONTH_NAMES_PT[value.month]}"


def parse_week_spec(raw: str | None, now: datetime | None = None) -> WeekSpec:
    current = now or datetime.now(timezone.utc)
    parts = (raw or "").strip().split()

    if not parts:
        end = current
        start = end - timedelta(days=7)
    elif len(parts) == 1:
        start = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=7)
    elif len(parts) == 2:
        start = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(parts[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        raise ValueError("semana inválida")

    if end <= start:
        raise ValueError("intervalo inválido")
    if (end - start).days > 31:
        raise ValueError("intervalo muito longo")

    if start.year == end.year and start.month == end.month:
        label = f"{start.day:02d}–{end.day:02d} {MONTH_NAMES_PT[start.month]} {start.year}"
    else:
        label = f"{_date_label(start)}–{_date_label(end)} {end.year}"

    return WeekSpec(label=label, start_ts=int(start.timestamp()), end_ts=int(end.timestamp()))


class LastfmWeeklyService(LastfmCapsuleService):
    async def build_capsule(self, user_id: int, display_name: str, raw_week: str | None = None) -> CapsuleResult:
        username = await lastfm_service.get_username(user_id)
        if not username:
            return CapsuleResult("Use /lastfm <username> antes de gerar o extrato da semana.")
        if not LASTFM_API_KEY:
            return CapsuleResult("LASTFM_API_KEY ausente no Railway. Não consigo consultar o Last.fm.")

        try:
            spec = parse_week_spec(raw_week)
        except Exception:
            return CapsuleResult("Semana inválida. Use /weekfm, /weekfm 2026-05-06 ou /weekfm 2026-05-06 2026-05-13.")

        recent_items, total_reported, capped = await self._recent_tracks(username, spec)  # type: ignore[arg-type]
        if not recent_items:
            return CapsuleResult(f"♫ Extrato da semana\n{_plain(spec.label)}\n\nNenhum scrobble encontrado para @{_plain(username)} nesse período.")

        track_counts: Counter[tuple[str, str]] = Counter()
        artist_counts: Counter[str] = Counter()
        album_counts: Counter[tuple[str, str]] = Counter()
        image_urls: dict[tuple[str, str], str] = {}

        for item in recent_items:
            track = _text(item.get("name"))
            artist = _text(item.get("artist"))
            album = _text(item.get("album"))
            if track and artist:
                key = _track_key(artist, track)
                track_counts[key] += 1
                artist_counts[artist] += 1
                image_url = _best_image_url(item.get("image"))
                if image_url and key not in image_urls:
                    image_urls[key] = image_url
            if album and artist:
                album_counts[(artist, album)] += 1

        minutes, _, _ = await self._estimate_minutes(track_counts)
        photo_bytes = await self._build_collage(track_counts.most_common(MIN_COLLAGE_COVERS), image_urls)

        safe_name = _plain(display_name or username)
        lines: list[str] = [
            f"{safe_name} · ♫ Extrato da semana",
            _plain(spec.label),
            "",
            "✦ Top artistas",
        ]

        for idx, (artist, count) in enumerate(artist_counts.most_common(5), 1):
            lines.append(f"{idx}. {_plain(_shorten(artist))} — {_format_number(count)} scrobbles")

        lines.extend(["", "♫ Top músicas"])
        for idx, ((artist, track), count) in enumerate(track_counts.most_common(5), 1):
            lines.append(f"{idx}. {_bold(_shorten(track, 42))} — {_italic(_shorten(artist, 24))} {_format_number(count)} plays")

        lines.extend(["", "◌ Disco mais ouvido"])
        if album_counts:
            (album_artist, album_name), album_count = album_counts.most_common(1)[0]
            lines.append(_plain(_shorten(album_name, 44)))
            lines.append(f"{_plain(_shorten(album_artist, 30))} · {_format_number(album_count)} scrobbles")
        else:
            lines.append("Sem álbum identificado nos scrobbles da semana.")

        lines.extend(["", "⌁ Total da semana"])
        lines.append(f"{_format_number(total_reported)} scrobbles")
        if minutes is not None:
            lines.append(f"aprox. {_format_number(minutes)} minutos ouvidos")
        else:
            lines.append("minutos ouvidos indisponíveis")

        if capped:
            lines.extend(["", "Resultado parcial: o período tem mais scrobbles do que o limite seguro de leitura do bot."])

        return CapsuleResult("\n".join(lines), photo_bytes=photo_bytes)


lastfm_weekly_service = LastfmWeeklyService()
