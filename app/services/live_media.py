from __future__ import annotations

import os

LIVE_MEDIA_ENABLED = os.getenv("LIVE_MEDIA_ENABLED", "").lower() in {"1", "true", "yes", "on"}
