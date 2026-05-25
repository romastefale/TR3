# tigraoRADIO TR3 — Memória do Projeto

## Stack
- Python · aiogram **3.27.0** · FastAPI · SQLAlchemy
- Editor: Replit · Deploy: Railway
- DB local: `/tmp/data` (em Replit `/data` é read-only)
- OWNER_ID: `8505890439` · Idioma: PT-BR

## Git policy
- Branch alvo: `month` via `https://x-access-token:${GITHUB_TOKEN}@github.com/romastefale/TR3` `HEAD:refs/heads/month`
- **NUNCA** `main`
- Push só quando user mandar literalmente "push"
- Git destrutivo (incl. `git commit`) **só** via `project_task`

## Telegram Bot API — versão de referência
**Bot API 10.0** (lançada 8/maio/2026). Sempre assumir 10.0 como base, não versões antigas.

Features 9.x/10.0 já relevantes ao projeto:
- `InlineKeyboardButton.style` ∈ `"primary"` (azul) / `"success"` (verde) / `"danger"` (vermelho)
- `InlineKeyboardButton.icon_custom_emoji_id` (custom emoji no botão)
- `CopyTextButton` + `copy_text` em `InlineKeyboardButton`
- `getUserPersonalChatMessages` (msgs do canal pessoal do user)
- `can_react_to_messages` em `ChatPermissions`/`ChatMemberRestricted` + `use_independent_chat_permissions` em `restrictChatMember`/`setChatPermissions`
- `return_bots` em `getChatAdministrators`
- `verifyUser` / `verifyChat` / `removeUserVerification` / `removeChatVerification`
- `setChatMemberTag` + `can_edit_tag` + `can_manage_tags` (9.2)
- `deleteMessageReaction` / `deleteAllMessageReactions` (já wrapados em `actions.py`)
- Reactions em service messages (9.1)
- `message_effect_id` em `sendMessage` (efeito visual ao enviar)
- `LinkPreviewOptions(is_disabled=True)`
- Guest mode (`answerGuestQuery`, `guest_message`)
- Bot-to-bot communication (opt-in mútuo)
- `ChatOwnerLeft` / `ChatOwnerChanged` em `Message`
- ⚠️ Breaking 10.0: `sendMessage` com `message_thread_id` em chat privado → `400 message thread not found`. **Evitar DM topics.**

## Políticas de moderação
- **ZERO mudança visual.** Owner-only NUNCA público.
- **Detecção NUNCA age automaticamente.** Todo módulo de detecção (X4, e futuros: CAS, captcha, anti-edit, anti-channel-as-user, anti-forward, etc.) deve apenas notificar o owner via DM com botões de ação (Ban / Mute / Del / Ignorar). A decisão é sempre do owner.
- Hard-block: nenhuma ação de moderação pode atingir `OWNER_ID`.

## Arquitetura de moderação atual
- **Routers:** `ddx_router`, `customize_router`, `member_tag_router`, `pinned_media_router`, `new_member_watch_router` (X4), `pm_router`, `router` (catch-all `/tigrao`)
- **Preprocessors** (rodam antes do dispatcher em `app/main.py`):
  - X4 new-member-watch preprocessor (linha ~527) — ANTES do DDX
  - DDX preprocessor (linha ~531)
- **Actions wrapadas** em `app/moderation_tigrao/actions.py`: ban/unban/mute/unmute, delete_message, copy_message, set_group_title/description/photo, set_member_tag, create_direct_link, create_approval_link, approve_join_request, reset_entry, delete_message_reaction, delete_all_message_reactions, mute_reactions (global), resolve_user_target
- **Sprints concluídos:**
  - X3: reaction audit + painel rmod
  - X4: alerta DM ao owner quando membro novo posta link nas 5 primeiras msgs (TTL 24h, cap 5, race fixado com UPDATE atômico)

## Preferências do usuário
- Linguagem PT-BR sempre
- Estudos e propostas: tabela + impacto/esforço, conciso
- Nunca implementar sem pedido explícito
- Quando perguntar sobre Bot API, assumir 10.0 (não 8.x)
- **Interface SEM emojis.** Usar apenas cores nativas de botão (`style="danger"`/`"success"`/`"primary"`) pra sinalização visual. Inclui: nenhum emoji em texto de botão, em header de painel, em mensagens de confirmação, em cards de detecção. Texto limpo + cor.
