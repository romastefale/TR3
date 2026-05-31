"""Testes do trecho da letra do /tly e do montador da legenda.

Cobre duas peças do /tly:
- `extract_snippet` (app/services/lyrics.py): escolhe sozinho o "trecho mais
  famoso" da letra — refrão (estrofe/linha mais repetida), senão as primeiras
  linhas. Sem esses testes, mexer na heurística poderia fazer o trecho sair
  errado (linha solta, corte no meio) sem ninguém perceber (ver task-40).
- `build_tly_payload` (app/bot/telegram.py): monta a legenda. Confirma o
  escaping de HTML e a presença/ausência do `<blockquote expandable>` quando
  há/não há letra.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.bot.telegram as tg
from app.services.lyrics import (
    SNIPPET_MAX_CHARS,
    SNIPPET_MAX_LINES,
    extract_snippet,
)


# ---------------------------------------------------------------------------
# extract_snippet
# ---------------------------------------------------------------------------


def test_snippet_estrofe_repetida_pega_refrao():
    """Estrofe que se repete (>=2x) é o refrão e vira o trecho."""
    lyrics = (
        "Andando pela rua\n"
        "Sentindo a batida\n"
        "\n"
        "Esse e o refrao\n"
        "Que todo mundo canta\n"
        "\n"
        "Mais um verso aqui\n"
        "Com outra ideia\n"
        "\n"
        "Esse e o refrao\n"
        "Que todo mundo canta\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet == "Esse e o refrao\nQue todo mundo canta"


def test_snippet_so_uma_linha_repetida():
    """Sem estrofe repetida, a linha mais repetida (>=2x) ancora o trecho.

    Pega a linha repetida + as seguintes da mesma estrofe.
    """
    lyrics = (
        "Primeira linha unica\n"
        "Segunda linha unica\n"
        "\n"
        "Linha gancho repete\n"
        "Linha diferente A\n"
        "\n"
        "Outra linha qualquer\n"
        "Linha gancho repete\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet == "Linha gancho repete\nLinha diferente A"


def test_snippet_sem_repeticao_cai_na_primeira_estrofe():
    """Letra sem repetição cai na primeira estrofe inteira (até o cap)."""
    lyrics = (
        "Linha um\n"
        "Linha dois\n"
        "Linha tres\n"
        "\n"
        "Estrofe dois A\n"
        "Estrofe dois B\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    out_lines = snippet.split("\n")
    # Primeira estrofe inteira, não cortada no meio.
    assert out_lines == ["Linha um", "Linha dois", "Linha tres"]
    assert len(out_lines) <= SNIPPET_MAX_LINES


def test_snippet_cap_de_seguranca_sem_separacao_de_estrofes():
    """Sem linha em branco, a 'estrofe' é a letra toda — o cap protege."""
    lyrics = "\n".join(f"Linha unica {i}" for i in range(30))
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    out_lines = snippet.split("\n")
    assert len(out_lines) <= SNIPPET_MAX_LINES


def test_snippet_linha_unica_gigante_respeita_cap_de_chars():
    """Letra numa única linha enorme (sem `\\n`) é truncada ao cap de chars."""
    lyrics = "palavra " * 500  # ~4000 chars, uma linha só
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    assert "\n" not in snippet
    assert len(snippet) <= SNIPPET_MAX_CHARS


def test_snippet_linha_gancho_no_meio_pega_estrofe_inteira():
    """Linha-gancho repetida no meio da estrofe -> retorna a estrofe inteira."""
    lyrics = (
        "Abre a estrofe aqui\n"
        "Linha gancho repete\n"
        "Fecha a estrofe aqui\n"
        "\n"
        "Outro verso solto\n"
        "Linha gancho repete\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    # Estrofe inteira (inclui a linha ANTES do gancho), não do gancho em diante.
    assert snippet == "Abre a estrofe aqui\nLinha gancho repete\nFecha a estrofe aqui"


def test_snippet_letra_vazia_retorna_none():
    """Letra vazia / só espaços em branco degrada pra None."""
    assert extract_snippet("") is None
    assert extract_snippet("   \n  \n\t\n") is None


def test_snippet_com_marcacoes_chorus_verse():
    """Marcações tipo [Chorus]/[Verse] não quebram a detecção do refrão.

    O bloco [Chorus] repetido é escolhido como trecho (a marcação fica
    junto, é o comportamento atual — o importante é cair no refrão certo).
    """
    lyrics = (
        "[Verse 1]\n"
        "Walking down the street\n"
        "Feeling the beat\n"
        "\n"
        "[Chorus]\n"
        "This is the hook\n"
        "This is the hook\n"
        "\n"
        "[Verse 2]\n"
        "Another day goes by\n"
        "Under the sky\n"
        "\n"
        "[Chorus]\n"
        "This is the hook\n"
        "This is the hook\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    assert "This is the hook" in snippet
    assert snippet.startswith("[Chorus]")
    # É o bloco do refrão, não um verso solto.
    assert "Walking down the street" not in snippet


def test_snippet_marcacao_solta_sem_repeticao_pega_inicio():
    """Com marcações mas sem repetição, ainda cai nas primeiras linhas."""
    lyrics = (
        "[Intro]\n"
        "Comeca assim\n"
        "Segue assim\n"
        "\n"
        "[Verse]\n"
        "Vai por aqui\n"
        "Termina ali\n"
    )
    snippet = extract_snippet(lyrics)
    assert snippet is not None
    assert snippet.split("\n")[0] == "[Intro]"


# ---------------------------------------------------------------------------
# build_tly_payload (montador da legenda)
# ---------------------------------------------------------------------------


def _fake_message(user_id: int = 123, full_name: str = "PI"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name=full_name),
        chat=SimpleNamespace(type="private"),
    )


@pytest.fixture
def _patch_tly_side_effects(monkeypatch):
    """Neutraliza os side effects (register_play + contador ♫) do payload.

    O foco do teste é a montagem da legenda, não a rede/DB.
    """

    async def _fake_register_play(*args, **kwargs):
        return None

    async def _fake_count(*args, **kwargs):
        return (7, "local")

    monkeypatch.setattr(tg.likes_service, "register_play", _fake_register_play)
    monkeypatch.setattr(tg, "_resolve_play_button_count", _fake_count)


_TRACK = {
    "track_id": "tid-1",
    "track_name": "Some Song",
    "artist": "Some Artist",
    "spotify_url": "https://open.spotify.com/track/abc",
    "album_image_url": "https://img/cover.jpg",
}


async def test_tly_caption_com_letra_tem_quote_expansivel(_patch_tly_side_effects):
    """Com letra, a legenda inclui o <blockquote expandable> com o trecho."""
    result = await tg.build_tly_payload(_fake_message(), dict(_TRACK), "Linha um\nLinha dois")
    assert result is not None
    _track_id, caption, _cover, _emoji = result
    assert "<blockquote expandable>" in caption
    assert "</blockquote>" in caption
    assert "Linha um\nLinha dois" in caption


async def test_tly_caption_sem_letra_nao_tem_quote(_patch_tly_side_effects):
    """Sem letra (None ou vazio), sai só o cabeçalho — sem blockquote."""
    for snippet in (None, ""):
        result = await tg.build_tly_payload(_fake_message(), dict(_TRACK), snippet)
        assert result is not None
        _track_id, caption, _cover, _emoji = result
        assert "<blockquote" not in caption


async def test_tly_caption_escapa_html_da_letra(_patch_tly_side_effects):
    """O trecho da letra é HTML-escapado dentro do quote."""
    result = await tg.build_tly_payload(
        _fake_message(), dict(_TRACK), "rock & roll <yeah>"
    )
    assert result is not None
    _track_id, caption, _cover, _emoji = result
    assert "rock &amp; roll &lt;yeah&gt;" in caption
    # O texto cru (não escapado) não pode aparecer.
    assert "rock & roll <yeah>" not in caption


async def test_tly_caption_escapa_html_do_nome(_patch_tly_side_effects):
    """O nome de exibição também é escapado (depois do negrito unicode)."""
    result = await tg.build_tly_payload(
        _fake_message(full_name="A&B"), dict(_TRACK), None
    )
    assert result is not None
    _track_id, caption, _cover, _emoji = result
    assert "&amp;" in caption
    assert "A&B" not in caption


async def test_tly_payload_sem_from_user_retorna_none(_patch_tly_side_effects):
    """Sem from_user não dá pra montar o payload."""
    msg = SimpleNamespace(from_user=None, chat=SimpleNamespace(type="private"))
    assert await tg.build_tly_payload(msg, dict(_TRACK), "x") is None


async def test_tly_payload_sem_track_id_retorna_none(_patch_tly_side_effects):
    """Sem track_id não dá pra registrar/montar — retorna None."""
    track = dict(_TRACK)
    track["track_id"] = ""
    assert await tg.build_tly_payload(_fake_message(), track, "x") is None
