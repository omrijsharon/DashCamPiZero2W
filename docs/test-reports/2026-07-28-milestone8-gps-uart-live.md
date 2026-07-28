# Milestone 8 GPS UART reader and receiver validation — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Release: `0.1.0.dev0-d72d3067350d3552`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope

This evidence covers the first three Milestone 8 tasks: production UART reader
integration, actual-receiver NMEA/counter validation, and no-GPS startup. It does not
accept GPS UTC anchoring, system-clock ownership, sidecar/filename
reconciliation, or GPS-derived metadata. No coordinates were retained.

## Closed deployment

The hash-closed deployment bundle identified release
`0.1.0.dev0-d72d3067350d3552`. Its bundle manifest hash was
`c79723bbc53ea02f5e1d9075992be8c43a521cf30e3fc27f3f84a1790907ca05`,
`SHA256SUMS` hash was
`38a33096548ce2fd003a15ce2825b2cf5bfecb01bd1bfc25cf46c6b7fcdecd1c`,
and application wheel hash was
`0c40f2ada1d70477ff50c3e7bc037cbe677c954f5fac243cdae69cca02488ab9`.
The authoritative initial plan/apply and byte-idempotent repeat passed with
zero package changes and zero service starts. The ignored machine-readable
evidence is `artifacts/pi-m8-20260728/dashcam-app-{plan,apply}-d72*.json`.

## UART failure investigation and final behavior

Two releases were rejected before the final result:

- Release `bc1` still used the shared asyncio executor for UART descriptor
  lifecycle and configured `VMIN=0`. Its first GPS open never completed under
  the production media load; four watchdog restarts were recorded before the
  fifth attempt was stopped, with about 38 CPU-seconds consumed per 20-second
  watchdog interval.
- Release `959c` treated `b""` as selector-not-ready. The PL011 remained
  perpetually readable, consumed about 150% CPU, and starved status handling,
  although stop was clean.

The accepted Linux adapter opens the character-device UART only with
`O_RDONLY|O_NOCTTY|O_NONBLOCK|O_CLOEXEC`, configures raw 8N1 at 115200 with
software/hardware flow control disabled and `VMIN=1`, `VTIME=0`, and never
writes. Constant-time nonblocking open/configure/restore/close operations do
not use the shared executor. An unexpected `b""` read is a bounded `EIO`
transport failure. Eight consecutive selector-ready reads making no progress
(`EAGAIN`/`EWOULDBLOCK`) are allowed; the ninth fails boundedly and the GPS
supervisor reconnects with its capped backoff. A quiet UART that never becomes
ready still returns the ordinary bounded empty timeout.

The focused local release checks passed: 221 tests, Ruff, and strict MyPy. The
final repository-wide run passed 1,777 tests with 10 documented
platform-inapplicable Windows/POSIX skips.

## Exact-Pi live result

The ordinary production service ran for 72 seconds and stopped cleanly:

- `ExecMainStatus=0`, `NRestarts=0`, and `throttled=0x0`.
- Status remained `RECORDING`; storage was `READY`; H.264 was hardware-backed;
  and the configured USB audio device was `MATCHED`.
- GPS reached `NAVIGATION_VALID`; its latest recognized sentence was GN GGA
  with nine satellites.
- GPS counters recorded 706,122 bytes, 12,807 lines, 1,339 valid
  sentences/fixes, 102 checksum failures, and 6,763 unsupported sentences.
  The counters also recorded one transport error/disconnect/reconnect, which
  recovered without a pipeline restart (`pipeline_restart_count=0`).

The privacy-safe preprobe independently observed GN RMC and GGA and accepted
150 trusted active-RMC anchors. A subsequent 120-second privacy-safe
receive-only comparator observed 1,234,468 bytes; 1,199 GGA and 1,200 active
RMC records; GGA/GLL/GSA/GSV/RMC/VTG sentence traffic; 12 satellites;
1,200 trusted RMC UTC candidates; two checksum failures; and zero malformed
records. It retained no coordinates. Its result SHA-256 is
`191eddb2a8747de4654d73e2730d495dbf1298b073897557d5e84bf155d13e2b`.
This is receiver/parser evidence only, not acceptance of the later product
anchor-integration task.

Finalized A/V clips 381 and 382 were 60.053333 and 12.697667 seconds. Both
contained 1080p H.264 High/4.1 and 48 kHz mono AAC-LC. Their sidecars remain
intentionally `UNSYNCED` with `gps.available=false`: production GPS
observations do not yet feed anchors or sidecars. Older pending historical
fragments predate this run; no new pending fragment existed after 18:39.

## No-GPS startup

The physical receiver remained connected. An isolated transient systemd unit
ran the same installed release with a temporary config referencing the
deliberately nonexistent `/dev/dashcam-gps-deliberately-absent`. The installed
config was not changed. The transient unit preserved the production
user/groups, verified exFAT bind, notify/watchdog contract, and media command.

At 15 seconds recording was active with matched audio, zero media/pipeline
restarts, and an advancing watchdog. GPS truthfully reported
`UART_UNAVAILABLE`: six bounded attempts produced six
`TRANSPORT_UNAVAILABLE` observations, while byte, line, fix, and accepted
sentence counters remained zero.

The run crossed a full clip boundary. Sequence 383 retained:

- stable clip UUID `ca075ef2-7f5d-4621-a85f-b1434e3ea6c1`
- boot UUID `601693e3-fa96-427e-906b-1621463a15cd` and sequence 383
- monotonic interval `210937312077923` to `210997300627266`
- null `start_utc`, `end_utc`, and `start_local`
- `gps_time_state=UNSYNCED` and `timestamp_quality=MONOTONIC_ONLY`
- `gps.available=false` and an empty GPS sample list

The finalized MP4 contains 1080p H.264 High/4.1 and 48 kHz mono AAC-LC.
`ffprobe` reported 60.032 seconds and 8,132,271 bit/s; full Raspberry Pi
`h264_v4l2m2m` hardware decode passed. GPS remained unavailable through nine
bounded attempts without disturbing recording. The transient service stopped
in three seconds with result `success`, status 0, zero service restarts, and
`throttled=0x0`. Its config was removed and ordinary `dashcamd` remained
inactive. The final runtime snapshot observed one cumulative video-frame drop;
it did not trigger a pipeline restart or prevent independent clip decode.

Privacy-safe runtime evidence:

- `artifacts/pi-m8-20260728/dashcam-m8-no-gps-status-15.json`, SHA-256
  `da116fbf7692bc5f025dfd2890f2b2f333f7317dc5823a01403e09b8e086848a`
- `artifacts/pi-m8-20260728/dashcam-m8-no-gps-status-final.json`, SHA-256
  `6dc84155bb2b3dd9123347b353f7be025ffd7d2bfcc793ea68c6fcc61f87a69f`

## Conclusion

The production UART reader is bounded, receive-only, restart-safe, and proven
against the actual receiver's GN RMC/GGA traffic and error counters. This
also proves monotonic-only recording when the configured GPS path is absent.
This checks Milestone 8's first three tasks. All later M8 tasks and the
milestone exit gate remain open.
