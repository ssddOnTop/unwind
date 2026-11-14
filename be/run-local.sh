#!/bin/sh
set -eu
BE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname -- "$BE")
export UV_CACHE_DIR="$ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$ROOT/.cache/python"
export PYTHONPYCACHEPREFIX="$ROOT/.cache/pycache"
export TMPDIR="$ROOT/.cache/tmp"
mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$PYTHONPYCACHEPREFIX" "$TMPDIR"
# The supported process is one local worker; arbitrary host overrides are not accepted.
exec uv run --project "$BE" --frozen python -m companion "$@"
