"""Testes do cache de Canvas por file_id.

Cobre:
- `is_cacheable_track_id`: só Spotify base62 é cacheável (lfm:/vazio fora).
- `CanvasCacheService` put/get/forget (upsert + idempotência) num sqlite real.
- `_extract_file_ids`: pega file_id de video/animation/document.
- `deliver_canvas` no CACHE HIT: reenvia por file_id (str), sem baixar do CDN.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.database import Base, engine
from app.models.canvas_file import CanvasFile  # noqa: F401  (registra a tabela)
from app.services.canvas_cache import canvas_cache_service, is_cacheable_track_id


@pytest.fixture(autouse=True)
def _ensure_canvas_table():
    """Garante a tabela canvas_files no sqlite de teste e limpa entre testes."""
    Base.metadata.create_all(bind=engine, tables=[CanvasFile.__table__])
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("DELETE FROM canvas_files"))
    yield


# ---------------------------------------------------------------------------
# is_cacheable_track_id
# ---------------------------------------------------------------------------


def test_cacheable_so_spotify_id():
    assert is_cacheable_track_id("3n3Ppam7vgaVa1iaRUc9Lp") is True


@pytest.mark.parametrize("bad", [None, "", "   ", "lfm:abcdef0123"])
def test_nao_cacheable(bad):
    assert is_cacheable_track_id(bad) is False


# ---------------------------------------------------------------------------
# put / get / forget
# ---------------------------------------------------------------------------


async def test_put_get_roundtrip():
    await canvas_cache_service.put("trackA", "FILE_ID_A", "UNIQUE_A")
    assert await canvas_cache_service.get_file_id("trackA") == "FILE_ID_A"


async def test_get_miss_retorna_none():
    assert await canvas_cache_service.get_file_id("inexistente") is None


async def test_put_upsert_atualiza_file_id():
    await canvas_cache_service.put("trackB", "OLD", "u1")
    await canvas_cache_service.put("trackB", "NEW", "u2")
    assert await canvas_cache_service.get_file_id("trackB") == "NEW"


async def test_forget_remove():
    await canvas_cache_service.put("trackC", "FID", None)
    await canvas_cache_service.forget("trackC")
    assert await canvas_cache_service.get_file_id("trackC") is None


async def test_put_ignora_track_nao_cacheable():
    await canvas_cache_service.put("lfm:hash", "FID", None)
    assert await canvas_cache_service.get_file_id("lfm:hash") is None


async def test_get_de_track_nao_cacheable_nao_consulta_db():
    # lfm:/vazio retornam None sem nem tocar no DB.
    assert await canvas_cache_service.get_file_id("lfm:x") is None
    assert await canvas_cache_service.get_file_id("") is None


# ---------------------------------------------------------------------------
# _extract_file_ids
# ---------------------------------------------------------------------------


def test_extract_file_ids_video():
    from app.bot.canvas_delivery import _extract_file_ids

    sent = SimpleNamespace(
        video=SimpleNamespace(file_id="VID", file_unique_id="VU"),
        animation=None,
        document=None,
    )
    assert _extract_file_ids(sent) == ("VID", "VU")


def test_extract_file_ids_animation_fallback():
    from app.bot.canvas_delivery import _extract_file_ids

    sent = SimpleNamespace(
        video=None,
        animation=SimpleNamespace(file_id="ANIM", file_unique_id="AU"),
        document=None,
    )
    assert _extract_file_ids(sent) == ("ANIM", "AU")


def test_extract_file_ids_nenhum():
    from app.bot.canvas_delivery import _extract_file_ids

    sent = SimpleNamespace(video=None, animation=None, document=None)
    assert _extract_file_ids(sent) == (None, None)


# ---------------------------------------------------------------------------
# deliver_canvas — CACHE HIT
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, user_id: int = 123):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=-100999, type="supergroup")
        self.bot = SimpleNamespace()
        self.answer_video_calls: list[dict] = []

    async def answer_video(self, **kwargs):
        self.answer_video_calls.append(kwargs)
        return SimpleNamespace(
            chat=self.chat,
            message_id=555,
            bot=self.bot,
            video=SimpleNamespace(file_id=kwargs.get("video"), file_unique_id="x"),
        )


async def test_deliver_canvas_cache_hit_reenvia_por_file_id(monkeypatch):
    """Com file_id em cache, deliver_canvas reenvia por file_id (str) e NÃO
    baixa do CDN nem chama get_canvas_url."""
    import app.bot.canvas_delivery as cd

    await canvas_cache_service.put("spotifyXYZ", "CACHED_FID", "u")

    # register_card + reação viram no-op (foco é o envio).
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(cd.reactions_service, "register_card", _noop)
    monkeypatch.setattr(cd, "_react_to_own_card", _noop)

    # get_canvas_url NÃO pode ser chamado no hit — se for, falha o teste.
    async def _boom(*args, **kwargs):
        raise AssertionError("get_canvas_url não deveria rodar no cache hit")

    monkeypatch.setattr(cd.spotify_canvas_service, "get_canvas_url", _boom)

    msg = _FakeMessage()
    track = {"track_id": "spotifyXYZ", "track_name": "N", "artist": "A"}
    await cd.deliver_canvas(
        msg,
        track=track,
        track_id="spotifyXYZ",
        caption="cap",
        cover=None,
        card_emoji=None,
        keyboard=None,
        log_prefix="TEST",
    )

    assert len(msg.answer_video_calls) == 1
    assert msg.answer_video_calls[0]["video"] == "CACHED_FID"


# ---------------------------------------------------------------------------
# deliver_canvas — CACHE MISS e STALE file_id
# ---------------------------------------------------------------------------


class _MissMessage:
    """Fake que diferencia envio por file_id (str) de upload de bytes."""

    def __init__(self, fail_on_str: bool = False, new_file_id: str = "NEWFID"):
        self.from_user = SimpleNamespace(id=1)
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.bot = SimpleNamespace()
        self.fail_on_str = fail_on_str
        self.new_file_id = new_file_id
        self.video_sends: list = []  # cada item: o valor de `video`

    async def answer_video(self, **kwargs):
        v = kwargs.get("video")
        self.video_sends.append(v)
        if isinstance(v, str):
            if self.fail_on_str:
                raise RuntimeError("Bad Request: wrong file_id")
            fid = v
        else:
            fid = self.new_file_id  # upload de bytes -> Telegram gera novo id
        return SimpleNamespace(
            chat=self.chat, message_id=10, bot=self.bot,
            video=SimpleNamespace(file_id=fid, file_unique_id="u"),
        )

    async def answer_photo(self, **kwargs):
        return SimpleNamespace(chat=self.chat, message_id=11, bot=self.bot,
                               video=None, animation=None, document=None)

    async def answer(self, *args, **kwargs):
        return SimpleNamespace(chat=self.chat, message_id=12, bot=self.bot,
                               video=None, animation=None, document=None)


@pytest.fixture
def _patch_canvas_io(monkeypatch):
    import app.bot.canvas_delivery as cd

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(cd.reactions_service, "register_card", _noop)
    monkeypatch.setattr(cd, "_react_to_own_card", _noop)

    async def _url(_tid):
        return "https://canvaz.scdn.co/abc"

    async def _bytes(_url):
        return b"VIDEO_BYTES"

    monkeypatch.setattr(cd.spotify_canvas_service, "get_canvas_url", _url)
    monkeypatch.setattr(cd.spotify_canvas_service, "download_canvas_bytes", _bytes)
    return cd


async def test_deliver_canvas_miss_baixa_sobe_e_cacheia(_patch_canvas_io):
    """Sem cache: baixa, sobe bytes no grupo e guarda o file_id capturado."""
    msg = _MissMessage(new_file_id="FRESH")
    track = {"track_id": "spMiss", "track_name": "N", "artist": "A"}
    await _patch_canvas_io.deliver_canvas(
        msg, track=track, track_id="spMiss", caption="c", cover=None,
        card_emoji=None, keyboard=None, log_prefix="TEST",
    )
    # Subiu bytes (não-str) e cacheou o id novo.
    assert len(msg.video_sends) == 1
    assert not isinstance(msg.video_sends[0], str)
    assert await canvas_cache_service.get_file_id("spMiss") == "FRESH"


async def test_deliver_canvas_stale_file_id_esquece_e_resobe(_patch_canvas_io):
    """file_id em cache fica inválido -> envio por str falha -> forget ->
    re-sobe os bytes -> cacheia o id novo."""
    await canvas_cache_service.put("spStale", "STALE_FID", "u")
    msg = _MissMessage(fail_on_str=True, new_file_id="REUP")
    track = {"track_id": "spStale", "track_name": "N", "artist": "A"}
    await _patch_canvas_io.deliver_canvas(
        msg, track=track, track_id="spStale", caption="c", cover=None,
        card_emoji=None, keyboard=None, log_prefix="TEST",
    )
    # 1ª tentativa por file_id (str, falhou) + 2ª upload de bytes.
    assert msg.video_sends[0] == "STALE_FID"
    assert not isinstance(msg.video_sends[-1], str)
    # Cache atualizado pro id do reupload.
    assert await canvas_cache_service.get_file_id("spStale") == "REUP"
