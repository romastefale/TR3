from __future__ import annotations

import os

import uvicorn

port = int(os.getenv("PORT", "8080"))
print(f"Starting live media worker on 0.0.0.0:{port}", flush=True)

uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
