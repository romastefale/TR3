#!/bin/bash
set -e

# Post-merge: instala deps Python. Idempotente.
if [ -f requirements.txt ]; then
    pip install --quiet --disable-pip-version-check -r requirements.txt
fi
