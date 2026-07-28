# Milestone 7 read-only audio/video media validation

This hash-closed harness validates three to ten already-finalized ordinary
audio clips from the exact `/srv/dashcam/clips` device. It never starts,
stops, or reconfigures a service; it never writes media, changes the catalog,
mounts, network state, or AP state. Its sole write is a caller-selected new
JSON evidence file outside `/srv/dashcam`.

Before use, copy this directory intact and verify its manifest:

```sh
sha256sum -c SHA256SUMS
sha256sum SHA256SUMS
```

Pass the exact lowercase hash from the second command as the manifest argument:

```sh
PYTHON=/opt/dashcam/current/venv/bin/python
HARNESS=/path/to/milestone7-audio/run.py
"$PYTHON" "$HARNESS" --expected-manifest-sha256 <sha256-of-SHA256SUMS> \
  validate-media --boot-id <full-kernel-boot-uuid> --start-sequence <n> \
  --count 3 --output /tmp/dashcam-m7-audio.json
```

The harness requires consecutive ordinary MP4+canonical JSON pairs on the
same real recording device, audio sidecars declaring AAC-LC/48 kHz/mono/128
kbps, and no pending member. Per clip it performs a compact fixed-argv
`ffprobe` stream/format query, a separate bounded first-video-packet IDR query,
and an independent full FFmpeg decode using `h264_v4l2m2m` for video while
mapping both video and audio. It never requests all packets or all frames.

Acceptance requires 1080p30 H.264 High, AAC-LC at 48 kHz mono, 59–61 second
containers/streams, 6–10 Mb/s video, AAC source bitrate within 6.4 kb/s of
128 kb/s, maximum start/end stream-edge A/V skew of 100 ms, and sidecar
monotonic boundaries within one 30 fps frame. The fixed tolerance accounts for
container-reported AAC bitrate rounding; the sidecar target remains exactly
128000 bit/s.
