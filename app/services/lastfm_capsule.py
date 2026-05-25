from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import logging
import socket
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw

from app.config.settings import LASTFM_API_BASE_URL, LASTFM_API_KEY
from app.services.lastfm import lastfm_service
from app.services.monthfm_card import CardArtist, CardTrack, MonthfmCardData

logger = logging.getLogger(__name__)

RECENT_LIMIT = 200
MAX_RECENT_PAGES = 20
MAX_DURATION_LOOKUPS = 80
HTTP_TIMEOUT_SECONDS = 8.0
COLLAGE_SIZE = 1024
COVER_SIZE = COLLAGE_SIZE // 2
MIN_COLLAGE_COVERS = 4

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


@dataclass(frozen=True)
class CapsuleResult:
    text: str
    photo_bytes: bytes | None = None
    card_data: MonthfmCardData | None = None


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


def _best_image_url(images: Any) -> str | None:
    if not isinstance(images, list):
        return None
    for preferred_size in ("mega", "extralarge", "large", "medium", "small"):
        for image in images:
            if isinstance(image, dict) and image.get("size") == preferred_size:
                url = _text(image)
                if url:
                    return url
    for image in reversed(images):
        url = _text(image)
        if url:
            return url
    return None


async def _host_resolves_public(host: str) -> bool:
    """Return True only if every A/AAAA resolves to a routable public IP."""
    if not host:
        return False
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


async def _fetch_image_bytes(
    url: str,
    *,
    max_bytes: int = 4_000_000,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    max_redirects: int = 3,
) -> bytes | None:
    """Safely download a remote image.

    Enforces: https/http scheme, public-only DNS (anti-SSRF), manual redirect
    handling with per-hop revalidation, hard byte limit during streaming,
    strict image/* content-type. Returns None on any failure.
    """
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(max_redirects + 1):
            if not current or not current.lower().startswith(("http://", "https://")):
                return None
            parsed = urlparse(current)
            host = parsed.hostname or ""
            if not await _host_resolves_public(host):
                logger.debug("HERO_IMAGE_FETCH_BLOCKED | host=%s", host)
                return None
            try:
                async with client.stream("GET", current) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = str(httpx.URL(current).join(location))
                        continue
                    if status != 200:
                        return None
                    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    # Accept image/*, octet-stream, or missing header. Bytes are
                    # validated downstream by Pillow before any use.
                    if content_type and not (
                        content_type.startswith("image/")
                        or content_type == "application/octet-stream"
                    ):
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks) if chunks else None
            except Exception:
                logger.debug("HERO_IMAGE_FETCH_FAILED | url=%s", current, exc_info=True)
                return None
    return None


def _plain(value: str) -> str:
    return html.escape(value, quote=False)


def _bold(value: str) -> str:
    return f"<b>{_plain(value)}</b>"


def _italic(value: str) -> str:
    return f"<i>{_plain(value)}</i>"


def _fit_cover(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    if width <= 0 or height <= 0:
        return Image.new("RGB", (size, size), (24, 24, 24))
    scale = max(size / width, size / height)
    new_size = (max(size, int(width * scale)), max(size, int(height * scale)))
    image = image.resize(new_size, Image.LANCZOS)
    left = (image.width - size) // 2
    top = (image.height - size) // 2
    return image.crop((left, top, left + size, top + size))


class LastfmCapsuleService:
    def __init__(self) -> None:
        # Sprint 5 (R5.01): pool httpx compartilhado. Antes cada chamada
        # (_recent_tracks, _estimate_minutes, _build_collage, hero upgrade
        # em build_capsule) abria AsyncClient próprio — somando 3-4 TLS
        # handshakes por execução de /monthfm. Lazy init, fechado em
        # shutdown() pelo on_shutdown do FastAPI.
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.exception("Last.fm capsule httpx pool close failed")
            self._http = None
        logger.info("Last.fm capsule service stopped.")

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

    async def _recent_tracks(self, username: str, spec: Any) -> tuple[list[dict[str, Any]], int, bool]:
        tracks: list[dict[str, Any]] = []
        total_reported = 0
        capped = False
        # Sprint 5 (R5.01): usa pool compartilhado em vez de AsyncClient
        # próprio. Páginas continuam seriais (paginação Last.fm depende
        # do total reportado na primeira página + early break por
        # total_pages, então paralelizar daria 0 ganho na maioria).
        client = self._client()
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

    async def _track_image_url(self, client: httpx.AsyncClient, artist: str, track: str) -> str | None:
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
        album = track_data.get("album")
        if isinstance(album, dict):
            return _best_image_url(album.get("image"))
        return None

    async def _estimate_minutes(self, track_counts: Counter[tuple[str, str]]) -> tuple[int | None, int, int]:
        if not track_counts:
            return None, 0, 0
        # Sprint 5 (P5.01 + R5.01): paraleliza até MAX_DURATION_LOOKUPS
        # buscas de duração via asyncio.gather com semáforo (cap=8 pra
        # não estourar rate limit da Last.fm). Antes era serial — uma
        # geração de /monthfm acumulava 10-30s só nesse loop.
        items = list(track_counts.most_common(MAX_DURATION_LOOKUPS))
        client = self._client()
        sem = asyncio.Semaphore(8)

        async def fetch(artist: str, track: str) -> int | None:
            async with sem:
                return await self._track_duration_seconds(client, artist, track)

        durations = await asyncio.gather(*(fetch(a, t) for (a, t), _ in items))
        covered_plays = 0
        total_seconds = 0
        for ((_, _), plays), duration in zip(items, durations):
            if duration:
                covered_plays += int(plays)
                total_seconds += int(plays) * duration
        looked_up = len(items)
        if total_seconds <= 0:
            return None, looked_up, covered_plays
        return round(total_seconds / 60), looked_up, covered_plays

    async def _build_collage(self, top_tracks: list[tuple[tuple[str, str], int]], image_urls: dict[tuple[str, str], str]) -> bytes | None:
        if len(top_tracks) < MIN_COLLAGE_COVERS:
            return None
        # Sprint 5 (P5.02 + R5.01): cada cover do collage é um pipeline
        # independente (lookup URL + download payload). Antes serial — 4
        # round-trips em cadeia. Agora gather paralelo. Decode PIL fica
        # fora do gather pra não bloquear o loop com CPU em paralelo
        # (decode em série após I/O terminar).
        client = self._client()

        async def fetch_cover(artist: str, track: str) -> bytes | None:
            key = _track_key(artist, track)
            url = image_urls.get(key) or await self._track_image_url(client, artist, track)
            if not url:
                return None
            payload = await _fetch_image_bytes(url)
            if not payload:
                logger.debug("MONTHFM_COLLAGE_FETCH_FAILED | artist=%s | track=%s", artist, track)
                return None
            return payload

        top = top_tracks[:MIN_COLLAGE_COVERS]
        payloads = await asyncio.gather(*(fetch_cover(a, t) for (a, t), _ in top))
        if any(p is None for p in payloads):
            return None

        covers: list[Image.Image] = []
        for payload, ((artist, track), _) in zip(payloads, top):
            try:
                with Image.open(io.BytesIO(payload)) as raw:  # type: ignore[arg-type]
                    covers.append(_fit_cover(raw, COVER_SIZE))
            except Exception:
                logger.exception("Failed to decode monthfm cover | artist=%s | track=%s", artist, track)
                return None
        if len(covers) != MIN_COLLAGE_COVERS:
            return None

        collage = Image.new("RGB", (COLLAGE_SIZE, COLLAGE_SIZE), (10, 10, 10))
        positions = [(0, 0), (COVER_SIZE, 0), (0, COVER_SIZE), (COVER_SIZE, COVER_SIZE)]
        for cover, position in zip(covers, positions, strict=True):
            collage.paste(cover, position)
        draw = ImageDraw.Draw(collage)
        draw.line((COVER_SIZE, 0, COVER_SIZE, COLLAGE_SIZE), fill=(12, 12, 12), width=6)
        draw.line((0, COVER_SIZE, COLLAGE_SIZE, COVER_SIZE), fill=(12, 12, 12), width=6)
        output = io.BytesIO()
        collage.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()

    async def build_capsule(self, user_id: int, display_name: str, raw_month: str | None = None) -> CapsuleResult:
        username = await lastfm_service.get_username(user_id)
        if not username:
            return CapsuleResult(
                "Você ainda não conectou um Last.fm. "
                "Use <code>/lastfm seu_username</code> (sem o @) antes de gerar a cápsula mensal."
            )
        if not LASTFM_API_KEY:
            return CapsuleResult("LASTFM_API_KEY ausente no Railway. Não consigo consultar o Last.fm.")

        try:
            spec = parse_month_spec(raw_month)
        except Exception:
            return CapsuleResult("Mês inválido. Use /monthfm, /monthfm 05 ou /monthfm 2026-05.")

        recent_items, total_reported, capped = await self._recent_tracks(username, spec)
        if not recent_items:
            return CapsuleResult(f"♫ Extrato de {html.escape(spec.label)}\n\nNenhum scrobble encontrado para @{html.escape(username)} nesse mês.")

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

        top_artists = artist_counts.most_common(5)
        top_tracks = track_counts.most_common(5)
        top_album = album_counts.most_common(1)
        album_artist = top_album[0][0][0] if top_album else "Last.fm"
        album_name = top_album[0][0][1] if top_album else "Sem disco identificado"
        album_count = top_album[0][1] if top_album else 0
        hero_key = top_tracks[0][0] if top_tracks else None
        hero_image = image_urls.get(hero_key) if hero_key else None
        hero_artist_name = top_tracks[0][0][0] if top_tracks else ""
        hero_track_title = top_tracks[0][0][1] if top_tracks else ""
        hero_plays_count = top_tracks[0][1] if top_tracks else 0
        hero_image_bytes: bytes | None = None
        if hero_key:
            # Sprint 5 (R5.01): reaproveita pool compartilhado.
            upgraded_url = await self._track_image_url(
                self._client(), hero_artist_name, hero_track_title
            )
            best_url = upgraded_url or hero_image
            if best_url:
                hero_image = best_url
                hero_image_bytes = await _fetch_image_bytes(best_url)

        card_data = MonthfmCardData(
            title=f"Extrato de {spec.label}",
            theme="dark",
            period_label="EXTRATO DE",
            period_value=spec.label.upper(),
            hero_image_url=hero_image,
            hero_image_bytes=hero_image_bytes,
            hero_track=hero_track_title,
            hero_artist=hero_artist_name,
            hero_plays=hero_plays_count,
            top_artists=tuple(CardArtist(name=artist, count=count) for artist, count in top_artists),
            top_tracks=tuple(CardTrack(title=track, artist=artist, plays=count) for (artist, track), count in top_tracks),
            album_name=album_name,
            album_artist=album_artist,
            album_count=album_count,
            total_scrobbles=total_reported,
            minutes=minutes,
        )

        safe_name = _plain(display_name or username)
        lines: list[str] = [
            f"{safe_name} · ♫ Extrato de {_plain(spec.label)}",
            "",
            "✦ Top artistas",
        ]

        for idx, (artist, count) in enumerate(top_artists, 1):
            lines.append(f"{idx}. {_plain(_shorten(artist))} — {_format_number(count)} scrobbles")

        lines.extend(["", "♫ Top músicas"])
        for idx, ((artist, track), count) in enumerate(top_tracks, 1):
            lines.append(f"{idx}. {_bold(_shorten(track, 42))} — {_italic(_shorten(artist, 24))} {_format_number(count)} plays")

        lines.extend(["", "◌ Disco mais ouvido"])
        if top_album:
            lines.append(_plain(_shorten(album_name, 44)))
            lines.append(f"{_plain(_shorten(album_artist, 30))} · {_format_number(album_count)} scrobbles")
        else:
            lines.append("Sem álbum identificado nos scrobbles do mês.")

        lines.extend(["", "⌁ Total do mês"])
        lines.append(f"{_format_number(total_reported)} scrobbles")
        if minutes is not None:
            lines.append(f"aprox. {_format_number(minutes)} minutos ouvidos")
        else:
            lines.append("minutos ouvidos indisponíveis")

        if capped:
            lines.extend(["", "Resultado parcial: o mês tem mais scrobbles do que o limite seguro de leitura do bot."])

        return CapsuleResult("\n".join(lines), photo_bytes=photo_bytes, card_data=card_data)

    async def build_capsule_text(self, user_id: int, display_name: str, raw_month: str | None = None) -> str:
        result = await self.build_capsule(user_id, display_name, raw_month)
        return result.text


lastfm_capsule_service = LastfmCapsuleService()
