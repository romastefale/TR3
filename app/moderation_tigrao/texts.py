from __future__ import annotations


def home_text() -> str:
    return (
        "Tigrão — painel de moderação\n\n"
        "Escolha uma opção pelos botões abaixo.\n"
        "Todas as confirmações e erros ficam somente neste privado.\n"
        "No grupo, o bot apenas executa ações administrativas inevitáveis."
    )


def blocked_text() -> str:
    return "Acesso negado."


def error_text(title: str, detail: str, fix: str | None = None) -> str:
    text = f"Tigrão — erro\n\n{title}\n\nMotivo: {detail}"
    if fix:
        text += f"\nCorreção: {fix}"
    return text


def success_text(title: str, detail: str) -> str:
    return f"Tigrão — ação concluída\n\n{title}\n\n{detail}"
