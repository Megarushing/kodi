#!/usr/bin/env bash
# Kodi Mega: build the Android APK on MegaServer. Default armeabi-v7a (bedroom Shield = 32-bit userspace);
# ABI=arm64-v8a builds the 64-bit APK (living-room Shield). Each ABI needs its OWN source checkout
# (depends is configured inside tools/depends): use a git worktree, e.g. ~/kodi-mega-src-arm64.
# Steps follow docs/README.Android.md. Re-runs are incremental; delete $WORK to start clean.
# Usage: [ABI=arm64-v8a] tools/mega/build-android.sh [depends|kodi|apk|symbols|all]   (default: all)
set -euo pipefail

MEGA_ABI=${ABI:-armeabi-v7a}                                 # armeabi-v7a (32-bit) or arm64-v8a (64-bit)
unset ABI                                                    # never leak: GMP's configure reads $ABI (failed with ABI=arm64-v8a)
case $MEGA_ABI in
  armeabi-v7a) HOST=arm-linux-androideabi; WORK_DEFAULT=$HOME/kodi-mega-build ;;
  arm64-v8a)   HOST=aarch64-linux-android; WORK_DEFAULT=$HOME/kodi-mega-build-arm64 ;;
  *) echo "unknown ABI $MEGA_ABI" >&2; exit 2 ;;
esac
SDK=$HOME/Android/Sdk                                        # sdkmanager root
NDK=$SDK/ndk/28.2.13676358                                  # NDK version README.Android.md asks for (r28c since Kodi 22 Piers)
WORK=${WORK:-$WORK_DEFAULT}                                  # tarballs, depends prefix, cmake build dir
SIGNING=$HOME/.config/kodimega/signing.properties            # keystore path + passwords, never in git
JOBS=$(getconf _NPROCESSORS_ONLN)

SRC=$(cd "$(dirname "$0")/../.." && pwd)
step=${1:-all}
# one ABI per source checkout: refuse to reconfigure a tree that depends was set up for another host
if [ -f "$SRC/tools/depends/Makefile.include" ] && ! grep -q "^HOST=$HOST\b" "$SRC/tools/depends/Makefile.include"; then
  echo "this checkout's depends is configured for $(grep -m1 '^HOST=' "$SRC/tools/depends/Makefile.include") - use a separate worktree for $MEGA_ABI" >&2; exit 2
fi
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
  sha256sum "$SRC"/kodiapp-$MEGA_ABI-*.apk > "$dir/apk.sha256"
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
