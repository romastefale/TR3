"""Serviço de letra pro /tly.

Busca a letra completa no lyrics.ovh (grátis, sem chave/auth) e extrai uma
estrofe pro quote do Telegram. Estratégia:
- Refrão = a estrofe (ou linha) que mais se repete na letra.
- Sem repetição detectável, cai na primeira estrofe.

Sai uma estrofe inteira (refrão de preferência) — não a letra completa. O
quote expansível do Telegram colapsa em ~3 linhas e abre no toque, então cabe a
estrofe toda. Toda falha de rede/parse degrada pra None (o caller manda só o
cabeçalho, sem quote). lyrics.ovh é instável; cache em memória com TTL evita
martelar e o negativo tem TTL curto pra dar nova chance.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

LYRICS_API_URL = "https://api.lyrics.ovh/v1"
LYRICS_TIMEOUT_SECONDS = 8.0
LYRICS_CACHE_TTL_SECONDS = 24 * 3600
LYRICS_NEGATIVE_TTL_SECONDS = 6 * 3600
LYRICS_CACHE_BOUND = 2000
# Guard contra resposta absurdamente grande (letra normal < ~6k chars).
LYRICS_MAX_CHARS = 8000
# Estrofe inteira (refrão de preferência). O quote colapsa em ~3 linhas e abre
# no toque, então cabe a estrofe completa. Os caps abaixo são só guarda de
# segurança: quando a letra vem sem separação de estrofes, a "estrofe" vira a
# letra toda — aí cortamos pra não despejar a música inteira no quote.
SNIPPET_MAX_LINES = 12
SNIPPET_MAX_CHARS = 700

# Limpeza de artista/título pra melhorar o acerto no lyrics.ovh (match meio
# exato). Tira sufixos de versão, parênteses/colchetes e participações.
_DASH_SUFFIX_RE = re.compile(
    r"\s*-\s*(remaster(ed)?|live|radio edit|single version|mono|stereo|"
    r"deluxe|bonus track|remix|acoustic|version).*$",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\b.*$", re.IGNORECASE
)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _clean_title(value: str) -> str:
    v = (value or "").strip()
    v = _DASH_SUFFIX_RE.sub("", v)
    v = _BRACKET_RE.sub("", v)
    v = _FEAT_RE.sub("", v)
    return v.strip()


def _clean_artist(value: str) -> str:
    v = (value or "").strip()
    v = _FEAT_RE.sub("", v)
    # Artista principal: corta no primeiro separador de colaboração.
    for sep in (",", "&", " feat", " ft", " x ", " + "):
        idx = v.lower().find(sep.lower())
        if idx > 0:
            v = v[:idx]
            break
    return v.strip()


def _norm(line: str) -> str:
    return _PUNCT_RE.sub("", line.lower()).strip()


def _trim_lines(lines: list[str]) -> str | None:
    out: list[str] = []
    total = 0
    for ln in lines:
        if not ln:
            continue
        if len(out) >= SNIPPET_MAX_LINES:
            break
        if out and total + len(ln) > SNIPPET_MAX_CHARS:
            break
        # Guarda contra linha única gigante (letra sem `\n`): trunca a 1ª linha
        # ao cap de chars pra nunca despejar a letra inteira no quote.
        if not out and len(ln) > SNIPPET_MAX_CHARS:
            ln = ln[:SNIPPET_MAX_CHARS].rstrip()
        out.append(ln)
        total += len(ln)
    text = "\n".join(out).strip()
    return text or None


def extract_snippet(lyrics: str) -> str | None:
    """Estrofe do refrão (estrofe/linha mais repetida); senão, 1ª estrofe."""
    if not lyrics:
        return None
    text = lyrics.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = [ln.strip() for ln in text.split("\n")]

    # Agrupa em estrofes (blocos separados por linha em branco).
    stanzas: list[list[str]] = []
    current: list[str] = []
    for ln in raw_lines:
        if ln:
            current.append(ln)
        elif current:
            stanzas.append(current)
            current = []
    if current:
        stanzas.append(current)
    if not stanzas:
        return None

    # 1) Refrão = estrofe que mais se repete (>= 2 ocorrências).
    counts: dict[str, int] = {}
    first_idx: dict[str, int] = {}
    for i, st in enumerate(stanzas):
        key = "\n".join(_norm(l) for l in st if _norm(l))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        first_idx.setdefault(key, i)
    best_key: str | None = None
    best = 1
    for key, c in counts.items():
        if c > best:
            best = c
            best_key = key
    if best_key is not None and best >= 2:
        snippet = _trim_lines(stanzas[first_idx[best_key]])
        if snippet:
            return snippet

    # 2) Linha que mais se repete (>= 2); pega ela + as seguintes.
    line_counts: dict[str, int] = {}
    line_first: dict[str, tuple[int, int]] = {}
    for i, st in enumerate(stanzas):
        for j, l in enumerate(st):
            k = _norm(l)
            if not k:
                continue
            line_counts[k] = line_counts.get(k, 0) + 1
            line_first.setdefault(k, (i, j))
    rep_key: str | None = None
    rep = 1
    for k, c in line_counts.items():
        if c > rep:
            rep = c
            rep_key = k
    if rep_key is not None and rep >= 2:
        si, _sj = line_first[rep_key]
        # Estrofe inteira que contém a linha-gancho (não corta a partir dela).
        snippet = _trim_lines(stanzas[si])
        if snippet:
            return snippet

    # 3) Fallback: a primeira estrofe inteira.
    return _trim_lines(stanzas[0])


class LyricsService:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._cache: dict[tuple[str, str], tuple[str | None, float]] = {}
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=LYRICS_TIMEOUT_SECONDS, follow_redirects=True
            )
        return self._http

    async def get_snippet(self, artist: str, title: str) -> str | None:
        """Trecho pronto pro quote (já extraído). None em qualquer falha."""
        lyrics = await self.get_lyrics(artist, title)
        if not lyrics:
            return None
        try:
            return extract_snippet(lyrics)
        except Exception:
            logger.exception("LYRICS_SNIPPET_FAILED artist=%s title=%s", artist, title)
            return None

    async def get_lyrics(self, artist: str, title: str) -> str | None:
        artist = (artist or "").strip()
        title = (title or "").strip()
        if not artist or not title:
            return None
        key = (artist.lower(), title.lower())
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]

        async with self._lock:
            now = time.time()
            cached = self._cache.get(key)
            if cached is not None and now < cached[1]:
                return cached[0]

            candidates: list[tuple[str, str]] = []
            ca, ct = _clean_artist(artist), _clean_title(title)
            if ca and ct:
                candidates.append((ca, ct))
            if (artist, title) not in candidates:
                candidates.append((artist, title))

            result: str | None = None
            for a, t in candidates:
                result = await self._fetch(a, t)
                if result:
                    break

            ttl = LYRICS_CACHE_TTL_SECONDS if result else LYRICS_NEGATIVE_TTL_SECONDS
            if len(self._cache) >= LYRICS_CACHE_BOUND:
                self._cache.clear()
            self._cache[key] = (result, time.time() + ttl)
            return result

    async def _fetch(self, artist: str, title: str) -> str | None:
        url = f"{LYRICS_API_URL}/{quote(artist, safe='')}/{quote(title, safe='')}"
        try:
            resp = await self._client().get(url)
        except Exception:
            logger.warning(
                "LYRICS_FETCH_ERROR artist=%s title=%s", artist, title, exc_info=True
            )
            return None
        if resp.status_code != 200:
            logger.info(
                "LYRICS_MISS artist=%s title=%s status=%s", artist, title, resp.status_code
            )
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        lyrics = data.get("lyrics") if isinstance(data, dict) else None
        if not isinstance(lyrics, str):
            return None
        lyrics = lyrics.strip()
        if not lyrics:
            return None
        return lyrics[:LYRICS_MAX_CHARS]

    async def shutdown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


lyrics_service = LyricsService()
