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
export LISTING_CLASSIFIER_BACKEND="${LISTING_CLASSIFIER_BACKEND:-cpp}"

if [[ "${LISTING_NATIVE_AUTO_BUILD:-1}" == "1" ]]; then
  if [[ ! -f "$ROOT_DIR/bin/liblisting_classifier_cpp.dylib" && ! -f "$ROOT_DIR/bin/liblisting_classifier_cpp.so" ]]; then
    bash "$ROOT_DIR/bin/build_native_classifiers.sh" >/dev/null 2>&1 || true
  fi
fi

if [[ "${LISTING_TDLIB_RELAY_AUTO_BUILD:-1}" == "1" ]]; then
  if [[ ! -x "$ROOT_DIR/bin/tdlib_json_relay" ]] || \
     [[ "$ROOT_DIR/cpp/tdlib_json_relay.cpp" -nt "$ROOT_DIR/bin/tdlib_json_relay" ]] || \
     [[ "$ROOT_DIR/cpp/build_tdlib_relay.sh" -nt "$ROOT_DIR/bin/tdlib_json_relay" ]]; then
    bash "$ROOT_DIR/cpp/build_tdlib_relay.sh" >/dev/null 2>&1 || true
  fi
fi

cd "$ROOT_DIR"

exec "$PYTHON_BIN" main.py \
  --realtime \
  --realtime-backend race \
  --strict-realtime \
  --memory-state \
  --source-only \
  --no-telegram \
  --state-flush-interval 0 \
  "$@"
