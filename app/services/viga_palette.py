"""viga_palette — extração de cor de destaque a partir de bytes de imagem.

Usa Pillow puro (sem deps externas). Estratégia: reduz a capa a uma
miniatura, quantiza a 16 cores, filtra cores pouco saturadas ou muito
escuras/claras e devolve a mais frequente. Pensado para alimentar a
variável CSS --accent do card mensal/semanal.
"""
from __future__ import annotations

import colorsys
import io
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

# Cor de fallback (verde mint do tema), usada quando a capa é inválida
# ou não há pixels com saturação suficiente.
DEFAULT_ACCENT_HEX = "#3FE0A6"


def _is_vibrant(r: int, g: int, b: int) -> bool:
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    return s >= 0.35 and 0.22 <= l <= 0.78


def _saturate_for_dark_ui(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Garante luminosidade suficiente para legibilidade sobre fundo escuro."""
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    l = max(l, 0.58)
    s = max(s, 0.55)
    nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
    return int(nr * 255), int(ng * 255), int(nb * 255)


def extract_accent_hex(image_bytes: Optional[bytes]) -> str:
    """Devolve cor dominante vibrante da imagem em formato `#RRGGBB`.

    Se `image_bytes` for vazio/inválido ou nenhuma cor passar nos filtros
    de saturação, devolve `DEFAULT_ACCENT_HEX`.
    """
    if not image_bytes:
        return DEFAULT_ACCENT_HEX
    try:
        with Image.open(io.BytesIO(image_bytes)) as raw:
            img = raw.convert("RGB")
            img.thumbnail((160, 160))
            quantized = img.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette() or []
            counts = sorted(quantized.getcolors() or [], key=lambda c: c[0], reverse=True)
        candidates: list[tuple[int, tuple[int, int, int]]] = []
        for count, idx in counts:
            r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            if _is_vibrant(r, g, b):
                candidates.append((count, (r, g, b)))
        if not candidates:
            return DEFAULT_ACCENT_HEX
        _, (r, g, b) = candidates[0]
        r, g, b = _saturate_for_dark_ui(r, g, b)
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        logger.debug("viga_palette extraction failed", exc_info=True)
        return DEFAULT_ACCENT_HEX


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Converte `#RRGGBB` em string CSS rgba(r,g,b,alpha)."""
    value = hex_color.lstrip("#")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.3f})"
