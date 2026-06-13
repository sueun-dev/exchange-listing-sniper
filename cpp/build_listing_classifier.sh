#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CPP_DIR="$ROOT_DIR/cpp"
BIN_DIR="$ROOT_DIR/bin"
SRC="$CPP_DIR/listing_classifier.cpp"

mkdir -p "$BIN_DIR"

UNAME="$(uname -s)"
if [[ "$UNAME" == "Darwin" ]]; then
  OUT="$BIN_DIR/liblisting_classifier_cpp.dylib"
  c++ -O3 -std=c++20 -dynamiclib "$SRC" -o "$OUT"
else
  OUT="$BIN_DIR/liblisting_classifier_cpp.so"
  c++ -O3 -std=c++20 -shared -fPIC "$SRC" -o "$OUT"
fi

echo "Built $OUT"
