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
            [_button("Personalização", "tigrao:customize", "success"), _button("Logs", "tigrao:logs", "primary")],
            [_button("Fechar", "tigrao:close", "danger")],
        ]
    )


def groups_keyboard(groups: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups[:10]:
        chat_id = int(group["chat_id"])
        title = str(group.get("title") or chat_id)
        label = title if len(title) <= 40 else title[:37] + "..."
        rows.append([_button(label, f"tigrao:group:{chat_id}", "primary")])
    rows.append([_button("Digitar chat_id", "tigrao:group:manual", "success")])
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
    rows = [[_button("Gerar link direto", "tigrao:link:direct", "success")], [_button("Gerar link com aprovação", "tigrao:link:approval", "primary")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def messages_keyboard() -> InlineKeyboardMarkup:
    rows = [[_button("Apagar por link", "tigrao:message:delete_link", "danger")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customize_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Enviar mensagem", "tigrao:message:send", "primary")],
        [_button("Enviar e fixar", "tigrao:message:pin", "success")],
        [_button("Enviar mídia", "tigrao:message:media", "primary")],
        [_button("Alterar foto do grupo", "tigrao:customize:photo", "success")],
        [_button("Alterar nome", "tigrao:customize:title", "primary"), _button("Alterar bio", "tigrao:customize:bio", "primary")],
        [_button("Tag de membro", "tigrao:customize:member_tag", "primary")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ddx_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("Adicionar filtro", "tigrao:ddx:add", "success")],
        [_button("Remover filtro", "tigrao:ddx:remove", "danger")],
        [_button("Listar filtros", "tigrao:ddx:list", "primary")],
        [_button("Desligar DDX", "tigrao:ddx:off", "danger")],
    ]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def logs_keyboard() -> InlineKeyboardMarkup:
    rows = [[_button("Atualizar logs", "tigrao:logs:refresh", "primary")]]
    rows.extend(_back_close_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)
