from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config.settings import HTTP_TIMEOUT_SECONDS, LASTFM_API_BASE_URL, LASTFM_API_KEY
from app.db.database import SessionLocal
from app.models.lastfm_profile import LastfmProfile

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_COVER_TIMEOUT_SECONDS = 2.5
LASTFM_TRACK_INFO_TIMEOUT_SECONDS = 2.5


def _clean_username(username: str) -> str:
    """Aceita o que o user manda e tenta extrair o username puro do Last.fm.

    Tolera @ no começo, URL completa (`https://www.last.fm/user/<nome>`),
    espaços extras e barras finais. Só levanta ValueError se mesmo depois
    da limpeza o resultado não casar com o formato aceito pelo Last.fm.
    """
    value = (username or "").strip()
    # URL do tipo "https://www.last.fm/user/romastefale[/...]"
    url_match = re.search(r"last\.fm/user/([A-Za-z0-9_.-]{2,64})", value, re.IGNORECASE)
    if url_match:
        value = url_match.group(1)
    # Remove @ e espaços/barras grudados no começo ou fim.
    value = value.strip().strip("/").strip()
    value = value.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", value):
        raise ValueError("username Last.fm inválido")
    return value


def _stable_track_id(artist: str, track: str) -> str:
    raw = f"{artist}:{track}".lower().strip()
    raw = re.sub(r"\s+", " ", raw)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"lfm:{digest}"


def _normalize_match(value: str) -> str:
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_match(expected: str, found: str) -> bool:
    expected_norm = _normalize_match(expected)
    found_norm = _normalize_match(found)
    if not expected_norm or not found_norm:
        return False
    return expected_norm == found_norm or expected_norm in found_norm or found_norm in expected_norm


def _unique_queries(artist: str, track_name: str, album: str | None) -> list[str]:
    queries = [
        f'artist:"{artist}" track:"{track_name}"',
        f"{artist} {track_name}",
    ]
    if album:
        queries.append(f'artist:"{artist}" album:"{album}"')
        queries.append(f"{artist} {album} {track_name}")
    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        clean = re.sub(r"\s+", " ", query).strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed >= 0 else None


_USERNAME_CACHE_MAX = 4096


class LastfmService:
    def __init__(self) -> None:
        # Sprint 4 (S4.1): pool httpx compartilhado pra Last.fm + Deezer.
        # Antes /tnow abria 3 sockets novos por chamada (recent + deezer +
        # track.getInfo) — agora keepalive reaproveita conexões. Como os
        # 3 endpoints têm timeouts diferentes, o pool é criado com o
        # timeout "padrão" (HTTP_TIMEOUT_SECONDS) e cada `.get()` que
        # precisa de algo mais agressivo passa `timeout=` explícito.
        self._http: httpx.AsyncClient | None = None
        # Sprint 4 (S4.5): cache user_id -> username|None. `None` cacheia
        # "sabidamente sem Last.fm" pra evitar SELECT idêntico repetido
        # (hot path: /songcharts agrega N membros do grupo, cada um
        # passava por get_username; /tnow chama uma vez por execução).
        # Sem TTL — invalidação acontece nas 3 rotas de mutação
        # (set_username, manual_register, clear_username). Single-process
        # no Railway, então cache fica coerente. Cap em 4096 entradas
        # com eviction simples dos mais antigos pra bounded memory.
        self._username_cache: dict[int, str | None] = {}

    def _username_cache_set(self, user_id: int, value: str | None) -> None:
        self._username_cache[user_id] = value
        if len(self._username_cache) > _USERNAME_CACHE_MAX:
            # Descarta 25% (ordem de inserção do dict — Python 3.7+).
            drop = len(self._username_cache) // 4
            for key in list(self._username_cache.keys())[:drop]:
                self._username_cache.pop(key, None)

    def _username_cache_invalidate(self, user_id: int) -> None:
        self._username_cache.pop(user_id, None)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.exception("Last.fm httpx pool close failed")
            self._http = None
        logger.info("Last.fm service stopped.")

    async def set_username(self, user_id: int, username: str) -> tuple[str, str | None]:
        """Salva (ou substitui) o Last.fm do usuário.

        Devolve `(novo_username, username_anterior_ou_None)` pra que o handler
        possa avisar quando substituiu uma conexão antiga.
        """
        clean = _clean_username(username)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        previous: str | None = None
        with SessionLocal() as db:
            existing = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            if existing:
                previous = existing.username
                existing.username = clean
                existing.updated_at = now
            else:
                db.add(LastfmProfile(user_id=user_id, username=clean, created_at=now, updated_at=now))
            db.commit()
        # Sprint 4 (S4.5): atualiza cache com o valor novo (write-through
        # evita um SELECT extra na próxima leitura).
        self._username_cache_set(user_id, clean)
        return clean, previous

    async def manual_register(self, user_id: int, username: str) -> tuple[str, int]:
        """Cadastro manual (admin) do Last.fm de um terceiro.

        Apaga TODAS as linhas de `lastfm_profiles` daquele `user_id` (defesa
        em profundidade contra qualquer sujeira de tentativa anterior) e
        cria uma linha nova. Tudo numa única transação — ou tudo entra ou
        nada muda.

        Retorna `(novo_username, qtd_de_linhas_apagadas)`.
        """
        clean = _clean_username(username)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with SessionLocal() as db:
            deleted = (
                db.query(LastfmProfile).filter_by(user_id=user_id).delete(synchronize_session=False)
            )
            db.add(
                LastfmProfile(
                    user_id=user_id,
                    username=clean,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()
        # Sprint 4 (S4.5): write-through. manual_register substitui tudo
        # do user_id, então gravar `clean` direto é seguro.
        self._username_cache_set(user_id, clean)
        return clean, int(deleted or 0)

    async def clear_username(self, user_id: int) -> bool:
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            if profile:
                db.delete(profile)
                db.commit()
                # Sprint 4 (S4.5): marca como ausente no cache (None) —
                # próxima leitura responde sem SELECT.
                self._username_cache_set(user_id, None)
                return True
        # Sprint 4 (S4.5): mesmo quando não havia profile no banco,
        # registra ausência no cache pra evitar SELECT futuro.
        self._username_cache_set(user_id, None)
        return False

    async def get_username(self, user_id: int) -> str | None:
        # Sprint 4 (S4.5): cache hit serve user_id conhecido (com username
        # ou marcado como ausente via None) sem tocar no DB. Em hot paths
        # como /songcharts (itera N membros do grupo), poupa N SELECTs por
        # execução. Invalidação cuidada nas 3 rotas de mutação.
        if user_id in self._username_cache:
            return self._username_cache[user_id]
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            username = profile.username if profile else None
        self._username_cache_set(user_id, username)
        return username

    async def get_all_profiles(self) -> list[tuple[int, str]]:
        """Lista todos os Last.fm conectados como tuplas (user_id, username).

        Usado pelo ranking do grupo (`/songcharts`) pra enumerar a base e,
        em seguida, filtrar por presença no chat (no fluxo do grupo) ou
        agregar globalmente (no fluxo do owner em DM).
        """
        with SessionLocal() as db:
            rows = (
                db.query(LastfmProfile.user_id, LastfmProfile.username)
                .order_by(LastfmProfile.user_id.asc())
                .all()
            )
        return [
            (int(user_id), str(username).strip())
            for user_id, username in rows
            if user_id is not None and username and str(username).strip()
        ]

    async def get_user_track_playcount(self, user_id: int, artist: str, track_name: str) -> int | None:
        username = await self.get_username(user_id)
        if not username or not LASTFM_API_KEY or not artist.strip() or not track_name.strip():
            return None

        params = {
            "method": "track.getInfo",
            "user": username,
            "artist": artist,
            "track": track_name,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "autocorrect": "1",
        }
        try:
            client = self._client()
            response = await client.get(
                LASTFM_API_BASE_URL,
                params=params,
                timeout=LASTFM_TRACK_INFO_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.info("Last.fm track.getInfo failed silently | user_id=%s | artist=%s | track=%s", user_id, artist, track_name)
            return None

        if response.status_code != 200:
            logger.info("Last.fm track.getInfo returned %s | user_id=%s | artist=%s | track=%s", response.status_code, user_id, artist, track_name)
            return None

        data = response.json()
        track_data = data.get("track") if isinstance(data, dict) else None
        if not isinstance(track_data, dict):
            return None

        user_playcount = _safe_int(track_data.get("userplaycount"))
        if user_playcount is not None:
            logger.info("Last.fm userplaycount matched | user_id=%s | artist=%s | track=%s | plays=%s", user_id, artist, track_name, user_playcount)
        return user_playcount

    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        username = await self.get_username(user_id)
        if not username or not LASTFM_API_KEY:
            return None

        params = {
            "method": "user.getrecenttracks",
            "user": username,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "limit": "1",
            "extended": "1",
        }
        try:
            client = self._client()
            response = await client.get(LASTFM_API_BASE_URL, params=params)
        except Exception:
            logger.exception("Last.fm request failed | user_id=%s | username=%s", user_id, username)
            return None

        if response.status_code != 200:
            logger.error("Last.fm error %s: %s", response.status_code, response.text)
            return None

        data = response.json()
        recent = (data.get("recenttracks") or {}).get("track") or []
        if isinstance(recent, dict):
            recent = [recent]
        if not recent:
            return None

        item = recent[0]
        return await self._map_track(username, item)

    def _text(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("#text") or value.get("name") or "").strip()
        return str(value or "").strip()

    async def _find_deezer_cover(self, *, artist: str, track_name: str, album: str | None = None) -> str | None:
        queries = _unique_queries(artist, track_name, album)

        try:
            client = self._client()
            for query in queries:
                response = await client.get(
                    DEEZER_SEARCH_URL,
                    params={"q": query, "limit": "10"},
                    timeout=DEEZER_COVER_TIMEOUT_SECONDS,
                )
                if response.status_code != 200:
                    logger.info(
                        "Deezer cover lookup returned %s | artist=%s | track=%s | query=%s",
                        response.status_code,
                        artist,
                        track_name,
                        query,
                    )
                    continue

                data = response.json()
                items = data.get("data") or []
                if not isinstance(items, list):
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    found_title = str(item.get("title") or "")
                    found_artist = str((item.get("artist") or {}).get("name") or "")
                    if not _looks_like_match(track_name, found_title) or not _looks_like_match(artist, found_artist):
                        continue
                    album_data = item.get("album") or {}
                    cover = album_data.get("cover_big") or album_data.get("cover_medium")
                    if cover:
                        logger.info(
                            "Deezer cover matched | artist=%s | track=%s | query=%s | cover=%s",
                            artist,
                            track_name,
                            query,
                            cover,
                        )
                        return str(cover)
        except Exception:
            logger.info("Deezer cover lookup failed silently | artist=%s | track=%s", artist, track_name)
            return None
        return None

    async def _map_track(self, username: str, item: dict[str, Any]) -> dict[str, Any] | None:
        track_name = self._text(item.get("name"))
        artist = self._text(item.get("artist"))
        album = self._text(item.get("album"))
        if not track_name or not artist:
            return None

        attr = item.get("@attr") or {}
        nowplaying = str(attr.get("nowplaying") or "").lower() == "true"
        date_data = item.get("date") or {}
        played_at = date_data.get("uts") if isinstance(date_data, dict) else None

        images = item.get("image") or []
        cover = None
        if isinstance(images, list):
            for image in reversed(images):
                if isinstance(image, dict) and image.get("#text"):
                    cover = image.get("#text")
                    break

        # Upgrade Spotify (Client Credentials, sem precisar do usuário
        # logado): UMA chamada à Search API resolve link DA música +
        # capa 640px no mesmo payload. Ordem de preferência da capa:
        # spotify (oficial 640px) > deezer (fallback existente) > lastfm.
        spotify_track_url: str | None = None
        spotify_cover: str | None = None
        try:
            from app.services.spotify import spotify_service  # import local p/ evitar ciclos
            match = await spotify_service.search_track(artist, track_name)
            if match:
                spotify_track_url = match.get("url")
                spotify_cover = match.get("cover")
        except Exception:
            logger.exception(
                "Spotify upgrade failed | artist=%s | track=%s", artist, track_name
            )

        cover_source = "lastfm"
        if spotify_cover:
            cover = spotify_cover
            cover_source = "spotify"
        else:
            # Spotify miss → preserva o fallback Deezer atual.
            deezer_cover = await self._find_deezer_cover(
                artist=artist, track_name=track_name, album=album or None
            )
            if deezer_cover:
                cover = deezer_cover
                cover_source = "deezer"
        logger.info(
            "Last.fm track mapped | username=%s | artist=%s | track=%s | cover_source=%s | cover=%s",
            username,
            artist,
            track_name,
            cover_source,
            cover,
        )

        track_url = (
            spotify_track_url
            or item.get("url")
            or f"https://www.last.fm/user/{quote(username)}/library"
        )
        album_url = f"https://www.last.fm/music/{quote(artist)}/{quote(album)}" if album else track_url

        return {
            "source": "lastfm_current" if nowplaying else "lastfm_last",
            "played_at": played_at,
            "track_name": track_name,
            "artist": artist,
            "album": album,
            "album_name": album,
            "track_id": _stable_track_id(artist, track_name),
            "spotify_url": track_url,
            "album_url": album_url,
            "album_image_url": cover,
        }


lastfm_service = LastfmService()
