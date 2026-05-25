"""Helpers para detectar se um usuário já conectou Spotify ou Last.fm,
e textos padronizados para orientar quem ainda não conectou.

Usado por comandos de música (/playing, /albnow, /tnow, ...) para
mostrar a mesma orientação clara em qualquer chat, antes de tentar bater
nas APIs externas.
"""
from __future__ import annotations

from app.db.database import SessionLocal
from app.models.lastfm_profile import LastfmProfile
from app.models.spotify_token import SpotifyToken


def is_user_connected(user_id: int) -> bool:
    """True se o usuário tem Spotify OU Last.fm vinculado ao bot."""
    with SessionLocal() as db:
        has_spotify = (
            db.query(SpotifyToken.id).filter_by(user_id=user_id).first() is not None
        )
        if has_spotify:
            return True
        has_lastfm = (
            db.query(LastfmProfile.user_id).filter_by(user_id=user_id).first()
            is not None
        )
        return has_lastfm


# Mensagem curta para uso em grupo: evita poluir o chat.
CONNECT_HINT_GROUP = (
    "👋 Você ainda não conectou o Last.fm — sem isso eu não consigo "
    "ler o que você está ouvindo.\n\n"
    "Manda aqui mesmo (ou no meu privado): "
    "<code>/lastfm seu_username</code> (sem o @)."
)

# Mensagem completa para uso no privado: já entrega o passo-a-passo.
CONNECT_HINT_PRIVATE = (
    "👋 Você ainda não conectou o Last.fm — sem isso eu não consigo "
    "ler o que você está ouvindo.\n\n"
    "🎧 <b>Como conectar:</b>\n"
    "<code>/lastfm seu_username</code> (sem o @)\n\n"
    "Depois é só rodar o comando de novo."
)


def connect_hint_for(chat_type: str | None) -> str:
    """Devolve o texto de orientação apropriado para o tipo de chat."""
    return CONNECT_HINT_PRIVATE if chat_type == "private" else CONNECT_HINT_GROUP
