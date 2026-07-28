# Milestone 6 exact-Pi acceptance harness

This hash-closed harness performs the remaining read-only media validation and
bounded two-hour observation for the video-only recorder. It has three
phases and never starts, stops, restarts, or reconfigures a service. It never
changes networking, NetworkManager, AP state, partitions, filesystems, the
catalog, or recording files.

The only accepted recording root is `/srv/dashcam`. Media validation reads ten
explicit consecutive ordinary MP4+JSON pairs below `clips` and refuses pending,
noncanonical, protected, wrong-device, or incorrectly identified members.
Its metadata/keyframe gate uses the exact-Pi-validated compact first-item
`ffprobe` query plus a separate first-packet NAL/IDR query. Aggregate bitrate
uses another compact query with video-stream bitrate, format bitrate, and
size/duration fallbacks. The exact Trixie compact bitrate response may also
contain only empty `programs` and `stream_groups` arrays; any nonempty or
unknown top-level shape refuses acceptance. The separate first-packet IDR
response is retained under the reviewed 8 MiB JSON bound, because the exact
Pi's packet-data response exceeds the normal 64 KiB command-output bound.
Independent full decode uses FFmpeg's explicitly selected `h264_v4l2m2m`
hardware decoder; a decoder error or timeout remains a hard media failure and
valid bitrate evidence cannot override it. The
collector reads `/run/dashcam/status.json`, `/proc`, CPU temperature,
`vcgencmd get_throttled`, and `statvfs("/srv/dashcam")`. It writes only the
caller-selected new evidence file, normally below `/tmp` or a rootfs evidence
directory. Do not place harness output on `/srv/dashcam`.

## Review and hash closure

Copy this directory without modification and verify it before any phase:

```sh
sha256sum -c SHA256SUMS
sha256sum SHA256SUMS
```

Pass the second command's exact lowercase hash as
`--expected-manifest-sha256`. Run with the installed release interpreter:

```sh
PYTHON=/opt/dashcam/current/venv/bin/python
HARNESS=/path/to/reviewed/milestone6-acceptance/run.py
MANIFEST_SHA256=<reviewed-SHA256SUMS-hash>
```

The harness requires `/usr/bin/ffprobe`, `/usr/bin/ffmpeg`, and
`/usr/bin/vcgencmd`. Every command has a fixed argument vector, retained-output
limit, and timeout.

## Ten consecutive ordinary clips

First let `dashcamd` finalize at least ten uninterrupted ordinary one-minute
clips from the same boot. Select the first exact sequence without including a
short shutdown fragment. The full UUID is the current kernel boot ID:

```sh
"$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  validate-media \
  --boot-id 601693e3-fa96-427e-906b-1621463a15cd \
  --start-sequence 18 \
  --output /tmp/dashcam-m6-ten-clips.json
```

The result passes only if all ten canonical pairs are present on the exact
recording device, have real frame counts, decode independently as H.264, start
with independently inspected IDR packets, last 59–61 seconds, and have all nine
sidecar-monotonic boundaries within one 30 fps frame. Aggregate measured
bitrate must be within ±25% of 8 Mbit/s. Raw per-file PTS values are never used
as a shared cross-file timeline.

## Two-hour endurance

Start the collector only after the recorder has finalized at least one clip, so
`last_clip` bitrate/timing fields are available. Resolve and record the current
`dashcamd` main PID without changing service state, then run:

```sh
"$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  collect-endurance \
  --pid <recorded-dashcamd-main-pid> \
  --output /tmp/dashcam-m6-two-hour.json
```

The production phase is fixed at 720 samples, one every ten seconds, spanning
at least 7,200 seconds. Each sample strictly requires the schema-v1 recorder
snapshot, process RSS/CPU identity, system used and available memory, swap,
temperature, throttle/undervoltage flags, exact-volume free bytes, raw,
encoded and dropped frame counters, most recent clip sequence and
bitrate, and pipeline restart count. PID reuse, mount drift, a non-recording
state, absent/null metrics, malformed data, missing samples, or a short
collection refuses acceptance rather than producing an indeterminate pass.

The analyzer additionally requires zero drop/restart growth, no observed
throttling or undervoltage, bounded RSS growth, adequate available memory,
average bitrate within tolerance, monotonic counters, continued clip progress,
and positive recording-volume free space. `/proc/swaps` must contain exactly
one `/dev/zram0` partition at both ends of a new collection; file, SD-card,
additional, missing, or changed swap policy refuses acceptance. Nonzero zram
occupancy is allowed only when no sample exceeds the first sample's exact
used-byte baseline. There is no growth tolerance. The available-memory and RSS
gates remain mandatory, so zram does not mask memory exhaustion or sustained
growth. Because the raw-input and
encoded-output probes attach independently and the encoder can hold one buffer
in flight, either frame counter may lead the other by exactly one at a sample.
Every sample must keep the absolute raw/encoded difference at zero or one; a
difference of two or more refuses acceptance. This does not relax the separate
zero dropped-frame and zero pipeline-restart growth gates. Save the output and
its reported SHA-256 with the deployment manifest, service logs,
boot/card/mount identity, and final catalog/media evidence.

## Hash-bound endurance reanalysis

`reanalyze-endurance` reads an existing full 720-sample result without changing
it, verifies its exact caller-supplied SHA-256, strictly validates the closed
result/status/sample schemas and every original non-swap gate, reads the current
zram-only policy, and recomputes the corrected no-growth analysis. Its output is
compact: it links the retained source hash and records the policy, swap summary,
corrected checks, and diagnostic checks without copying the 720 samples.

```sh
"$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  reanalyze-endurance \
  --source /path/to/retained-two-hour.json \
  --expected-source-sha256 ce3bb587b15678a23028b2f82ff14ff19ecb50a71884e7310cdd7cd37931ad7f \
  --output /tmp/dashcam-m6-two-hour-reanalysis.json
```

A wrong source hash, malformed or incomplete result, fewer than 720 samples,
shorter than 7,200 seconds, any original non-swap failure, non-zram swap policy,
or any swap growth above the first sample refuses acceptance.
