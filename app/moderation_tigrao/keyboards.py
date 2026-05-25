from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    if style:
        try:
            return InlineKeyboardButton(
                text=text,
                callback_data=callback_data,
                style=style,
            )
        except Exception:
            pass
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _back_close_rows() -> list[list[InlineKeyboardButton]]:
    return [[_button("Voltar", "tigrao:home", "primary"), _button("Fechar", "tigrao:close", "danger")]]


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Escolher grupo", "tigrao:groups", "primary")],
            [_button("Ações de usuário", "tigrao:user_actions", "primary"), _button("Links", "tigrao:links", "primary")],
            [_button("Filtros DDX", "tigrao:ddx", "primary"), _button("Mensagens", "tigrao:messages", "primary")],
            [_button("Moderar Reactions", "tigrao:rmod", "primary")],
            [_button("Personalização", "tigrao:customize", "primary"), _button("Logs", "tigrao:logs", "primary")],
            [_button("Fechar", "tigrao:close", "danger")],
        ]
    )


def reactions_mod_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Apagar reaction de 1 pessoa (msg)", "tigrao:rmod:del_user_msg", "danger")],
        [_button("Apagar reactions de 1 pessoa (grupo)", "tigrao:rmod:del_user_chat", "danger")],
        [_button("Apagar TODAS reactions desta msg", "tigrao:rmod:del_all_msg", "danger")],
        [_button("Silenciar reactor", "tigrao:rmod:mute_react", "danger")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rmod_reactors_picker_keyboard(reactors: list[dict], nonce: str) -> InlineKeyboardMarkup:
    """Sprint X3: picker de reactors (1 botão por user).

    `reactors` é a lista (já ordenada e truncada pelo service) salva em
    `session.payload['reactors']`. `nonce` é um token curto único por
    render do picker, persistido em `session.payload['picker_nonce']`.

    Callback format: `tigrao:rmod:pick:<nonce>:<user_id>`
    - nonce: invalida cliques em pickers antigos quando um novo é
      aberto (evita resolver pro user errado se o owner ignora um
      picker antigo no histórico e abre outro fluxo).
    - user_id: identidade imutável (não depende de índice).

    Layout: 1 user por linha (nome + emojis recentes). Linhas finais
    com fallback "digitar manualmente" e "cancelar". O botão "Voltar"
    leva ao menu rmod (não cancela o fluxo todo).
    """
    rows: list[list[InlineKeyboardButton]] = []
    for r in reactors:
        name = (
            r.get("user_name")
            or (f"@{r['user_username']}" if r.get("user_username") else None)
            or str(r.get("user_id"))
        )
        emojis = "".join(r.get("emojis", [])[:3])
        label = f"{name} {emojis}".strip()
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([_button(label, f"tigrao:rmod:pick:{nonce}:{r['user_id']}", "primary")])
    rows.append([_button("Digitar manualmente", f"tigrao:rmod:manual:{nonce}", "primary")])
    rows.append([_button("Voltar", "tigrao:rmod", "primary"), _button("Cancelar", "tigrao:rmod:cancel", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rmod_duration_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("10 min", "tigrao:rmod:dur:10m", "primary"), _button("1 hora", "tigrao:rmod:dur:1h", "primary")],
        [_button("6 horas", "tigrao:rmod:dur:6h", "primary"), _button("24 horas", "tigrao:rmod:dur:24h", "primary")],
        [_button("7 dias", "tigrao:rmod:dur:7d", "primary"), _button("Indefinido", "tigrao:rmod:dur:i", "danger")],
        [_button("Cancelar", "tigrao:rmod:cancel", "danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rmod_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Confirmar", "tigrao:rmod:confirm", "success")],
            [_button("Cancelar", "tigrao:rmod:cancel", "danger")],
            [_button("Voltar", "tigrao:rmod", "primary")],
        ]
    )


def groups_keyboard(groups: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups[:10]:
        chat_id = int(group["chat_id"])
        title = str(group.get("title") or chat_id)
        label = title if len(title) <= 40 else title[:37] + "..."
        rows.append([_button(label, f"tigrao:group:{chat_id}", "primary")])
    rows.append([_button("Digitar chat_id", "tigrao:group:manual", "primary")])
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_actions_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Banir", "tigrao:action:ban", "danger"), _button("Desbanir", "tigrao:action:unban", "success")],
        [_button("Mutar", "tigrao:action:mute", "danger"), _button("Desmutar", "tigrao:action:unmute", "success")],
        [_button("Aprovar entrada", "tigrao:action:approve", "success")],
        [_button("Resetar entrada", "tigrao:action:reset", "danger")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Confirmar", "tigrao:confirm", "success")],
            [_button("Cancelar", "tigrao:cancel", "danger")],
            [_button("Voltar", "tigrao:user_actions", "primary")],
        ]
    )


def links_keyboard() -> InlineKeyboardMarkup:
    rows = [[_button("Gerar link direto", "tigrao:link:direct", "primary")], [_button("Gerar link com aprovação", "tigrao:link:approval", "primary")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def messages_keyboard() -> InlineKeyboardMarkup:
    rows = [[_button("Apagar por link", "tigrao:message:delete_link", "danger")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customize_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Enviar mensagem", "tigrao:message:send", "primary")],
        [_button("Enviar e fixar", "tigrao:message:pin", "primary")],
        [_button("Enviar mídia", "tigrao:message:media", "primary")],
        [_button("Enviar mídia e fixar", "tigrao:message:media_pin", "primary")],
        [_button("Alterar foto do grupo", "tigrao:customize:photo", "primary")],
        [_button("Alterar nome", "tigrao:customize:title", "primary"), _button("Alterar bio", "tigrao:customize:bio", "primary")],
        [_button("Tag de membro", "tigrao:customize:member_tag", "primary")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ddx_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Adicionar filtro", "tigrao:ddx:add", "primary")],
        [_button("Remover filtro", "tigrao:ddx:remove", "primary")],
        [_button("Listar filtros", "tigrao:ddx:list", "primary")],
        [_button("Desligar DDX", "tigrao:ddx:off", "primary")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def logs_keyboard() -> InlineKeyboardMarkup:
    rows = [[_button("Atualizar logs", "tigrao:logs:refresh", "primary")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)
