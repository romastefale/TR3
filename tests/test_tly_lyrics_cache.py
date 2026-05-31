"""Testes da camada de rede/cache do /tly (LyricsService).

Cobre a parte delicada que a task-40 não tocou: a busca no lyrics.ovh
(instável) e o cache em memória. Sem esses testes, mexer na heurística de
limpeza de artista/título, no fallback de candidatos ou no TTL do cache
poderia reintroduzir o "martelar" do serviço externo ou cachear errado sem
ninguém perceber (ver task-41).

Tudo roda com o cliente httpx mockado (FakeClient) — zero rede real — e o
relógio controlado (Clock) pra exercitar TTL sem `sleep`.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

import app.services.lyrics as lyrics_mod
from app.services.lyrics import (
    LYRICS_API_URL,
    LYRICS_CACHE_TTL_SECONDS,
    LYRICS_NEGATIVE_TTL_SECONDS,
    LyricsService,
    _clean_artist,
    _clean_title,
)


# ---------------------------------------------------------------------------
# Infra de teste: relógio controlado + cliente httpx falso
# ---------------------------------------------------------------------------


def _url(artist: str, title: str) -> str:
    """Mesma montagem de URL do _fetch — pra casar respostas por candidato."""
    return f"{LYRICS_API_URL}/{quote(artist, safe='')}/{quote(title, safe='')}"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(lyrics_mod.time, "time", c.time)
    return c


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, raise_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("json invalido")
        return self._payload


class FakeClient:
    """Casa URL -> FakeResponse (ou Exception). URL não mapeada vira 404."""

    def __init__(self, responses=None, default: FakeResponse | None = None):
        self.responses = responses or {}
        self.default = default or FakeResponse(status_code=404, payload={"error": "no"})
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        r = self.responses.get(url, self.default)
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self) -> None:  # pragma: no cover - simetria com httpx
        pass


def make_service(responses=None, default=None) -> LyricsService:
    svc = LyricsService()
    svc._http = FakeClient(responses, default)
    return svc


# ---------------------------------------------------------------------------
# Cache positivo
# ---------------------------------------------------------------------------


async def test_cache_hit_positivo_nao_refaz_fetch(clock):
    """Segunda chamada com a mesma chave serve do cache, sem novo fetch."""
    url = _url("artist", "song")
    svc = make_service({url: FakeResponse(payload={"lyrics": "linha\n"})})

    r1 = await svc.get_lyrics("artist", "song")
    assert r1 == "linha"
    assert len(svc._http.calls) == 1

    r2 = await svc.get_lyrics("artist", "song")
    assert r2 == "linha"
    assert len(svc._http.calls) == 1  # não martelou o serviço


async def test_cache_positivo_usa_ttl_longo(clock):
    """Resultado positivo expira no TTL longo (>> negativo)."""
    url = _url("artist", "song")
    svc = make_service({url: FakeResponse(payload={"lyrics": "ok"})})
    await svc.get_lyrics("artist", "song")
    _val, exp = svc._cache[("artist", "song")]
    assert exp == pytest.approx(clock.t + LYRICS_CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Cache negativo
# ---------------------------------------------------------------------------


async def test_cache_negativo_respeita_ttl_curto(clock):
    """Miss cacheia None com TTL curto; refaz só depois do TTL expirar."""
    svc = make_service({})  # tudo 404 -> None
    key = ("nobody", "nothing")

    r = await svc.get_lyrics("nobody", "nothing")
    assert r is None
    first = len(svc._http.calls)
    assert first >= 1

    _val, exp = svc._cache[key]
    assert exp == pytest.approx(clock.t + LYRICS_NEGATIVE_TTL_SECONDS)
    assert LYRICS_NEGATIVE_TTL_SECONDS < LYRICS_CACHE_TTL_SECONDS

    # Dentro do TTL: serve None do cache, sem novo fetch.
    assert await svc.get_lyrics("nobody", "nothing") is None
    assert len(svc._http.calls) == first

    # Passa o TTL: dá nova chance (refaz o fetch).
    clock.t += LYRICS_NEGATIVE_TTL_SECONDS + 1
    assert await svc.get_lyrics("nobody", "nothing") is None
    assert len(svc._http.calls) > first


# ---------------------------------------------------------------------------
# Fallback de candidatos (limpo -> original)
# ---------------------------------------------------------------------------


async def test_fallback_candidato_limpo_para_original(clock):
    """Candidato limpo falha (404) -> tenta o original e acha a letra."""
    artist = "Drake feat. Future"
    title = "Money (Remastered)"
    ca, ct = _clean_artist(artist), _clean_title(title)
    assert (ca, ct) != (artist, title)  # garante 2 candidatos distintos

    url_clean = _url(ca, ct)
    url_orig = _url(artist, title)
    svc = make_service(
        {
            url_clean: FakeResponse(status_code=404),
            url_orig: FakeResponse(payload={"lyrics": "got the money"}),
        }
    )

    r = await svc.get_lyrics(artist, title)
    assert r == "got the money"
    assert url_clean in svc._http.calls
    assert url_orig in svc._http.calls


async def test_candidato_limpo_acerta_nao_tenta_original(clock):
    """Se o candidato limpo já acha, não chega a tentar o original."""
    artist = "Drake feat. Future"
    title = "Money (Remastered)"
    ca, ct = _clean_artist(artist), _clean_title(title)
    url_clean = _url(ca, ct)
    svc = make_service({url_clean: FakeResponse(payload={"lyrics": "hit"})})

    r = await svc.get_lyrics(artist, title)
    assert r == "hit"
    assert svc._http.calls == [url_clean]  # parou no primeiro acerto


# ---------------------------------------------------------------------------
# Degradação pra None em qualquer falha
# ---------------------------------------------------------------------------


async def test_status_nao_200_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: FakeResponse(status_code=500)})
    assert await svc.get_lyrics("a", "b") is None


async def test_json_invalido_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: FakeResponse(raise_json=True)})
    assert await svc.get_lyrics("a", "b") is None


async def test_lyrics_vazio_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: FakeResponse(payload={"lyrics": "   "})})
    assert await svc.get_lyrics("a", "b") is None


async def test_lyrics_nao_string_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: FakeResponse(payload={"lyrics": None})})
    assert await svc.get_lyrics("a", "b") is None


async def test_payload_nao_dict_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: FakeResponse(payload=["nope"])})
    assert await svc.get_lyrics("a", "b") is None


async def test_erro_de_rede_degrada_none(clock):
    url = _url("a", "b")
    svc = make_service({url: httpx.ConnectError("boom")})
    assert await svc.get_lyrics("a", "b") is None


async def test_artista_ou_titulo_vazio_nao_faz_fetch(clock):
    """Sem artista ou título não dá pra buscar — None sem tocar a rede."""
    svc = make_service({})
    assert await svc.get_lyrics("", "song") is None
    assert await svc.get_lyrics("artist", "   ") is None
    assert svc._http.calls == []


# ---------------------------------------------------------------------------
# Bound do cache
# ---------------------------------------------------------------------------


async def test_bound_do_cache_limpa_ao_estourar(clock, monkeypatch):
    """Ao bater LYRICS_CACHE_BOUND, o cache é limpo antes de inserir o novo."""
    monkeypatch.setattr(lyrics_mod, "LYRICS_CACHE_BOUND", 3)
    url = _url("artist", "song")
    svc = make_service({url: FakeResponse(payload={"lyrics": "linha"})})

    # Pré-enche até o bound com entradas válidas (não expiradas).
    for i in range(3):
        svc._cache[("x", str(i))] = ("v", clock.t + 9999)
    assert len(svc._cache) == 3

    r = await svc.get_lyrics("artist", "song")
    assert r == "linha"
    # Estourou o bound -> limpou tudo e ficou só com a entrada nova.
    assert ("artist", "song") in svc._cache
    assert len(svc._cache) == 1


# ---------------------------------------------------------------------------
# get_snippet (integra get_lyrics + extract_snippet)
# ---------------------------------------------------------------------------


async def test_get_snippet_usa_lyrics_e_extrai(clock):
    """get_snippet busca a letra e devolve o trecho extraído."""
    lyrics = "Linha um\nLinha dois\nLinha tres\n"
    url = _url("artist", "song")
    svc = make_service({url: FakeResponse(payload={"lyrics": lyrics})})

    snip = await svc.get_snippet("artist", "song")
    assert snip is not None
    assert "Linha um" in snip


async def test_get_snippet_sem_letra_retorna_none(clock):
    """Sem letra (fetch falhou), get_snippet degrada pra None."""
    svc = make_service({})  # 404 -> None
    assert await svc.get_snippet("nobody", "nothing") is None


# ---------------------------------------------------------------------------
# _clean_title / _clean_artist
# ---------------------------------------------------------------------------


def test_clean_title_remove_sufixo_de_versao():
    assert _clean_title("Song - Remastered 2011") == "Song"
    assert _clean_title("Track - Live at Wembley") == "Track"
    assert _clean_title("Hit - Radio Edit") == "Hit"
    assert _clean_title("Tune - Acoustic Version") == "Tune"


def test_clean_title_remove_colchetes_e_parenteses():
    assert _clean_title("Song (Remix)") == "Song"
    assert _clean_title("Song [Bonus Track]") == "Song"


def test_clean_title_remove_feat():
    assert _clean_title("Song feat. Someone") == "Song"
    assert _clean_title("Song (ft. Someone)") == "Song"


def test_clean_artist_remove_feat():
    assert _clean_artist("Artist feat. Other") == "Artist"
    assert _clean_artist("Artist ft. Other") == "Artist"


def test_clean_artist_corta_no_separador_de_colaboracao():
    assert _clean_artist("Artist, Other") == "Artist"
    assert _clean_artist("Artist & Other") == "Artist"
    assert _clean_artist("Artist x Other") == "Artist"
    assert _clean_artist("Artist + Other") == "Artist"
