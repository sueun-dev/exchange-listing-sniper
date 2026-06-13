#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
KEEP_WARM_INTERVAL="${KEEP_WARM_INTERVAL:-30}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export BYBIT_FAST_EXECUTOR_ENABLED="${BYBIT_FAST_EXECUTOR_ENABLED:-1}"
export BYBIT_FAST_EXECUTOR_AUTO_BUILD="${BYBIT_FAST_EXECUTOR_AUTO_BUILD:-1}"

cd "$ROOT_DIR"

exec "$PYTHON_BIN" main.py \
  --realtime \
  --strict-realtime \
  --keep-warm-interval "$KEEP_WARM_INTERVAL" \
  "$@"
