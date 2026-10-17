# Kodi Mega

Megarushing's personal Kodi fork, for the bedroom NVIDIA Shield (Android 11, 32-bit userspace).
It installs next to the Play Store Kodi and does not replace it.

- Base: upstream tag `21.2-Omega`. Branch: `mega`. Upstream updates: `git fetch upstream tag <tag>` + `git merge`.
- Package: `org.xbmc.kodimega`, launcher label "Kodi Mega". `APP_NAME` stays `Kodi`, so the data folder is still `.kodi`
  (`/sdcard/Android/data/org.xbmc.kodimega/files/.kodi`).
- Not an upstream PR. Every local change is listed below and has a `LOCAL PATCH (megarushing)` comment.

## Local patches

| File | Change | Why |
|---|---|---|
| `xbmc/platform/android/MemUtils.cpp` | `AlignedMalloc` returns `nullptr` when `posix_memalign` fails | Fixes the Arctic Fuse image crash (SIGABRT scudo in `CTexture`). Upstream [xbmc/xbmc#29091](https://github.com/xbmc/xbmc/issues/29091). |
| `xbmc/guilib/Texture.cpp` | `LoadPaletted` returns `false` when allocation failed | Same failure would otherwise write through a null pointer. |
| `version.txt` | `APP_PACKAGE org.xbmc.kodimega` | Installs next to the Play Store Kodi. A Play-signed app cannot be updated by a self-signed APK. |
| `tools/android/packaging/xbmc/strings.xml.in` | `app_name` = `@APP_NAME@ Mega` | Launcher shows "Kodi Mega". |
| `tools/mega/build-android.sh` | Build script | Reproducible build (below). |

## Build (MegaServer)

Needs: apt packages from `docs/README.Android.md` §2, JDK 17+, Android SDK at `~/Android/Sdk` with
`ndk;21.4.7075529`, `platforms;android-34`, `build-tools;33.0.1`.
Signing key: `~/.config/kodimega/signing.properties` + `kodimega.keystore` (not in git; master copy in 1Password).
Losing the key means the installed app can never be updated in place.

```sh
tmux new -s kodimega 'tools/mega/build-android.sh all 2>&1 | tee ~/kodi-mega-build.log'
# steps can run alone: depends | kodi | apk
```

Output: the signed APK in the repo root (`kodiapp-armeabi-v7a-release.apk`), and the unstripped
`libkodi.so` (with debug info) in `~/kodi-mega-build/symbols/<commit>/`. It is a release build on purpose:
a debug build uses more memory and changes the memory crashes this fork exists to fix.

First full build on MegaServer (4 cores, 2026-10-17): about 1 h 50 min. Later builds only recompile what changed.

## Read a crash

```sh
adb logcat -d -b crash > crash.txt
~/Android/Sdk/ndk/21.4.7075529/ndk-stack -sym ~/kodi-mega-build/symbols/<commit> -dump crash.txt
```

## Install on the Shield

```sh
adb connect <shield>:5555
adb install -r kodiapp-armeabi-v7a-*.apk
```

First install only: copy the data of the old Kodi into the new package folder
(`/sdcard/Android/data/org.xbmc.kodi/files/.kodi` → `/sdcard/Android/data/org.xbmc.kodimega/files/.kodi`), with both apps stopped.
