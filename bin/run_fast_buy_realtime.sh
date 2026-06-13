#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
KEEP_WARM_INTERVAL="${KEEP_WARM_INTERVAL:-15}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export BYBIT_FAST_EXECUTOR_ENABLED="${BYBIT_FAST_EXECUTOR_ENABLED:-1}"
export BYBIT_FAST_EXECUTOR_AUTO_BUILD="${BYBIT_FAST_EXECUTOR_AUTO_BUILD:-1}"
export BYBIT_SPOT_BUY_ENABLED="${BYBIT_SPOT_BUY_ENABLED:-1}"
export BYBIT_QUERY_FILL_AFTER_BUY="${BYBIT_QUERY_FILL_AFTER_BUY:-0}"
export BYBIT_WS_ORDER_ENABLED="${BYBIT_WS_ORDER_ENABLED:-0}"
export BYBIT_CPP_WS_EXECUTOR_ENABLED="${BYBIT_CPP_WS_EXECUTOR_ENABLED:-0}"
export BYBIT_ORDER_TRANSPORT_PREFERENCE="${BYBIT_ORDER_TRANSPORT_PREFERENCE:-cpp}"
export BYBIT_PREFER_CACHED_SYMBOL_CHECK="${BYBIT_PREFER_CACHED_SYMBOL_CHECK:-1}"
export BYBIT_RESOLVE_DUPLICATE_ORDER_LINK_ID="${BYBIT_RESOLVE_DUPLICATE_ORDER_LINK_ID:-0}"
export LISTING_CLASSIFIER_BACKEND="${LISTING_CLASSIFIER_BACKEND:-cpp}"
export LISTING_CPP_ULTRA_ENGINE_ENABLED="${LISTING_CPP_ULTRA_ENGINE_ENABLED:-1}"
export LISTING_TDLIB_WATCH_CHATS="${LISTING_TDLIB_WATCH_CHATS:--1002562064658:upbit_news,-1001202540487:BithumbExchange}"

if [[ "${LISTING_NATIVE_AUTO_BUILD:-1}" == "1" ]]; then
  if [[ ! -f "$ROOT_DIR/bin/liblisting_classifier_cpp.dylib" && ! -f "$ROOT_DIR/bin/liblisting_classifier_cpp.so" ]] || \
     [[ ! -f "$ROOT_DIR/bin/liblisting_ultra_engine.dylib" && ! -f "$ROOT_DIR/bin/liblisting_ultra_engine.so" ]]; then
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

if [[ "${LISTING_CLASSIFIER_VERIFY:-1}" == "1" ]]; then
  if ! VERIFY_OUTPUT="$("$PYTHON_BIN" "$ROOT_DIR/bin/verify_listing_classifiers.py" --require-tdlib-relay 2>&1)"; then
    echo "$VERIFY_OUTPUT" >&2
    echo "Listing classifier fixture verification failed; refusing to start realtime buy." >&2
    exit 1
  fi
fi

cd "$ROOT_DIR"

exec "$PYTHON_BIN" main.py \
  --realtime \
  --realtime-backend race \
  --strict-realtime \
  --keep-warm-interval "$KEEP_WARM_INTERVAL" \
  --memory-state \
  --ultra-buy \
  --no-telegram \
  "$@"
