#!/usr/bin/env bash
# Kodi Mega: build the armeabi-v7a APK (Shield = 32-bit userspace) on MegaServer.
# Steps follow docs/README.Android.md. Re-runs are incremental; delete $WORK to start clean.
# Usage: tools/mega/build-android.sh [depends|kodi|apk|symbols|all]   (default: all)
set -euo pipefail

HOST=arm-linux-androideabi                                   # 32-bit ARM target
SDK=$HOME/Android/Sdk                                        # sdkmanager root
NDK=$SDK/ndk/21.4.7075529                                    # NDK version README.Android.md asks for
WORK=$HOME/kodi-mega-build                                   # tarballs, depends prefix, cmake build dir
SIGNING=$HOME/.config/kodimega/signing.properties            # keystore path + passwords, never in git
JOBS=$(getconf _NPROCESSORS_ONLN)

SRC=$(cd "$(dirname "$0")/../.." && pwd)
step=${1:-all}
mkdir -p "$WORK/tarballs" "$WORK/depends" "$WORK/build"

depends() {
  cd "$SRC/tools/depends"
  [ -x configure ] || ./bootstrap
  ./configure --with-tarballs="$WORK/tarballs" --host=$HOST \
    --with-sdk-path="$SDK" --with-ndk-path="$NDK" --prefix="$WORK/depends" --disable-debug  # release, like the Play Store build
  make -j"$JOBS"
}

kodi() {
  make -C "$SRC/tools/depends/target/cmakebuildsys" BUILD_DIR="$WORK/build"
  make -C "$WORK/build" -j"$JOBS"
}

apk() {
  prop() { grep "^$1=" "$SIGNING" | cut -d= -f2-; }
  export KODI_ANDROID_STORE_FILE=$(prop storeFile)
  export KODI_ANDROID_STORE_PASSWORD=$(prop storePassword)
  export KODI_ANDROID_KEY_ALIAS=$(prop keyAlias)
  export KODI_ANDROID_KEY_PASSWORD=$(prop keyPassword)
  make -C "$WORK/build" apk
  ls -l "$WORK/build"/*.apk "$SRC"/*.apk 2>/dev/null || true
  symbols
}

# Keep the unstripped libkodi.so per commit, so Shield crash logs can be read later:
#   adb logcat -d -b crash | $NDK/ndk-stack -sym $WORK/symbols/<commit>
symbols() {
  local rev dir
  rev=$(git -C "$SRC" rev-parse --short HEAD)
  git -C "$SRC" diff --quiet HEAD -- xbmc || rev="$rev-dirty"
  dir="$WORK/symbols/$rev"
  mkdir -p "$dir"
  cp "$WORK/build/libkodi.so" "$dir/"
  sha256sum "$SRC"/kodiapp-armeabi-v7a-*.apk > "$dir/apk.sha256"
  echo "symbols: $dir"
}

case $step in
  depends) depends ;;
  kodi) kodi ;;
  apk) apk ;;
  symbols) symbols ;;
  all) depends; kodi; apk ;;
  *) echo "usage: $0 [depends|kodi|apk|symbols|all]" >&2; exit 2 ;;
esac
