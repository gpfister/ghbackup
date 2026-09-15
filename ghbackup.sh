#!/usr/bin/env bash
#
# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT
#

set -e

# Ensure uv is located
if command -v uv >/dev/null 2>&1; then
    UV_BIN="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
else
    echo "Error: uv is not installed or not found in PATH." >&2
    exit 1
fi

exec "$UV_BIN" run src.main:app "$@"
