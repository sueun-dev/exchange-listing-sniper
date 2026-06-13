#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CPP_DIR="$ROOT_DIR/cpp"
BIN_DIR="$ROOT_DIR/bin"
SRC="$CPP_DIR/bybit_fast_path.cpp"
OUT="$BIN_DIR/bybit_fast_path"
CXX_OPT_FLAGS="${CXX_OPT_FLAGS:--O3}"

source "$CPP_DIR/build_support.sh"
prepare_openssl_prefix "$ROOT_DIR"

mkdir -p "$BIN_DIR"

if command -v pkg-config >/dev/null 2>&1; then
  CURL_CFLAGS=""
  CURL_LIBS=""
  OPENSSL_CFLAGS=""
  OPENSSL_LIBS=""
  if pkg-config --exists libcurl; then
    CURL_CFLAGS="$(pkg-config --cflags libcurl)"
    CURL_LIBS="$(pkg-config --libs libcurl)"
  fi
  if pkg-config --exists openssl; then
    OPENSSL_CFLAGS="$(pkg-config --cflags openssl)"
    OPENSSL_LIBS="$(pkg-config --libs openssl)"
  fi
  c++ $CURL_CFLAGS $OPENSSL_CFLAGS $CXX_OPT_FLAGS \
    -std=c++20 "$SRC" \
    -Wl,-rpath,"$OPENSSL_PREFIX/lib" \
    $CURL_LIBS $OPENSSL_LIBS \
    -o "$OUT"
else
  c++ $CXX_OPT_FLAGS -std=c++20 \
    "$SRC" \
    -I"$OPENSSL_PREFIX/include" \
    -L"$OPENSSL_PREFIX/lib" \
    -Wl,-rpath,"$OPENSSL_PREFIX/lib" \
    -lcurl -lssl -lcrypto \
    -o "$OUT"
fi

echo "Built $OUT"
