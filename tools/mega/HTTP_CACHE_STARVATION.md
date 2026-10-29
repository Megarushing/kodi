# HTTP cache starvation and cancellation

Status: **core fix implemented and host regressions green; ARM64/live acceptance pending**. Deployment is authorised only to the living-room Shield for this workstream. No FFmpeg or dependency patch.

## Demonstrated failure

`CCircularCache::WaitForData` returns available bytes (possibly zero) when a timed wait expires, even if input is still open. The old `CFileCache::Read` treated that zero as EOF after ten seconds. `CFile::Read` passed it to `CDVDInputStreamFile`, which set input EOF; the demux callback then returned `AVERROR_EOF` to Matroska. A controlled open HTTP response with a 40-second gap reproduced premature Matroska EOF with `deadline_s=600 expired=false input_eof=true`. Separately, the old demux20s deadline also produces terminal `AVERROR_EXIT`; extending that timer alone did not fix the cache false EOF.

## Scope of the fix

- Only HTTP/HTTPS cache reads wait through temporary starvation, in 100ms slices, up to `max(20 seconds, network/curllowspeedtime)`. This and the demux deadline now share the configured patience. The timeout is finite, not a promise of unlimited buffering.
- Zero is returned only for real end-of-input after draining any final write that races the EOF observation. Cached bytes preceding terminal source errors are delivered; the eventual source failure/premature known-length zero is an error, not a successful EOF. Positive reads reset the next read's patience budget.
- Global `WaitForData` semantics, nonblocking cache status, seeking and nonHTTP ten-second behavior are unchanged. Explicit zero-size read probes remain immediate.
- Internal `IOCTRL_CANCEL_READ=32` is a nonblocking, idempotent terminal-abort request. Video input forwards Abort to cache/source; the cache marks its read aborted and producer stopped before signalling the shared seek event. It never acquires the read lock or joins from that control path.
- Curl cancellation only stores an owned atomic flag, not replaceable seek-state pointers. Each read state borrows that flag from its owning curl file through `SetCommonOptions`; the file survives until the producer joins. Curl polling checks the flag in bounded100ms slices, and source cancellation occurs before cache producer join. The old blocking public `Cancel()` is not called. Reopen/reset clears abort/error/patience as appropriate.
- No new retry/error is exposed to FFmpeg for a temporary empty HTTP cache. True EOF, finite timeout and user cancellation remain distinct. Existing curl error/retry policy is otherwise unchanged.

## Tests

Manual: `python3 tools/mega/tests/test_http_cache_starvation.py` and `python3 tools/mega/tests/test_http_curl_cancellation.py`.

The native C++17 cache harness compiles the actual production `Read` and `WaitForData` bodies, plus reset and terminal-source publication statements. A virtual scheduler exercises40s starvation against60/600s budgets, expiry, genuine/unknown-length EOF, source/premature-zero failure, final-write race, immediate/during-wait cancellation, nonHTTP behavior and reopen. The original six-case test had four RED failures returning false zero after10000ms. A separately added zero-size-read regression also failed before correction.

The curl harness compiles actual production cancel/reset/poll bodies with mocked curl dispatch but real POSIX select/threads. Cases include cancellation during real fd/no-fd polls (observed100ms join), poisoned/replaced state pointers, reset, and source lifecycle ordering. It is not a live-network test or a TSAN proof. The complete suite and real build/device results must be recorded by the deployment owner before claiming playback fixed.

## Operational limits and rollback

With configured600, a failed/abandoned server can cause a long wait unless the user stops; Stop must be validated during the actual stalled read, not only after it recovers. Unknown-length EOF is valid; known-length truncation is terminal error. Socket/DNS/open-timeout behavior is not made universally interruptible by a read-loop fix.

For an unexpected hang/regression, stop only the owned test, retain logs and reinstall the retained target-matching ARM64 rollback using the existing build/install workflow. Parent retained `~/tmp/kodi-http-deadline-20261029/rollback-484a66ac53-arm64.apk`, SHA256 `b57e6b905af2ed316c64a001c0097bf292a3f4944da4c98f748b18acebe9289b`. Source revert alone does not alter installed devices. HTTP patience can be returned to default20 in the living target's advancedsettings with a clean restart; that is a configuration fallback, not reversal of the code. No fleet rollout or bedroom change is authorised by the living-only trial.

## Bounded issue lookup

Searched upstream/fork including closed: `repo:xbmc/xbmc CircularCache`, `repo:xbmc/xbmc "WaitForData" "EOF"`, `repo:Megarushing/kodi WaitForData` (2026-10-29). The fork query returned no entries. Upstream included#29287 (open, cache duration sizing),#28889 (closed, seek past end),#17081 (closed, small-file caching),#5708 (closed, double file cache). None was adopted as a matching fix. These titles are not evidence of equivalence.
