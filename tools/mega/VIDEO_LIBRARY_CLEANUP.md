# Missing TV-show folder cleanup

`CVideoDatabase::CleanDatabase` in `xbmc/video/VideoDatabase.cpp` includes
`tvshowlinkpath`-linked paths even when their hash/content/settings are empty.
Previously those paths were never checked for existence, and their remaining
links prevented removal of the zero-episode show record.

Before removing a linked missing folder, the cleaner resolves its configured
source and requires that source to be accessible. An unavailable or unknown
source retains the link/show. Existing folders and existing explicit keep
choices are preserved. This does not remove a show just because it has no
episodes, alter media files, or introduce a janitor/recurring script.

## Regression command

`python3 tools/mega/tests/test_video_library_cleanup.py -v`

The test executes the actual candidate SQL with SQLite and compiles the actual
C++ path-decision loop with controlled directory/scraper collaborators. It covers
missing empty-hash shows, existing empty shows, unavailable/unknown sources,
hashed show folders, explicit keep choices, and unrelated unlinked paths.
This is a host regression, not proof of SMB network behavior or GUI refresh.

## Canary and rollback

Deploy first to the explicitly approved Android canary. Other platforms and
devices are not implicitly approved by a successful build. Preserve an APK whose
SHA-256 matches the installed canary before rebuilding; use the existing
`megabuild.py` workflow to build/install without uninstalling or clearing data.

Trigger: native manual/background video library cleaning. A source outage or
unknown source deliberately defers removal; a later clean after recovery is
needed. Check `VideoLibrary.GetTVShows` and the exact source/folder before
removing any supposed stale item. Existing empty on-disk folders deliberately
keep their show cards.

A source revert does not change an installed app. To disable this behavior,
install the hash-verified previous signed APK after checking playback is idle;
never reset the library. For a suspected incorrect removal, stop further cleans,
preserve logs/database evidence and restore metadata only from a verified backup
with Kodi stopped. Do not delete missing-source protections as a workaround.
