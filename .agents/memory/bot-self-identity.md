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

**Why:** cachear o fallback mínimo pelo TTL inteiro congela nome/foto errados
após uma falha de rede passageira, contrariando "identidade sempre atual".

**How to apply:** proteja o refresh com um lock async (evita chamadas
duplicadas quando vários cards expiram o TTL juntos) e só renove o cache em
sucesso de getMe.
