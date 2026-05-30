---
name: Bot self identity (nome + foto)
description: Como obter nome e foto de perfil atuais do próprio bot Telegram, com cache TTL.
---

Um bot consegue ler a própria foto de perfil chamando `getUserProfilePhotos`
com o **próprio** `id` (vindo de `getMe`), depois `getFile` + `download_file`.
A maior resolução é `photos.photos[0][-1].file_id`.

**Regra de cache (durável):** só cacheie a identidade quando `getMe` teve
sucesso. Em falha transitória de `getMe` sem cache prévio, devolva um fallback
efêmero (não cacheado) para não congelar nome/foto errados por todo o TTL.

**Why:** revisão apontou que cachear o fallback mínimo por 1h contraria o
requisito de "nome/foto sempre atuais" após uma falha de rede passageira.

**How to apply:** ver `app/services/bot_identity.py` — TTL 1h, `asyncio.Lock`
no refresh pra evitar chamadas duplicadas quando vários cards expiram juntos.
