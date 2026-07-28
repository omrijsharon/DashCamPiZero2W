# Milestone 7 unscheduled force-key capability harness

This hash-closed, exact-Pi diagnostic answers one narrow question: can the
installed IMX219 -> NV12 -> `v4l2h264enc` hardware-H.264 graph honour repeated,
unscheduled upstream `GstForceKeyUnit` requests without interrupting camera or
encoder flow?  It is a capability experiment, not a production-code change and
does not enable microphone restoration or change `dashcamd`.

The harness first requires the verified production exFAT mount, and requires
`dashcamd.service` to be exactly inactive/dead with MainPID 0.  It then creates
one bounded diagnostic pipeline on the exact production 1920x1080/30 NV12,
High/4.1, constrained-VBR, 8 Mb/s, repeated-header hardware path.  A private
quarantine child receives a small bounded set of video-only MP4s; production
pending, clips, catalog, and sidecars are never touched.  It neither starts,
stops, nor restarts any service, and does not inspect or alter USB/audio/sysfs
state.

At three deliberately different offsets within the ordinary one-second GOP,
the harness uses the official `GstVideo.video_event_new_upstream_force_key_unit`
helper and sends the event upstream from the hardware encoder source pad. This follows
GStreamer's application-facing `Gst.Pad.send_event` direction contract: a
source pad accepts an upstream event. Sending directly to the encoder avoids
spending the product skew margin on parser-side request forwarding. A probe at
the encoded parser output must observe the corresponding downstream
force-key-unit event with the same unique count and `all_headers=true`,
followed by a non-delta H.264 buffer. The hardware encoder may assign a new
downstream seqnum, so the harness records both seqnums and whether they match
but does not use seqnum as the correlation key. It records wall-clock
request-to-event, request-to-IDR, and
event-to-IDR latency plus the encoded-media PTS distance from the last
pre-request frame to the forced IDR, exact source/object identity, PTS
continuity, and camera/encoder PLAYING state. Each request must produce its IDR
strictly below 100 ms in media time. This is only the hardware capability
ceiling; it supplies no assumed margin. Production must separately prove that
forced-IDR PTS minus the last AAC access-unit end is also strictly below
100 ms. Wall-clock callback latency remains diagnostic because it includes
userspace scheduling. It refuses success on a warning/error/QoS, clock/base-time
change, restart, unbounded latency, missing event/IDR, PTS gap, bad cleanup,
non-IDR-started output, hardware-decode error, or any throttle flag.

`passed=true` proves only that the target stack supported this bounded
force-key capability.  It does **not** by itself prove a safe restoration
handoff, remove the production default-off restoration gate, or complete
Milestone 7.

## Review and run

Copy the directory without modification and verify its closed manifest:

```sh
sha256sum -c SHA256SUMS
MANIFEST_SHA256=$(sha256sum SHA256SUMS | cut -d' ' -f1)
PYTHON=/opt/dashcam/current/venv/bin/python

sudo -u dashcam "$PYTHON" run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  run-experiment \
  --output-directory /srv/dashcam/quarantine/m7-forcekey-20260728a \
  --output /tmp/m7-forcekey-20260728a.json
```

The output must be one new rootfs JSON file in a directory writable by the
unprivileged `dashcam` account, not a file under `/srv/dashcam`. Copy the
finished `/tmp` result into the retained evidence set before removing it.
Retain the result SHA-256, manifest SHA-256, system state before/after, and the
new quarantine directory with the review evidence.
