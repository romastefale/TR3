---
name: Canvas file_id cache
description: Por que /tcanvas e /tly cacheiam o vídeo por file_id do Telegram e as regras de chave/robustez.
---

# Cache de Spotify Canvas via file_id do Telegram

`/tcanvas` e `/tly` reusam o `file_id` que o Telegram devolve ao enviar um vídeo:
depois do 1º envio de uma faixa, os próximos vão por `file_id` (sem rebaixar do
CDN nem re-subir). Persistido em DB (`canvas_files`) via `canvas_cache_service`;
entrega centralizada em `app/bot/canvas_delivery.py:deliver_canvas`.

**Regra de chave (crítica):** a chave do cache é o **Spotify track_id base62**
(`canvas_track_id` resolvido). NUNCA o `lfm:<hash>` interno — esse é a chave
histórica dos likes (`register_card`) e jamais resolve Canvas. Os dois IDs são
distintos e ambos precisam ser preservados no mesmo fluxo.

**Why:** `file_id` é por-bot e reusável entre chats (Telegram Bot FAQ / grammY
docs); aiogram 3 `send_video` aceita `file_id` como `str`. MAS o Telegram não
garante estabilidade do `file_id` — ele PODE mudar/invalidar. Por isso guardamos
`file_id` (reenvia) + `file_unique_id` (dedup/diagnóstico, NÃO reenvia/baixa) e,
quando o envio por `file_id` falha (ex.: "wrong file_id"/400), chamamos
`forget()` e re-subimos os bytes.

**How to apply:**
- Mexeu em `deliver_canvas`? Mantenha: hit→send by file_id; falha→forget+miss;
  fallback foto/texto em TODO caminho; `register_card`+reação no final sempre.
- `CANVAS_CACHE_CHANNEL_ID` (env, 0=sem canal): com canal, sobe 1x lá (bot tem
  que ser ADMIN) e o grupo recebe por file_id; sem canal, captura o file_id do
  próprio envio no grupo. Em ambos o ganho vale a partir do 2º envio.
- Lock por-track é **process-local** (memória). Multi-worker pode duplicar upload
  da mesma faixa (sem corrupção, só perde coalescência global).
