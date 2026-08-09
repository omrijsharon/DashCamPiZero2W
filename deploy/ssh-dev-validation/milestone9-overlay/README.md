# Milestone 9 exact-Pi functional overlay harness

This hash-closed harness qualifies the installed accepted release
`0.1.0.dev0-5f95dd806342ac9e` on the declared Raspberry Pi Zero 2 W. It runs the
production `dashcam.daemon` in a non-restarting temporary systemd unit while
the ordinary `dashcamd.service` and `dashcam-network-fallback.service` remain
exactly inactive. A PTY supplies bounded, checksum-valid, zero-coordinate RMC
input; the real IMX219, native NV12/DMABUF overlay, hardware H.264 encoder,
optional production USB-audio path, exact exFAT volume, finalizer, and catalog
remain in use.

The release marker must contain manifest SHA-256
`619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655`,
and `/etc/dashcam/config.toml` must match the accepted SHA-256
`1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`.

The bounded scenario dwells for at least two 0.5-second overlay update periods
in each state and records two consecutive normal one-minute clips. It holds
the stale state across their boundary, then recovers in the successor. It proves:

- unsynced startup burns `TIME UNSYNCED` and invalid navigation;
- a valid synthetic anchor produces changing local time and current navigation;
- silence beyond the configured two-second limit burns `GPS LOST` while the
  current-navigation fields are hidden;
- valid input restores navigation without a camera/pipeline restart;
- the native renderer remains active and advances during every phase, while
  changing-time phases also advance overlay updates, and reports no contract,
  transform, synchronization, or update failure;
- both normal clips remain within the product's closed 59–61-second duration
  range, and bounded `ffprobe -count_packets` proves at least 29.9 actual video
  packets per media second with no pre-analysis drop/restart increase; the Pi
  remains unthrottled;
- the two canonical sidecars are exactly adjacent half-open intervals with no
  overlapping telemetry ownership; valid and recovered dwell intervals each
  own at least one exact zero-location/zero-speed synthetic sample while stale
  dwell intervals own none;
- each sampled frame's actual decoded PTS maps into one canonical sidecar's half-open monotonic
  window, and both the overlay template and sidecar civil timestamps are
  derived from that sidecar's stable GPS anchor plus monotonic deltas. This is
  evidence for a shared GPS producer and stable-anchor time model, not a claim
  that overlay and finalization retain the same Python snapshot object.

Actual burn-in is checked on the Pi. Bounded `ffprobe` reads the first video
frame PTS. `ffmpeg -copyts` decodes one bounded frame for each phase, uses
`showinfo` to report the actual selected PTS, and emits only the fixed
`x=40,y=40,w=1152,h=64` luma crop in memory. The harness refuses a first PTS
beyond 40 ms or a selected PTS that differs from the sidecar-monotonic target
by more than one 30-fps frame plus 5 ms. It then compares a frozen
128-threshold mask against bitmaps made
by the installed production `render_luma_bitmap`, requires F1 >= 0.88, and
requires the correct template to beat the relevant wrong-state template by at
least 0.08. Timestamped templates allow only a closed +/-2-second projection
window. Crops and media are never written to a temporary file.

After both canonical sidecars exist, the harness captures its final live
zero-drop/restart and healthy-renderer status, then cleanly stops the temporary
recorder and requires systemd `Result=success`, exit status zero, no restart,
and an inactive/dead unit. Only then does it run `ffprobe`, the five software
decodes, and media hashing, so analysis cannot compete with the live recorder
and manufacture later shutdown-fragment drops. Functional FPS is derived only
from bounded `ffprobe -count_packets` divided by the probed video-stream
duration. The sidecar's asynchronous `frames_written` observer value is
retained as a privacy-safe diagnostic alongside its signed delta from the
packet count; equality is reported truthfully but is neither assumed nor used
as the FPS source.

Privacy and cleanup

- Raw NMEA, rendered text, latitude/longitude, sidecar samples, decoded crops,
  frames, and media are never placed in the result. Only state labels,
  booleans, bounded counters, hashes, similarity scores, and stable clip UUIDs
  are retained. Product MP4/JSON pairs stay on Pi exFAT; do not copy raw
  sidecars or media to Windows.
  In short: do not copy raw sidecars or media to Windows.
- A nonblocking kernel lock is acquired before live work. The harness refuses
  the wrong board, release, installed-release journal, exFAT identity, active
  ordinary recorder/AP fallback, competing clock owner, changed production
  config, initial throttling, or pre-existing transient path.
- It performs no partition, format, mount, package, network, AP, microphone,
  physical GPS, or wall-clock mutation. Normal production media/catalog writes
  are preserved.
- The transient unit carries the production service's exact
  `StateDirectory=dashcam` and `StateDirectoryMode=0750` directives. This lets
  systemd establish `/var/lib/dashcam` ownership before `ExecStart`, which is
  required for bounded SQLite journal/WAL creation; catalog file ownership
  alone is insufficient when the parent directory is not writable.
- `finally` stops the transient unit and removes only its exact unit, `/run`
  config, PTY link, descriptors, and status runtime directory. It then proves
  the production config hash, ordinary service states, and throttle state.
- If the transient daemon fails during systemd startup, the result retains a
  bounded privacy-safe lifecycle state/reason/detail from its status file, or
  explicitly records that no status file existed, before cleanup removes it.
- Every poll, command, read, directory scan, decode, sentence count, stop, and
  the complete scenario has a hard bound; the two-clip scenario is capped at
  175 seconds. Any ambiguity fails closed.
- A clip-directory observation may contain at most 66 new canonical sidecars:
  the product's bounded 64-UUID reconciliation backlog plus exactly two
  scenario-created clips. Selection still requires the sidecar's exact
  half-open monotonic interval to cover the requested phase and exact catalog
  UUID/content equality; unrelated reconciled backlog entries cannot satisfy a
  scenario wait.

Run from an unchanged reviewed directory on the Pi:

```sh
cd /path/to/milestone9-overlay
MANIFEST_SHA256="$(sha256sum SHA256SUMS | cut -d' ' -f1)"
sudo /opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --expected-release 0.1.0.dev0-5f95dd806342ac9e \
  --expected-board-serial 00000000db28ffe4 \
  --expected-storage-uuid 7EED-3EA7 \
  --output /var/lib/dashcam/m9-overlay-functional-result.json
```

The output must be a new file in an existing real rootfs directory. A
`passed=true` result is evidence only for the exact declared Pi/image/release.
It does not run or replace the separate Section C1 paired ten-clip resource
matrix.
