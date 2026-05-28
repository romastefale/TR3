"""Configuração compartilhada da suíte de testes.

Em Replit `/data` é read-only — qualquer import de `app.config.settings`
chama `DATA_DIR.mkdir()` no nível de módulo e quebraria a coleta dos testes.
Forçamos `DATA_DIR=/tmp/data` ANTES de qualquer import de `app.*`, que é o
único requisito de ambiente da suíte (ver task-32).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATA_DIR", "/tmp/data")
Path(os.environ["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
