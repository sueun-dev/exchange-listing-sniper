#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

bash "$ROOT_DIR/cpp/build_listing_classifier.sh"
bash "$ROOT_DIR/cpp/build_listing_ultra_engine.sh"
bash "$ROOT_DIR/rust/listing_classifier/build.sh"
