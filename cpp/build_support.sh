#!/usr/bin/env bash

prepare_openssl_prefix() {
  local root_dir="$1"
  local local_openssl_prefix="$root_dir/vendor/openssl-local"

  if [[ -z "${OPENSSL_PREFIX:-}" && -d "$local_openssl_prefix/lib" ]]; then
    OPENSSL_PREFIX="$local_openssl_prefix"
  else
    OPENSSL_PREFIX="${OPENSSL_PREFIX:-/opt/homebrew/opt/openssl@3}"
  fi
  export OPENSSL_PREFIX

  if [[ -d "$OPENSSL_PREFIX/lib/pkgconfig" ]]; then
    export PKG_CONFIG_PATH="$OPENSSL_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
  fi

  _repair_local_openssl_install_names "$local_openssl_prefix"
}

repair_tdlib_openssl_links() {
  local root_dir="$1"
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return
  fi
  if ! command -v install_name_tool >/dev/null 2>&1; then
    return
  fi
  if ! command -v otool >/dev/null 2>&1; then
    return
  fi

  local tdlib_dylib="$root_dir/vendor/tdlib-latest/build/libtdjson.1.8.63.dylib"
  if [[ ! -f "$tdlib_dylib" ]]; then
    return
  fi

  local ssl_ref=""
  local crypto_ref=""
  ssl_ref="$(otool -L "$tdlib_dylib" | awk '/libssl\.3\.dylib/ {print $1; exit}')"
  crypto_ref="$(otool -L "$tdlib_dylib" | awk '/libcrypto\.3\.dylib/ {print $1; exit}')"

  if [[ -n "$ssl_ref" && "$ssl_ref" != "@rpath/libssl.3.dylib" ]]; then
    install_name_tool -change "$ssl_ref" "@rpath/libssl.3.dylib" "$tdlib_dylib" 2>/dev/null || true
  fi
  if [[ -n "$crypto_ref" && "$crypto_ref" != "@rpath/libcrypto.3.dylib" ]]; then
    install_name_tool -change "$crypto_ref" "@rpath/libcrypto.3.dylib" "$tdlib_dylib" 2>/dev/null || true
  fi
}

_repair_local_openssl_install_names() {
  local local_openssl_prefix="$1"
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return
  fi
  if [[ "$OPENSSL_PREFIX" != "$local_openssl_prefix" ]]; then
    return
  fi
  if ! command -v install_name_tool >/dev/null 2>&1; then
    return
  fi
  if ! command -v otool >/dev/null 2>&1; then
    return
  fi

  local ssl_dylib="$OPENSSL_PREFIX/lib/libssl.3.dylib"
  local crypto_dylib="$OPENSSL_PREFIX/lib/libcrypto.3.dylib"
  local old_crypto_id=""

  if [[ -f "$crypto_dylib" ]]; then
    old_crypto_id="$(otool -D "$crypto_dylib" | sed -n '2p')"
    install_name_tool -id "@rpath/libcrypto.3.dylib" "$crypto_dylib" 2>/dev/null || true
  fi
  if [[ -f "$ssl_dylib" ]]; then
    install_name_tool -id "@rpath/libssl.3.dylib" "$ssl_dylib" 2>/dev/null || true
    if [[ -n "$old_crypto_id" && "$old_crypto_id" != "@rpath/libcrypto.3.dylib" ]]; then
      install_name_tool -change "$old_crypto_id" "@rpath/libcrypto.3.dylib" "$ssl_dylib" 2>/dev/null || true
    fi
    install_name_tool -change "$crypto_dylib" "@rpath/libcrypto.3.dylib" "$ssl_dylib" 2>/dev/null || true
  fi
}
