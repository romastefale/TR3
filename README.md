# tigraoRADIO TR3

Bot de Telegram integrado ao Spotify e ao Last.fm para mostrar a música atual ou a última música ouvida, registrar reproduções, curtidas e rankings.

A UX principal foi mantida no mesmo padrão do TR2: `/playing`, gatilhos textuais, caption, botões de plays/likes, `/mood`, `/myself` e `/songcharts`.

## Fontes de música

O bot usa uma camada unificada de música:

1. Se o usuário vinculou Last.fm com `/lastfm <username>`, o bot tenta ler o Last.fm primeiro.
2. Se não houver Last.fm válido, ou se a consulta falhar, o bot usa o Spotify conectado por `/login`.

## Comandos públicos

```text
/start
/help
/login
/logout
/lastfm <username>
/lastfmoff
/playing
/mood <0-10>
/myself
/songcharts
```

## Gatilhos textuais

Também acionam a lógica de `/playing`:

```text
tocando, pifm, cyo, py, braya, dead, ag, rosan, roro, ro, rafarl, pipi, bressing, kur, xxt, ts, cebrutius, tigraofm, djpi, royalfm, geeksfm, radinho, qap
```

## Last.fm

Para conectar:

```text
/lastfm username
```

Também aceita:

```text
/lastfm @username
```

Para remover:

```text
/lastfmoff
```

O Last.fm usa scrobbles públicos via `user.getrecenttracks`. Não usa OAuth.

## Spotify

Para conectar:

```text
/login
```

O bot gera a URL OAuth do Spotify e salva `access_token`, `refresh_token` e expiração.

Para remover a sessão Spotify:

```text
/logout
```

## Variáveis de ambiente

```text
TELEGRAM_BOT_TOKEN
BASE_URL
OWNER_ID
SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET
LASTFM_API_KEY
DATABASE_URL
```

`DATABASE_URL` é opcional. Se ausente, o projeto usa SQLite em `/data/app.db`.

## Deploy Railway

Start command:

```text
python -m app.bootstrap
```

Healthcheck:

```text
/healthz
```

## Observações técnicas

- O Last.fm gera `track_id` curto no formato `lfm:<hash>` para caber em callbacks do Telegram.
- O botão de like carrega `owner_user_id` no callback, preservando curtidas por dono da postagem.
- A camada `music_service` evita misturar Last.fm dentro do serviço Spotify.
- As tabelas principais são `spotify_tokens`, `lastfm_profiles`, `track_plays` e `track_likes`.
