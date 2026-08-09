# Milestone 9 functional overlay qualification (2026-08-09)

## Scope and identity

This report closes only the three Milestone 9 functional gates: truthful
unsynced/stale rendering, overlay-to-sidecar time-model agreement, and
clip-boundary/GPS-loss/recovery/changing-text stress. It does not run or replace
the Section C1 paired ten-clip resource matrix, and it does not close the
Milestone 9 exit gate.

The target was the verified Raspberry Pi Zero 2 W at `192.168.68.112`, Wi-Fi
MAC `2c:cf:67:98:4c:49`, board serial `00000000db28ffe4`, using the pinned SSH
host key. The installed accepted release was
`0.1.0.dev0-5f95dd806342ac9e`, release-manifest SHA-256
`619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655`,
managed-config SHA-256
`1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`,
and verified exFAT `DASHCAM` UUID `7EED-3EA7` at `/srv/dashcam`.

The hash-closed harness manifest SHA-256 was
`e38b54ea71268f1cd82a50b1a2ef85891ac68c9a5124599e2d37ef2bd88f4ff5`.
Its `README.md` and `run.py` member hashes were
`b79fe538e54a48f81856ce1cd5f4719ba1ff228614996348d82c94cb8d35d96e`
and
`855da738bdb54a71222fbc453e7cae9a0d3b45f27ed4d68213b006ae3843d5aa`.
The final privacy-safe result is retained only on the Pi at
`/var/lib/dashcam/m9-overlay-functional-result-e38b54ea.json`; its SHA-256 is
`3f60f366555869c913be66e5535f22b9c62d69923beeb0abf15dc12cb025ef97`.

## Method

The ordinary recorder and AP fallback remained inactive. A non-restarting
transient systemd unit launched the exact installed production wheel with the
real IMX219, native NV12/DMABUF renderer, hardware H.264 encoder, configured USB
audio path, exFAT finalizer/catalog, and a bounded PTY GPS source. The source
emitted checksum-valid synthetic zero-coordinate/zero-speed RMC input; no
physical GPS claim follows from this functional integration test.

The scenario drove startup unsynced, valid lock, stale before the first
boundary, stale after the boundary, and recovered valid state in the successor.
It required live renderer/update progress and zero drop/restart/failure counters
at every dwell. After both canonical sidecars existed, it captured the final
live status and cleanly stopped the transient recorder. Only after that stop did
bounded Pi-local `ffprobe`/`ffmpeg` inspect packet timing and decode five exact
`x=40,y=40,w=1152,h=64` luma crops. No raw NMEA, coordinates, sidecar samples,
pixels, frames, or media were copied to Windows or retained in the result.

Each decoded frame supplied an actual selected PTS. The harness required the
first video PTS within 40 ms, selected-target error within 38.333333 ms, and the
mapped monotonic instant inside the relevant canonical sidecar and phase dwell.
It compared a frozen threshold-128 glyph mask to the exact installed
`render_luma_bitmap()` output, required F1 at least 0.88, and required a margin
of at least 0.08 over a deliberately wrong state.

## Accepted result

All five stored-pixel classifications passed with F1 **1.0**. Wrong-state
margins ranged from **0.231788** to **0.6097**. Actual PTS mapping errors ranged
from **0.051113 ms** to **32.368196 ms**, below the fixed 38.333333 ms ceiling.
The decoded states were:

- `TIME UNSYNCED` with invalid navigation at startup;
- valid GPS time/navigation;
- `GPS LOST` with current navigation hidden before the boundary;
- `GPS LOST` with current navigation hidden after the boundary; and
- recovered valid GPS time/navigation in the successor.

Canonical sequences 46 and 47 had a zero-nanosecond half-open boundary delta,
consecutive sequence numbers, and zero telemetry-sample ownership overlap.
Both sidecars were `GPS_ANCHORED`. Start/end/sample UTC projection errors were
at most **985 microseconds**. The valid-lock and recovery dwells each owned one
expected synthetic sample; unsynced and both stale dwells owned none. Every
selected frame PTS mapped into the correct sidecar window.

This proves that overlay and sidecar finalization consume the same GPS producer
and stable-anchor/monotonic time model. They intentionally consume different
temporal views, so this is not evidence that they retain the same Python
`GpsSnapshot` object.

The two clips had independently probed media durations **59.988667 s** and
**59.022333 s**, packet counts **1800** and **1771**, and actual packet rates
**30.005668 fps** and **30.005591 fps**. Live encoded frames advanced from 31
to 3,608. Dropped frames remained `0 -> 0`; pipeline and service restarts,
renderer contract/transform/synchronization/update failures, undervoltage, and
throttling remained zero. The transient unit stopped with result `success`,
exit status zero, main PID zero, and no restart before offline analysis.

## Frame-observer diagnostic

The canonical sidecars' asynchronous `video.frames_written` observations did
not equal the later MP4 packet counts. Sequence 46 reported 1,799 versus 1,800
actual packets; sequence 47 reported 1,799 versus 1,771 actual packets. The
harness disclosed both signed differences and derived functional FPS only from
bounded `ffprobe` packet count divided by media duration.

This observation does not invalidate the overlay state, GPS time-model, or
boundary/recovery gates: live drops/restarts were zero and the actual media
rates passed. It must nevertheless remain visible. Do not claim that the
sidecar observer counter equals container packet count until its semantics or
observation timing are separately resolved, and never use it to waive Section
C1.

## Harness corrections and cleanup

Earlier bounded runs failed closed and are not acceptance evidence. They
exposed a missing transient `StateDirectory`, an observer bound smaller than
the product's 64-entry reconciliation backlog, a too-narrow duration check, and
offline software decoding performed before recorder stop. The final harness
restored the production `StateDirectory=dashcam` semantics, used the bounded
64-backlog-plus-two-scenario observer contract, applied the product's closed
59-61 second duration gate, used actual packet timing, and moved all offline
analysis after a strictly verified clean stop.

After the accepted run, the transient unit and exact runtime config/GPS/status
paths were absent. The ordinary recorder and AP fallback were still inactive;
the production config hash was unchanged; `/var/lib/dashcam` and its catalog
had production `dashcam:dashcam` ownership; and the Pi reported
`throttled=0x0`. Product media and all failed/pass result files remain on the Pi
and were not deleted.

## Decision

The Milestone 9 functional state-rendering, shared-producer/stable-anchor model,
and boundary/loss/recovery/changing-text checklist items pass. Milestone 9 as a
whole remains open because the prespecified Section C1 paired ten-clip resource
matrix and exit gate have not passed.
