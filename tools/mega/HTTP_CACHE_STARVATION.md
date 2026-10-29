# HTTP circular-cache starvation: RED regression, not a fix

Manual command: `python3 tools/mega/tests/test_http_cache_starvation.py`.
Native C++17 harness compiles the verbatim production `CFileCache::Read` and
`CCircularCache::WaitForData` bodies. Only scheduling, logging, locks and cache
producer state are replaced. The virtual clock avoids real ten-second waits.

## Verified on b651fff000

Six new tests: true EOF and explicit cache error PASS; 40-second starvation,
60-second HTTP patience expiry, pre-read cancellation and during-wait cancellation
FAIL. All four failures return zero after 10000 virtual milliseconds while EOF is
false. Full discovery: 55 tests, 4 expected failures; existing 49 tests pass.
Cancellation tests define an unwired contract, not proof of a working Abort path.
The explicit cache error test is NOT a producer/source-error test. Source failure,
reopen/reset, real HTTP, Matroska packet integrity and device Stop remain untested.

## Exact causal trace at this revision

* `xbmc/filesystem/CircularCache.cpp:182-202`: WaitForData computes availability,
  waits to deadline, then returns availability (zero), even if !IsEndOfInput.
* `xbmc/filesystem/FileCache.cpp:469-513`: ReadFromCache WOULD_BLOCK triggers
  WaitForData(1, 10s); returned zero bypasses CACHE_RC_TIMEOUT and returns EOF.
* `xbmc/filesystem/File.cpp:593-668`: CFile Read passes the zero to its caller.
* `xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamFile.cpp:90-103`: zero
  sets m_eof; demux dvd_file_read maps zero to AVERROR_EOF. Parent live evidence
  confirms premature Matroska termination during an open 40-second HTTP gap.

## Safety blocker and next required change

Extending the wait alone is not safe. `VideoPlayer.cpp:709-715` aborts demux and
input, but `DVDInputStream.h:173` defaults Abort to a no-op and
CDVDInputStreamFile does not override it. FileCache Read holds m_sync while
waiting. FileCache Close calls StopThread before taking that lock and closing the
source (`FileCache.cpp:578-605`). The producer can still be blocked in curl.
`CurlFile.cpp:1038-1043` public Cancel sets a plain bool and waits for m_opened to
clear: calling it synchronously before Close can deadlock. FillBuffer checks that
bool at line 1691, but its select wait at 1833-1867 is not bounded to a cancellation
slice. An atomic, lifetime-safe, nonblocking cancellation route from input Abort
through cache to producer/curl, including Close and reopen ordering, is required;
no such route was implemented or claimed safe in this bounded task.

Also `FileCache.cpp:342-387` marks EndOfInput for both terminal source errors and
true EOF. A fix must preserve that distinction, not merely retry a zero returned
from WaitForData. Do not expose transient starvation errors to FFmpeg. Scope HTTP
waiting only; retain local/SMB behavior and seek/status WaitForData callers.

## Issue lookup and status

GitHub searches (including closed issues): `repo:xbmc/xbmc CircularCache`,
`repo:xbmc/xbmc "WaitForData" "EOF"`, and
`repo:Megarushing/kodi WaitForData`. The latter returned no entries. Upstream
results included #29287 (open, cache duration sizing), #28889 (closed, seek past
end), #17081 (closed, small-file caching), and #5708 (closed, double file cache).
Titles do not establish a matching fix; no upstream patch was adopted.

No production changes, FFmpeg/dependency changes, build, deployment or device
settings changes. An ARM64 build of this regression-only commit would not fix the
known failure, so none was started. Existing deployed rollback remains owned by
the parent. Remove this test and README link to discard the regression artifact.
