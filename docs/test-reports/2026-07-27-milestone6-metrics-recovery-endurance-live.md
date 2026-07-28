# Milestone 6 live metrics, recovery, media, and endurance evidence

Date: 2026-07-27

## Scope and result

The Milestone 6 video-only recorder acceptance work passed on the declared
exact Pi.  It covered the installed recorder's truthful runtime metrics,
bounded camera/encoder recovery, ten consecutive normal clips, and a
two-hour continuous endurance run.  This is target evidence, not a claim that
the optional audio path is integrated: the USB microphone remained connected,
but the recorder path exercised here was intentionally video-only.  Audio A/V
integration, synchronization, and hot-unplug recovery remain Milestone 7
work.

`dashcam-network-fallback.service` remained inactive throughout; no AP
activation was attempted.

The raw target evidence is intentionally ignored by Git and retained under
[`artifacts/pi-m6-20260727/`](../../artifacts/pi-m6-20260727/).  The SHA-256
values below bind each result to its retained JSON.

## Hash-closed deployment and idempotency

The authoritative deployment installed release
`0.1.0.dev0-8320b1f190f2cbb4` from the reviewed hash-closed bundle:

- bundle archive SHA-256:
  `63e96308e67fc595fe2f79d601faae5d9494ca0ca546f34abba813f5debdb4a3`
- bundle manifest SHA-256:
  `e15558317daf255c3a9d5b35b7355ac795b6f66fc183cb8fdfab3d62177d58f7`
- application wheel SHA-256:
  `13f6b44ca8358a973dd5aac65134d42ed556cac7bb3bb04218335cbdbdf14923`
- authoritative saved plan SHA-256:
  `bc39768176c3c8022d5045f67d94cb70c60957f967700018350aa2912a8681c7`
- authoritative apply SHA-256:
  `7f055e5ca37a18b6abe27a561c5e7ad944e9dff066e1e14bf01ff74db525d7fc`
- idempotent saved plan/apply SHA-256:
  `d8a349188a74e331cf58c3e7f9518a352a4f923a4fe8b53d1df8de5494f1656c` /
  `22c7f5110e45e36aecf4325f05f81f8ec799060197fcfe87629e37f764787d7c`

The authoritative dry run and idempotent dry-run/apply evidence reported zero
APT download/install bytes, no package work, and no recorder service start.
APT indexes were refreshed before the authoritative plan, never between that
saved plan and apply, and were not refreshed for the idempotent proof.

## Recorder implementation and local assurance

The installed release publishes an atomic bounded runtime snapshot: configured
and negotiated video settings; raw, encoded, and dropped-frame counters with
their source; latest-clip duration/bitrate/counters; pipeline restart count;
and closed storage-preflight mount shape.  Recoverable camera/encoder faults
receive at most three replacement attempts with 1, 2, and 4 second backoff.
Each attempt re-runs storage gating, uses a fresh backend/sequence, releases
the failed camera owner, and becomes terminal after the bounded budget.  A
stable finalized clip resets the consecutive-recovery budget.

Local tests cover truthful counters, no-progress/watchdog reporting, recovery
ordering and exhaustion, stop during backoff, replacement storage refusal,
cleanup/observer failures, and camera-owner release.  The exact-Pi harnesses
are hash closed and exercise the installed release's real daemon, hardware
backend, preflight, finalizer, catalog, and `/srv/dashcam` exFAT volume as
user `dashcam`.

## Exact-Pi injected recovery

The recovery harness manifest SHA-256 was
`fa1cde80e23077787396749bacfa87639c6c5c1c2ef82ee5d38367cef7b79f0f`.
Its passed JSON is
[`dashcam-m6-recovery-v4.json`](../../artifacts/pi-m6-20260727/dashcam-m6-recovery-v4.json),
SHA-256
`610258c6e41a6c2b70cfb1d65b37055f62b960c6cdff1bf3b812a951dc95a407`.

The one wrapped first backend injected a single recoverable fault after
5.0457451580005 seconds and 151 encoded frames (minimums: 5.0 seconds and
150 frames).  The replacement backend reached 151 encoded frames.  There was
exactly one pipeline restart, and the required recovery transition appeared:

```text
FAULTED -> STARTING -> RECORDING
```

Both generated pairs (sequences 19 and 20) were independently decodable,
H.264, first-keyframe and first-IDR-started, reconciled `FINALIZED` catalog
pairs, and had no generated pending members.  The scenario finished in
64.446048595 seconds against its 119-second bound, left zero catalog pending
intents, released the camera owner, and stopped cleanly.

The final recovery snapshot was 1920x1080 H.264 at 30/1, NV12, High/4.1,
8,000,000 bit/s target, GOP 30, on hardware `v4l2h264enc` at `/dev/video11`.
It reported zero dropped frames, one pipeline restart, and a ready read/write
exFAT `DASHCAM` mount at `/srv/dashcam`.

## Safely refused diagnostic attempts

The earlier recovery harness attempts are retained only as fail-closed
diagnostic provenance; they do not invalidate the passed `v4` run above.

- `v1` followed the installed Python interpreter symlink when checking bundle
  provenance and refused before opening the camera.
- `v2` used an obsolete replacement-frame predicate, waited its bounded
  96 seconds without a diagnostic result, and then failed safely; cleanup
  succeeded and it created no new media.
- `v3` lost `/run/dashcam` when systemd removed `RuntimeDirectory`, so setup
  failed its proof before the camera was opened.

None changed configuration or target identity, managed services, activated
AP, reformatted storage, deleted media, or overwrote evidence.

## Ten consecutive normal clips and continuity

The final media result is
[`dashcam-m6-ten-clips-v5.json`](../../artifacts/pi-m6-20260727/dashcam-m6-ten-clips-v5.json),
SHA-256
`3cb0a862e1bc4e63d4abe2c3cd671716bcaad7494d6bf0052aaed5b97aeefcf6`.
It passed for ordinary uninterrupted sequences 30 through 39.

All ten clips independently decoded as H.264, began with a keyframe and IDR,
and met the 59.0 to 61.0 second duration requirement.  Observed durations
ranged from 59.022000 to 59.022333 seconds.  The nine normalized boundaries
each had a zero-second delta (within the 1/30-second frame-period limit).
Aggregate measured bitrate was 8,001,474 bit/s, within the accepted 6 to
10 Mb/s range.  Each sidecar reported zero dropped frames.

The preceding media diagnostics are not production-media failures: `v3`
rejected a strict result-schema shape before decode, and `v4` passed decode,
continuity, and bitrate but left IDR indeterminate because its probe output was
bounded to 64 KiB.  `v5` fixed the evidence collection and is the result used
for this acceptance gate.

## Two-hour video-only endurance

The original complete 720-sample run is
[`dashcam-m6-two-hour-v2.json`](../../artifacts/pi-m6-20260727/dashcam-m6-two-hour-v2.json),
SHA-256
`ce3bb587b15678a23028b2f82ff14ff19ecb50a71884e7310cdd7cd37931ad7f`.
It ran for 7,200.01160989 seconds.  Its first analyzer incorrectly required
zero absolute swap use and therefore reported diagnostic failure despite the
system's zram-only policy.

The strict hash-bound reanalysis is
[`dashcam-m6-two-hour-reanalysis-v3.json`](../../artifacts/pi-m6-20260727/dashcam-m6-two-hour-reanalysis-v3.json),
SHA-256
`4f0857f8106efcad691938628dbe5e1fc0b94a439683c2ea915a09852bc905e6`.
Its final acceptance-harness manifest SHA-256 was
`2854a7e48b2607b6bdf6ccfad048d46d8ec4641ee967cc370380d9bade49cdb4`.
The reanalysis verified the original source hash, all 720 samples, and the
current `/proc/swaps` policy: the only swap device is `/dev/zram0`, not
SD-card or file-backed swap.  It allows the initial zram baseline but rejects
growth and non-zram swap.

| Measurement | Result | Acceptance outcome |
| --- | ---: | --- |
| Clip advance | 122 | pass (minimum 118) |
| RSS growth | 1,417,216 bytes | pass (maximum 33,554,432) |
| Minimum available memory | 188,833,792 bytes | pass (minimum 33,554,432) |
| Maximum CPU | 60.5000326% | pass (maximum 100%) |
| Maximum temperature | 54.23 C | pass (maximum 80 C) |
| Throttling / undervoltage | 0 / 0 | pass |
| Dropped frames / restarts | 0 / 0 | pass |
| Average bitrate | 8,005,271.525 bit/s | pass (6 to 10 Mb/s) |
| zram baseline / maximum / final | 11,145,216 / 11,145,216 / 7,868,416 bytes | pass (zero growth) |

The maximum raw-to-encoded frame-counter difference was one frame, within the
accepted bound.  Every sample retained positive exFAT free space.  The
reanalysis passed.

## Final target state

After the acceptance runs, the recorder was inactive with
`Result=success`, exit status 0, and zero systemd restarts.  AP fallback was
inactive; there were no camera users and no throttle flags.  The catalog held
142 `FINALIZED` rows, zero intents, and zero managed unreconciled pairs.
Diagnostic pending outputs for sequences 0 through 15 remain preserved and
are not catalogued production clips.  Root free space was 2,966,614,016 bytes
and exFAT free space was 15,819,210,752 bytes.
