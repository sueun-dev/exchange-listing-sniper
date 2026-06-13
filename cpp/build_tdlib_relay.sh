#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CPP_DIR="$ROOT_DIR/cpp"
BIN_DIR="$ROOT_DIR/bin"
SRC="$CPP_DIR/tdlib_json_relay.cpp"
OUT="$BIN_DIR/tdlib_json_relay"
TDLIB_PREFIX="${TDLIB_PREFIX:-/opt/homebrew/opt/tdlib}"
TDLIB_SOURCE_DIR="${TDLIB_SOURCE_DIR:-}"
TDLIB_BUILD_DIR="${TDLIB_BUILD_DIR:-}"
CXX_OPT_FLAGS="${CXX_OPT_FLAGS:--O3 -DNDEBUG -march=native}"

source "$CPP_DIR/build_support.sh"
prepare_openssl_prefix "$ROOT_DIR"
repair_tdlib_openssl_links "$ROOT_DIR"

if [[ -z "$TDLIB_BUILD_DIR" && -f "$ROOT_DIR/vendor/tdlib-latest/build/libtdjson.dylib" ]]; then
  TDLIB_SOURCE_DIR="$ROOT_DIR/vendor/tdlib-latest"
  TDLIB_BUILD_DIR="$ROOT_DIR/vendor/tdlib-latest/build"
fi

mkdir -p "$BIN_DIR"

if [[ -n "$TDLIB_BUILD_DIR" ]]; then
  TDLIB_INCLUDE_FLAGS=()
  if [[ -n "$TDLIB_SOURCE_DIR" ]]; then
    TDLIB_INCLUDE_FLAGS+=(-I"$TDLIB_SOURCE_DIR")
  fi
  TDLIB_INCLUDE_FLAGS+=(-I"$TDLIB_BUILD_DIR")
  TDLIB_LIB_DIR="$TDLIB_BUILD_DIR"
  TDLIB_LINK_TARGET="$TDLIB_BUILD_DIR/libtdjson.dylib"
else
  TDLIB_INCLUDE_FLAGS=(-I"$TDLIB_PREFIX/include")
  TDLIB_LIB_DIR="$TDLIB_PREFIX/lib"
  TDLIB_LINK_TARGET="-ltdjson"
fi

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
    -std=c++20 -pthread \
    "$SRC" \
    "${TDLIB_INCLUDE_FLAGS[@]}" \
    -L"$TDLIB_LIB_DIR" \
    -L"$OPENSSL_PREFIX/lib" \
    -Wl,-rpath,"$TDLIB_LIB_DIR" \
    -Wl,-rpath,"$OPENSSL_PREFIX/lib" \
    "$TDLIB_LINK_TARGET" $CURL_LIBS $OPENSSL_LIBS -lz \
    -o "$OUT"
else
  c++ $CXX_OPT_FLAGS -std=c++20 -pthread \
    "$SRC" \
    "${TDLIB_INCLUDE_FLAGS[@]}" \
    -I"$OPENSSL_PREFIX/include" \
    -L"$TDLIB_LIB_DIR" \
    -L"$OPENSSL_PREFIX/lib" \
    -Wl,-rpath,"$TDLIB_LIB_DIR" \
    -Wl,-rpath,"$OPENSSL_PREFIX/lib" \
    "$TDLIB_LINK_TARGET" -lcurl -lssl -lcrypto -lz \
    -o "$OUT"
fi

echo "Built $OUT"
