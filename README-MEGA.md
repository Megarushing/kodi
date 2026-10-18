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
| `xbmc/platform/android/activity/XBMCApp.cpp` (`onNewIntent`) | Ignore intents while Kodi is not initialized | Null logger SIGSEGV (fault addr 0x14), 2 crashes. |
| `xbmc/platform/android/activity/JNIXBMCJsonHandler.cpp` | `_requestJSON` answers "not running" when the logger is down | Java `XBMCJsonRPC` (TV-channel sync job) called JSON-RPC with Kodi stopped: SIGSEGV in `CLog::CanLogComponent`, 2 crashes. |
| `XBMCApp.cpp` (`onStart`, `run`) | Restart a finished run thread instead of assigning over it | `run()` gives up after 30 s without a window and leaves an unjoined thread; next `onStart` -> `std::terminate`, 4+ crashes. |
| `xbmc/dialogs/GUIDialogBusy.cpp` | Nested busy wait reuses the open dialog | Upstream throws `std::logic_error` ("known issue, the application will exit"), 1 crash during nested plugin resolve. |
| `XBMCApp.cpp` (`MemoryGuardProcess`) + `GUILargeTextureManager` stats | **Memory guard**: every 2 s reads MemAvailable, SwapFree, VmSize; under pressure frees unused images/textures at once and purges the heap (`mallopt(M_PURGE)`); logs `MemoryGuard:` lines with undrawn-image bytes. During video: at most every 30 s, no purge. | Android's low-memory killer killed Kodi with no warning; Kodi ignored memory pressure (`onLowMemory` was a no-op). |
| `xbmc/guilib/GUIControlGroupList.cpp`, `GUIControlGroup`, `GUIBaseContainer` | Rows more than one viewport off screen are not processed or drawn, and their item images are released once (items stay, no refetch) | Kodi processed every row of a long home screen, decoding posters it never drew: 595 undrawn images = 274 MB, then a low-memory kill. After: ~20 MB. |
| `xbmc/guilib/listproviders/DirectoryProvider.cpp`, `IListProvider.h`, `GUIBaseContainer.cpp` | Leaving a window cancels its unfinished widget fetches; non-plugin lists (addons://, library) fetch at NORMAL priority | Fetches of tabs already left kept running; local lists waited behind them (LOW jobs only start while < 3 jobs run). Add-ons hub icons: Play Store Kodi still empty at 40 s, Kodi Mega full at 2 s. |
| `tools/android/packaging/xbmc/src/Splash.java.in` | Unpack completeness = marker file written after the last file | A start-up unpack cut short counted as done (folder mtime) -> "Unable to load settings definitions" + SIGSEGV at every start. |
| `DVDVideoCodecAndroidMediaCodec.cpp` (`SignalEndOfStream`), `XBMCApp.cpp` (`android_printf`) | Log format fixes | `"{}"` without argument / formatted text used as format -> `fmt::format_error` -> terminate when the decoder service dies. Upstream master has the codec fix. |
| `AESinkAUDIOTRACK.cpp/.h` | Float PCM written through one persistent direct `ByteBuffer` | `write(float[])` allocated a Java array + native copy per 32 ms period; that allocation failing aborted Kodi (2 crashes). |
| `xbmc/threads/Thread.cpp` | `CThread::Create` retries 2x, then logs instead of terminating | `std::system_error` from `pthread_create` (no memory for a stack) aborted Kodi. |
| `XBMCApp.cpp` (emergency reserve) | 24 MB address-space reserve + `std::set_new_handler`: a failing `operator new` gets the reserve once; re-armed when memory is fine | Uncaught `std::bad_alloc` aborts. |
| `GUIWindowManager.cpp` (`UnloadHiddenKeptWindows`) + memory guard | During video under critical memory, unload hidden kept-in-memory windows once (properties stay) | Frees menu memory during 4K playback; measured: Kodi RSS 419 -> 392 MB, did NOT remove Remux hitches. |
| `tools/android/packaging/xbmc/AndroidManifest.xml.in` | `<profileable android:shell="true"/>` | Lets perfetto heapprofd profile native memory from adb (no runtime change, unlike debuggable). |
| `tools/mega/build-android.sh` | Build script | Reproducible build (below). |

## Tags

- `mega-v1-imagefix` = the first Kodi Mega (#29091 image fix + build tooling), before the 2026-10-18 crash-fixing night.

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
adb install -r -f kodiapp-armeabi-v7a-*.apk   # -f = internal storage; without it an update moves to the adopted SD card
```

The version string Kodi reports (`Application.GetProperties version.revision`) is set at cmake configure time, so an incremental build keeps the old commit id. Match crash symbols by build id / the `symbols/<commit>` folder printed by the build, not by that string.

## Memory diagnosis tools (bedroom Shield, 2026-10-18)

- `MemoryGuard:` lines in kodi.log: free RAM, swap, address space, RSS, images held / undrawn.
- `script.mega.memdump` add-on (in `kodi-customizations/crash-evidence/2026-10-18-0015-lmk-ram-growth/`): copies `/proc/self/{maps,smaps,status}` from inside Kodi (adb cannot read them); `vsum.py` groups the address space.
- perfetto heapprofd: config + analysis recipe in that evidence folder's README. MegaServer's CPU has no AVX2: use perfetto **v25.0** `trace_processor_shell`; resolve Kodi frames with the NDK `addr2line`/symbol names in the trace.

First install only: copy the data of the old Kodi into the new package folder
(`/sdcard/Android/data/org.xbmc.kodi/files/.kodi` → `/sdcard/Android/data/org.xbmc.kodimega/files/.kodi`), with both apps stopped.
