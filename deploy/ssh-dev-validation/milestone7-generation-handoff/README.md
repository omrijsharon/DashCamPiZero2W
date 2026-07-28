# Milestone 7 immutable recording-generation capability harness

This hash-closed, exact-Pi harness tests a safer alternative to changing an
`audio_%u` pad on a live `splitmuxsink`. It is an isolated capability
experiment and does not change production code or prove production hot-plug
support.

One parent pipeline owns the exact IMX219/NV12 hardware-H.264 encoder and the
matched USB microphone/AAC encoder for the entire run. Before the parent enters
`PLAYING`, exactly three complete immutable recording generation bins, every
internal splitmux request pad, and every external tee request pad exist.
Standby bins remain locked in `NULL`, their external tee pads remain unlinked,
and their valves remain closed with `drop-mode=forward-sticky-events`; a linked
child is never locked in `NULL`. The bounded sequence is:

1. connected-microphone A/V generation;
2. video-only generation;
3. restored connected-microphone A/V generation.

At each handoff the harness blocks the encoded video stream at an IDR and
blocks the encoded audio stream. While both common inputs are held, it unlocks
and externally links the already-complete successor, synchronizes its state,
closes and externally unlinks the old generation, then opens the successor.
It injects EOS downstream of the closed old gates. The old generation must
report bounded fragment closure. All request pads remain allocated until the
whole parent reaches `NULL`; only then are they released and bins removed. No
live splitmux generation ever gains, loses, or replaces a stream pad.

The run writes only 8–12 short MP4s in one new direct child of
`/srv/dashcam/quarantine` and one exclusive JSON result on rootfs. It never
writes the production pending, clips, sidecar, or catalog paths and never
starts/stops/restarts a service. Acceptance requires the exact per-generation
stream sets, IDR starts, hardware-H.264 decode, unchanged parent identities,
one-frame normalized video continuity, restored A/V skew below 100 ms without
drift, zero bus warnings/errors, zero service restarts, no throttling, and
bounded clean finalization.

Verify and run with the exact installed release:

```sh
sha256sum -c SHA256SUMS
MANIFEST_SHA256=$(sha256sum SHA256SUMS | cut -d' ' -f1)
PYTHON=/opt/dashcam/releases/<release>/venv/bin/python

sudo -u dashcam "$PYTHON" run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  run-experiment \
  --output-directory /srv/dashcam/quarantine/m7-generation-20260727a \
  --output /var/lib/dashcam/m7-generation-20260727a.json
```

Any unknown state, link return, request-pad shape, warning, error, timeout,
foreign path, decode failure, clock/base-time change, or incomplete closure
fails closed. `passed=true` remains capability evidence only:
`safe_to_integrate_production` is deliberately false.
