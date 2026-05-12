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


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("Escolher grupo", "tigrao:groups", "primary"),
            ],
            [
                _button("Ações de usuário", "tigrao:user_actions", "primary"),
                _button("Links", "tigrao:links", "primary"),
            ],
            [
                _button("Filtros DDX", "tigrao:ddx", "primary"),
                _button("Mensagens", "tigrao:messages", "primary"),
            ],
            [
                _button("Logs", "tigrao:logs", "primary"),
                _button("Fechar", "tigrao:close", "danger"),
            ],
        ]
    )
