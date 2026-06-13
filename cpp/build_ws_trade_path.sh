#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CPP_DIR="$ROOT_DIR/cpp"
BIN_DIR="$ROOT_DIR/bin"
SRC="$CPP_DIR/bybit_ws_trade_path.cpp"
OUT="$BIN_DIR/bybit_ws_trade_path"
BOOST_PREFIX="${BOOST_PREFIX:-/opt/homebrew/opt/boost}"

source "$CPP_DIR/build_support.sh"
prepare_openssl_prefix "$ROOT_DIR"

mkdir -p "$BIN_DIR"

if [[ ! -f "$BOOST_PREFIX/include/boost/asio/connect.hpp" ]]; then
  echo "Boost headers not found: $BOOST_PREFIX/include/boost/asio/connect.hpp" >&2
  echo "Install Boost or set BOOST_PREFIX to a prefix containing include/boost/asio." >&2
  exit 1
fi

c++ -O3 -std=c++20 \
  "$SRC" \
  -I"$BOOST_PREFIX/include" \
  -I"$OPENSSL_PREFIX/include" \
  -L"$OPENSSL_PREFIX/lib" \
  -Wl,-rpath,"$OPENSSL_PREFIX/lib" \
  -lssl -lcrypto \
  -o "$OUT"

echo "Built $OUT"
