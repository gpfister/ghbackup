#!/usr/bin/env bash
#
# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure uv or venv is located
if command -v uv >/dev/null 2>&1; then
    exec uv run src.main:app "$@"
elif [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" run src.main:app "$@"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    exec "$SCRIPT_DIR/.venv/bin/python" -m src.main "$@"
else
    echo "Error: Neither uv nor .venv python found." >&2
    exit 1
fi
