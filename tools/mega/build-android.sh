#!/usr/bin/env bash
# Kodi Mega: build the armeabi-v7a APK (Shield = 32-bit userspace) on MegaServer.
# Steps follow docs/README.Android.md. Re-runs are incremental; delete $WORK to start clean.
# Usage: tools/mega/build-android.sh [depends|kodi|apk|all]   (default: all)
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
    --with-sdk-path="$SDK" --with-ndk-path="$NDK" --prefix="$WORK/depends"
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
}

case $step in
  depends) depends ;;
  kodi) kodi ;;
  apk) apk ;;
  all) depends; kodi; apk ;;
  *) echo "usage: $0 [depends|kodi|apk|all]" >&2; exit 2 ;;
esac
