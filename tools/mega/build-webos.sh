#!/usr/bin/env bash
# Kodi Mega: build the webOS .ipk for Master's LG TVs (32-bit ARM userspace) on MegaServer.
# Steps follow docs/README.webOS.md. Re-runs are incremental; delete $WORK to start clean.
# Usage: tools/mega/build-webos.sh [toolchain|depends|kodi|ipk|symbols|all]   (default: all)
# Every ipk also archives the UNSTRIPPED kodi-webos to $SYMBOLS/<commit>/ (the ipk itself is stripped).
# Build worktree: ~/kodi-mega-builds/webos, building into its own dist/ (git-ignored).
# Never `git clean -x` a build worktree: it deletes dist/ and Kodi's depends setup (hours to rebuild).
set -euo pipefail

HOST=arm-webos-linux-gnueabi                                 # 32-bit ARM target
TC_RELEASE=webos-b17b4cc                                     # openlgtv/buildroot-nc4 release: GCC 12.2 (2026.08 = GCC 16, too new for Omega)
SRC=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${WORK:-$SRC/dist}                                      # tarballs, depends prefix, cmake build dir
TC=$HOME/kodi-mega-builds/toolchains/arm-webos-linux-gnueabi_sdk-buildroot  # unpacked toolchain, shared by every WORK
SYMBOLS=${SYMBOLS:-$HOME/kodi-mega-builds/symbols/webos}     # per-commit kodi-webos, outside dist/ so it survives a clean
CMAKE_ARGS='-DCORE_PLATFORM_NAME=webos -DENABLE_DBUS=OFF -DENABLE_CEC=OFF -DENABLE_PIPEWIRE=OFF -DHAVE_LINUX_UDMABUF=OFF'
JOBS=$(getconf _NPROCESSORS_ONLN)
export PATH="$HOME/.npm-global/bin:$PATH"                    # ares-package (npm i -g @webos-tools/cli)

step=${1:-all}
mkdir -p "$WORK/tarballs" "$WORK/deps" "$WORK/build" "$WORK/dl"

toolchain() {
  [ -x "$TC/bin/$HOST-gcc" ] && return 0
  mkdir -p "$(dirname "$TC")"
  curl -fL -o "$(dirname "$TC")/toolchain-$TC_RELEASE.tar.gz" \
    "https://github.com/openlgtv/buildroot-nc4/releases/download/$TC_RELEASE/arm-webos-linux-gnueabi_sdk-buildroot.tar.gz"
  tar xzf "$(dirname "$TC")/toolchain-$TC_RELEASE.tar.gz" -C "$(dirname "$TC")"
  (cd "$TC" && ./relocate-sdk.sh)
}

depends() {
  cd "$SRC/tools/depends"
  [ -x configure ] || ./bootstrap
  ./configure --prefix="$WORK/deps" --host=$HOST --with-toolchain="$TC" \
    --with-tarballs="$WORK/tarballs" --enable-debug=no
  make -j"$JOBS"
}

kodi() {
  # LOCAL PATCH (megarushing): incremental builds also need the real RPU
  # conversion library. A settings toggle/EL discard without libdovi is not it.
  local cargo_jobs=$((JOBS > 8 ? 8 : JOBS))
  CARGO_BUILD_JOBS="$cargo_jobs" make -C "$SRC/tools/depends/native" -j"$cargo_jobs" rustup cargo-c
  CARGO_BUILD_JOBS="$cargo_jobs" make -C "$SRC/tools/depends/target/libdovi"
  make -C "$SRC/tools/depends/target/cmakebuildsys" BUILD_DIR="$WORK/build" CMAKE_EXTRA_ARGUMENTS="$CMAKE_ARGS"
  make -C "$WORK/build" -j"$JOBS"
}

ipk() {
  command -v ares-package >/dev/null || { echo "ares-package missing: npm i -g @webos-tools/cli"; exit 1; }
  make -C "$WORK/build" ipk
  ls -l "$WORK/build"/*.ipk
  symbols
}

symbols() {
  local dir="$SYMBOLS/$(git -C "$SRC" rev-parse --short=10 HEAD)"
  mkdir -p "$dir" && cp -p "$WORK/build/kodi-webos" "$dir/" && echo "symbols: $dir/kodi-webos"
}

case "$step" in
  toolchain) toolchain ;;
  depends)   toolchain; depends ;;
  kodi)      kodi ;;
  ipk)       ipk ;;
  symbols)   symbols ;;
  all)       toolchain; depends; kodi; ipk ;;
  *)         echo "usage: $0 [toolchain|depends|kodi|ipk|symbols|all]"; exit 2 ;;
esac
