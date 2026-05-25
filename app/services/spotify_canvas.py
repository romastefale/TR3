from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
import pyotp

from app.config.settings import (
    SPOTIFY_CANVAS_ENABLED,
    SPOTIFY_CANVAS_SP_DC,
    SPOTIFY_CANVAS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# Token anônimo do web player dura ~1h; usamos 50min p/ margem de segurança.
CANVAS_TOKEN_TTL_SECONDS = 50 * 60
# Canvas URL pra um track muda raramente; 24h de cache reduz drasticamente
# o tráfego pro canvaz-cache mas ainda permite refresh diário.
CANVAS_URL_CACHE_TTL_SECONDS = 24 * 3600
# Cache curto pra "miss confiável" (canvasdownloader retornou página
# "Canvas not found"). Como o proxy dá MUITO false negative, não pode
# cachear 24h — em 1h tentamos de novo (incluindo via TOTP/sp_dc).
CANVAS_URL_NEGATIVE_TTL_SECONDS = 1 * 3600
# Quando o endpoint de token do Spotify devolve 403 (IP bloqueado), não
# vale a pena retentar a cada request — ficamos em backoff por 10min.
CANVAS_TOKEN_BACKOFF_SECONDS = 10 * 60
# Canvas é vertical 720x1280 H.264, raramente passa de 2MB. 8MB já é teto
# bem folgado — qualquer coisa maior é provavelmente bug e a gente aborta.
CANVAS_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
CANVAS_DOWNLOAD_TIMEOUT_SECONDS = 10.0

# Endpoint anônimo do web player. Funciona de IPs residenciais; IPs de
# datacenter (Railway, AWS etc) tipicamente recebem `403 URL Blocked` da
# camada upstream (Cloudflare/Akamai) — daí a necessidade do sp_dc cookie.
CANVAS_TOKEN_URL_ANON = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
# Endpoint autenticado por cookie de sessão (sp_dc) — trata como browser
# logado, atravessa o bloqueio de datacenter. Cookie dura ~1 ano; o token
# derivado dura ~1h (já cacheado em memória).
CANVAS_TOKEN_URL_COOKIE = "https://open.spotify.com/api/token?reason=transport&productType=web_player"
CANVAS_API_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
CANVAS_URL_RE = re.compile(rb"https://canvaz\.scdn\.co/[^\x00\s\"'<>]+")

# Método TOTP (introduzido pelo Spotify em 2024 pro endpoint anônimo).
# Secret + clientId rotacionam ~6 meses. Fonte: glomatico/votify, KraXen72,
# spotify-aac-downloader. Se quebrar (token 400 ou 401 ao invés de 403),
# checar repos pra valor atualizado. 403 = bloqueio de IP (não secret errado).
SPOTIFY_TOTP_CANDIDATES: list[tuple[str, str]] = [
    # (secret_base32, clientId) — tentamos em ordem; primeiro que der 200 vence.
    ("S7YF4O6G6SJS6Y2I", "764836690740445fb56501729424e8c1"),
    ("GU2TANZRGQ2TQNJTGQ4DONBZGM2DMNTYME3DKMTYGY2DOMTSGE", "650271733a5c4c578ed18d0981977df2"),
]
CANVAS_TOKEN_URL_TOTP_TEMPLATE = (
    "https://open.spotify.com/get_access_token?"
    "reason=transport&productType=web_player&"
    "totp={totp}&totpVer=2&ts={ts}&clientId={client_id}"
)

# Proxy terceirizado: canvasdownloader.com já tem IP residencial / parceria
# com o Spotify pra atravessar o bloqueio de datacenter. Devolve HTML com
# <video src="https://canvaz.scdn.co/...">. A gente regexa o src e baixa
# direto do CDN oficial (mesmo SSRF guard de antes).
#
# Trade-offs aceitos (estudados):
# - É um terceiro: se ele cair, /tcanvas cai pra /playing (fallback igual hoje)
# - Cloudflare na frente: User-Agent realista + ~50 req/dia esperado = bem abaixo
#   de qualquer limite razoável. Sem captcha visível em 2026
# - Privacidade: a gente expõe pra ele só o track_id público do Spotify
#   (mesmo dado que o user vê na URL). Sem user_id, sem token nosso
# - Cache de 24h por track_id (já existente) reduz drasticamente a frequência
CANVASDOWNLOADER_URL = "https://www.canvasdownloader.com/canvas"
CANVASDOWNLOADER_TIMEOUT_SECONDS = 8.0
CANVASDOWNLOADER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
    "Referer": "https://www.canvasdownloader.com/",
}
# Marker que o canvasdownloader.com cospe quando ele NÃO acha o Canvas.
# Validado ao vivo: várias músicas populares (Blinding Lights etc) caem nessa
# página mesmo tendo Canvas no app — false negative. Por isso o cache pra
# "miss" do proxy é curto (1h) e a gente tenta camadas seguintes.
CANVASDOWNLOADER_NOT_FOUND_MARKER = "Canvas not found"
TOKEN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
CANVAS_HEADERS = {
    "Accept": "application/protobuf",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Language": "en",
    "User-Agent": "Spotify/8.5.49 iOS/Version 13.3.1 (Build 17D50)",
    "Accept-Encoding": "gzip, deflate, br",
}


def _encode_varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _encode_length_delimited(field_number: int, payload: bytes) -> bytes:
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_canvas_request(track_id: str) -> bytes:
    track_uri = f"spotify:track:{track_id}".encode()
    inner = _encode_length_delimited(1, track_uri)
    outer = _encode_length_delimited(1, inner)
    return outer


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("Incomplete varint")


def _iter_length_delimited_fields(data: bytes):
    offset = 0
    while offset < len(data):
        try:
            tag, offset = _decode_varint(data, offset)
        except ValueError:
            return
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 2:
            try:
                length, offset = _decode_varint(data, offset)
            except ValueError:
                return
            payload = data[offset : offset + length]
            offset += length
            yield field_number, payload
        elif wire_type == 0:
            try:
                _, offset = _decode_varint(data, offset)
            except ValueError:
                return
        elif wire_type == 1:
            offset += 8
        elif wire_type == 5:
            offset += 4
        else:
            return


def _find_canvas_url_from_protobuf(data: bytes) -> str | None:
    for _, payload in _iter_length_delimited_fields(data):
        if payload.startswith(b"http") and b"canvaz.scdn.co" in payload:
            try:
                return payload.decode()
            except UnicodeDecodeError:
                continue
        nested = _find_canvas_url_from_protobuf(payload)
        if nested:
            return nested
    return None


def _find_canvas_url(data: bytes) -> str | None:
    protobuf_url = _find_canvas_url_from_protobuf(data)
    if protobuf_url:
        return protobuf_url
    match = CANVAS_URL_RE.search(data)
    if not match:
        return None
    try:
        return match.group(0).decode()
    except UnicodeDecodeError:
        return None


class SpotifyCanvasService:
    def __init__(self) -> None:
        # Cache do token anônimo (compartilhado entre requests).
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # Backoff: se o endpoint de token retorna 403 (IP bloqueado), evita
        # martelar — ficamos em "modo desistir" por CANVAS_TOKEN_BACKOFF_SECONDS.
        self._token_blocked_until: float = 0.0
        # Cache de URL por track_id; armazena `None` como cache negativo pra
        # não martelar o canvaz-cache em músicas que sabidamente não têm Canvas.
        self._url_cache: dict[str, tuple[str | None, float]] = {}
        self._url_lock = asyncio.Lock()

    async def get_canvas_url(self, track_id: str) -> str | None:
        clean_track_id = (track_id or "").strip()
        if not SPOTIFY_CANVAS_ENABLED:
            logger.info("Spotify Canvas skipped: disabled")
            return None
        if not clean_track_id:
            logger.info("Spotify Canvas skipped: empty track_id")
            return None

        # Fast path: cache hit sem lock.
        now = time.time()
        cached = self._url_cache.get(clean_track_id)
        if cached is not None and now < cached[1]:
            return cached[0]

        # Slow path: re-check sob lock pra não duplicar fetch.
        async with self._url_lock:
            now = time.time()
            cached = self._url_cache.get(clean_track_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            try:
                canvas_url: str | None = None
                # Marca se a resposta negativa veio de uma fonte confiável
                # (Spotify oficial via token válido) — aí cacheia por 24h.
                # Senão, cacheia só 1h pra dar chance de tentar de novo logo.
                negative_is_authoritative = False

                # CAMADA 1: Spotify direto via TOTP (sem cookie).
                # Em IPs que o Cloudflare libera, funciona out-of-the-box e
                # tem ~99% de hit rate (API oficial). Em IPs bloqueados, dá
                # 403 e entra em backoff — não tenta de novo por 10min.
                if time.time() >= self._token_blocked_until:
                    token = await self._get_access_token()
                    if token:
                        canvas_url = await self._fetch_canvas_url(clean_track_id, token)
                        if canvas_url:
                            logger.info(
                                "Spotify Canvas via TOKEN_DIRECT: track_id=%s", clean_track_id
                            )
                        else:
                            # Spotify oficial disse "sem canvas" → confiável.
                            negative_is_authoritative = True

                # CAMADA 2: canvasdownloader.com (proxy terceirizado).
                # Pega tracks que o caminho direto não conseguiu (IP bloqueado
                # ou Spotify devolveu vazio por motivo desconhecido). Tem
                # MUITO false negative — por isso "miss" dele não é confiável.
                if canvas_url is None:
                    canvas_url, proxy_definitive = await self._fetch_via_canvasdownloader(
                        clean_track_id
                    )
                    if canvas_url:
                        logger.info(
                            "Spotify Canvas via PROXY: track_id=%s", clean_track_id
                        )
                    elif proxy_definitive:
                        # Proxy confirmou "Canvas not found" — mas como ele
                        # mente bastante, ainda não consideramos autoritativo.
                        pass

                # Decide TTL do cache:
                # - Positivo: 24h (Canvas URLs são estáveis)
                # - Negativo confiável (Spotify direto disse não): 24h
                # - Negativo não-confiável (só o proxy/falha): 1h pra retry
                ttl = (
                    CANVAS_URL_CACHE_TTL_SECONDS
                    if canvas_url or negative_is_authoritative
                    else CANVAS_URL_NEGATIVE_TTL_SECONDS
                )
                if not canvas_url:
                    logger.info(
                        "Spotify Canvas NOT FOUND: track_id=%s (cache_ttl=%ss authoritative=%s)",
                        clean_track_id,
                        ttl,
                        negative_is_authoritative,
                    )
                self._url_cache[clean_track_id] = (canvas_url, time.time() + ttl)
                return canvas_url
            except Exception:
                logger.exception("Spotify Canvas lookup failed: track_id=%s", clean_track_id)
                return None

    async def _fetch_via_canvasdownloader(
        self, track_id: str
    ) -> tuple[str | None, bool]:
        """Resolve Canvas URL via canvasdownloader.com (proxy terceirizado).

        Retorna (url, definitivo_negativo). O segundo bool indica se o proxy
        explicitamente disse "Canvas not found" (página com marker). Mesmo
        assim NÃO é autoritativo — o proxy dá MUITO false negative em
        músicas populares (Blinding Lights, Call Me Maybe testadas). Só
        ajuda a distinguir "erro de rede" de "proxy respondeu não".

        Segurança:
        - Só passa o track_id público (mesmo dado da URL do Spotify)
        - URL extraída é revalidada no download_canvas_bytes (SSRF guard)
        - Timeout fail-fast 8s
        """
        track_url = f"https://open.spotify.com/track/{track_id}"
        try:
            async with httpx.AsyncClient(
                timeout=CANVASDOWNLOADER_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers=CANVASDOWNLOADER_HEADERS,
            ) as client:
                response = await client.get(
                    CANVASDOWNLOADER_URL, params={"link": track_url}
                )
        except Exception:
            logger.warning(
                "Canvas proxy request error: track_id=%s", track_id, exc_info=True
            )
            return None, False
        if response.status_code != 200:
            logger.warning(
                "Canvas proxy non-200: track_id=%s status=%s",
                track_id,
                response.status_code,
            )
            return None, False
        match = CANVAS_URL_RE.search(response.content)
        if match:
            try:
                return match.group(0).decode(), False
            except UnicodeDecodeError:
                logger.warning("Canvas proxy: URL não-utf8 track_id=%s", track_id)
                return None, False
        # Detecta a página "Canvas not found" do proxy.
        is_not_found = CANVASDOWNLOADER_NOT_FOUND_MARKER in response.text
        logger.info(
            "Canvas proxy MISS: track_id=%s proxy_says_not_found=%s",
            track_id,
            is_not_found,
        )
        return None, is_not_found

    async def download_canvas_bytes(self, url: str) -> bytes | None:
        """Baixa o vídeo Canvas pra memória, com teto de tamanho.

        Só aceita URLs do domínio oficial `canvaz.scdn.co` (SSRF guard).
        Retorna `None` em qualquer falha — chamador deve cair pro fallback.
        """
        if not url or not url.startswith("https://canvaz.scdn.co/"):
            logger.warning("Canvas download rejected: bad url=%s", url[:120] if url else None)
            return None
        try:
            async with httpx.AsyncClient(
                timeout=CANVAS_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Canvas download failed: status=%s url=%s", response.status_code, url
                        )
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > CANVAS_DOWNLOAD_MAX_BYTES:
                            logger.warning(
                                "Canvas download aborted: oversize (>%s bytes) url=%s",
                                CANVAS_DOWNLOAD_MAX_BYTES,
                                url,
                            )
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception:
            logger.exception("Canvas download error url=%s", url)
            return None

    async def _get_access_token(self) -> str | None:
        # Fast path: token em cache e ainda válido.
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            # Re-check sob lock.
            now = time.time()
            if self._token and now < self._token_expires_at:
                return self._token
            token = await self._fetch_access_token()
            if token:
                self._token = token
                self._token_expires_at = now + CANVAS_TOKEN_TTL_SECONDS
            return token

    async def _fetch_access_token(self) -> str | None:
        """Cascata de aquisição de token (do mais robusto pro mais frágil):

        1. sp_dc cookie (se configurado) — atravessa bloqueio de datacenter,
           ~99% de sucesso. Cookie dura ~1 ano.
        2. TOTP (novo método 2024+) — funciona em janelas que o Cloudflare
           libera; sem cookie, sem credencial. Em IP bloqueado dá 403 e
           a gente ativa backoff de 10min.
        3. Anônimo legacy — quase sempre 403 desde 2024, mantido por
           compat caso o Spotify reverta.

        Se TODAS as tentativas resultarem em 403, ativa backoff pra não
        martelar o endpoint até a próxima janela.
        """
        if SPOTIFY_CANVAS_SP_DC:
            token = await self._fetch_token_with_cookie()
            if token:
                return token
            logger.warning(
                "Spotify Canvas: sp_dc não devolveu token (cookie expirado/inválido)"
            )

        # TOTP: tenta cada candidato (secret, clientId).
        for secret, client_id in SPOTIFY_TOTP_CANDIDATES:
            token, was_blocked = await self._fetch_token_totp(secret, client_id)
            if token:
                return token
            if was_blocked:
                # 403 do upstream: outros candidatos vão dar igual, e o
                # legacy também. Ativa backoff e desiste por enquanto.
                self._token_blocked_until = time.time() + CANVAS_TOKEN_BACKOFF_SECONDS
                logger.warning(
                    "Spotify Canvas token endpoint BLOCKED (datacenter IP). "
                    "Backoff %ss. Configure SPOTIFY_CANVAS_SP_DC pra 99%% hit rate.",
                    CANVAS_TOKEN_BACKOFF_SECONDS,
                )
                return None

        # Último recurso: anônimo legacy.
        return await self._fetch_token_anonymous()

    async def _fetch_token_totp(
        self, secret: str, client_id: str
    ) -> tuple[str | None, bool]:
        """Gera TOTP HMAC-SHA1 e tenta o endpoint anônimo assinado.

        Retorna (token, blocked_by_upstream). `blocked_by_upstream=True`
        sinaliza pro chamador ativar backoff (não adianta tentar outros).
        """
        try:
            totp_code = pyotp.TOTP(secret).now()
            ts = int(time.time() * 1000)
            url = CANVAS_TOKEN_URL_TOTP_TEMPLATE.format(
                totp=totp_code, ts=ts, client_id=client_id
            )
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(url, headers=TOKEN_HEADERS)
        except Exception:
            logger.exception("Spotify Canvas TOTP token request error")
            return None, False
        # 403 com "URL Blocked" = upstream IP block, não vale insistir.
        if response.status_code == 403 and "URL Blocked" in response.text:
            return None, True
        token = self._extract_token(response, source=f"totp[{client_id[:6]}]")
        return token, False

    async def _fetch_token_with_cookie(self) -> str | None:
        headers = dict(TOKEN_HEADERS)
        headers["Cookie"] = f"sp_dc={SPOTIFY_CANVAS_SP_DC}"
        try:
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(CANVAS_TOKEN_URL_COOKIE, headers=headers)
        except Exception:
            logger.exception("Spotify Canvas cookie token request error")
            return None
        return self._extract_token(response, source="cookie")

    async def _fetch_token_anonymous(self) -> str | None:
        try:
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(CANVAS_TOKEN_URL_ANON, headers=TOKEN_HEADERS)
        except Exception:
            logger.exception("Spotify Canvas anonymous token request error")
            return None
        return self._extract_token(response, source="anon")

    def _extract_token(self, response: httpx.Response, source: str) -> str | None:
        if response.status_code == 403 and "URL Blocked" in response.text:
            # Caso clássico de IP de datacenter bloqueado pelo upstream do Spotify.
            logger.warning(
                "Spotify Canvas token BLOCKED_BY_UPSTREAM (datacenter IP block) source=%s — "
                "configure SPOTIFY_CANVAS_SP_DC com o cookie sp_dc de uma conta logada",
                source,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "Spotify Canvas token failed: source=%s status=%s body=%s",
                source,
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning(
                "Spotify Canvas token failed: non-json response source=%s body=%s",
                source,
                response.text[:200],
            )
            return None
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            logger.warning(
                "Spotify Canvas token failed: token key missing source=%s keys=%s",
                source,
                sorted(data.keys()),
            )
            return None
        is_anonymous = bool(data.get("isAnonymous"))
        logger.info("Spotify Canvas token OK source=%s isAnonymous=%s", source, is_anonymous)
        return str(token)

    async def _fetch_canvas_url(self, track_id: str, access_token: str) -> str | None:
        payload = _encode_canvas_request(track_id)
        headers = dict(CANVAS_HEADERS)
        headers["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.post(CANVAS_API_URL, content=payload, headers=headers)
        if response.status_code != 200:
            logger.warning("Spotify Canvas API failed: track_id=%s status=%s body=%s", track_id, response.status_code, response.text[:200])
            return None
        canvas_url = _find_canvas_url(response.content)
        if not canvas_url:
            logger.info("Spotify Canvas API response parsed without URL: track_id=%s bytes=%s", track_id, len(response.content))
        return canvas_url


spotify_canvas_service = SpotifyCanvasService()
