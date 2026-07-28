# Milestone 7 USB audio and A/V baseline — 2026-07-27

## Scope and result

The exact reference Pi passed the connected-microphone startup, muxing,
timestamp/resampling, metadata, ten-clip continuity, independent decode, and
A/V-skew slice of Milestone 7. Microphone hot-unplug, restoration, repeated
disconnect/reconnect, and physical wrong-device substitution remain open and
are not claimed by this report.

The accepted run used boot ID
`601693e3-fa96-427e-906b-1621463a15cd`, IMX219 hardware H.264, USB microphone
`08bb:2902`, and the exact exFAT `DASHCAM` volume at `/srv/dashcam`.

## Closed deployment

- Accepted release: `0.1.0.dev0-011a148e085da278`
- Application bundle manifest SHA-256:
  `4b8b52dc6c2561d7e68ed3ceaa911462bfa55b5d4d1d69d4e4b6a368844b97df`
- Locked application wheel SHA-256:
  `35e58088cca428052b2dd4e318dd949cc60eac188f48143adb605a136fe21a93`
- APT refresh stdout/stderr SHA-256:
  `563ba2840a041e4d00846e2851b038921a3c12e194f4dd793187f366e305f122` /
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- Authoritative dry-run SHA-256:
  `5e7fa9d2e42b3b61d068cbb731227284653191db788c4f84de0b308eb0631935`
- Apply SHA-256:
  `0aaf7f2897586c985b8514729bca5fa746f621d19ddd2e1dbad8e9e0d9e2bded`
- Idempotent dry-run/apply SHA-256:
  `3d664df2f531a96e795bdf9f11040298a4fd5688391926fa4663d6bb7ba0ad47` /
  `9e2f68fb02f46062c6616d1aaecbc56bf1a5c5a4190689644ed421beba092e3a`
- Missing/solver packages: zero
- Services started by either apply: none
- The installed recorder unit grants only the explicit `audio`, `video`,
  `render`, and `dashcam-storage` supplementary groups.

The first candidate release failed closed to video-only because the bounded
udev parser incorrectly applied its 128-byte identity-field limit to the real
but unrelated long `DEVLINKS` property. Video continued with zero drops and
restarts and stopped cleanly. The parser was corrected narrowly: total
output/key syntax remain bounded, unknown properties are ignored, and every
identity field used for selection remains strict. The accepted unprivileged
discovery then matched exactly:

```text
VID:PID       08bb:2902
product       USB_PnP_Sound_Device
physical path platform-3f980000.usb-usb-0:1:1.0
ALSA endpoint hw:1,0,0 (derived only after stable identity matched)
```

## Production A/V graph

The runtime resolved audio before claiming the camera and reported:

- `audio.state=MATCHED`
- S16LE, 48 kHz, mono
- AAC MPEG-4/LC, raw stream format
- `voaacenc` at 128,000 bit/s followed by `aacparse`
- positive encoded AAC access-unit counts
- `startup_video_only_fallback_used=false`

`alsasrc` explicitly uses the shared pipeline clock with
`provide-clock=false`, `use-driver-timestamps=false`, `do-timestamp=true`, and
`slave-method=resample`. Both audio queues are bounded and downstream-leaky so
optional audio cannot backpressure recording. Every accepted sidecar declared
`audio.available=true` only after validated effective caps and positive
fragment AAC observations.

## Ten-clip media acceptance

The reviewed read-only harness is
`deploy/ssh-dev-validation/milestone7-audio`. Its final `SHA256SUMS` SHA-256 is
`fc1270741c4aa2ac7a40cc4a4f66ee5c723d0652b2e69f17957a0abd31dbad9c`.
The accepted result for ordinary sequences 161–170 is:

- Result SHA-256:
  `8a4c103a99cae87a04f662ab02288944bf35fdd8b921e422bc0f95e905bd9436`
- Ignored raw copy:
  `artifacts/pi-m7-20260727/dashcam-m7-audio-161-170-v2.json`
- Ten of ten clips independently decoded with `h264_v4l2m2m` while explicitly
  mapping both video and audio.
- Ten of ten began with an independently inspected H.264 IDR packet.
- Encoded video was 1920×1080, 30/1, H.264 High; bitrate was approximately
  8.00 Mbit/s.
- Audio was AAC-LC, 48 kHz mono; reported bitrate was 127,545–128,000 bit/s.
- Durations stayed within 59–61 seconds.
- All nine sidecar-monotonic boundary deltas were exactly zero.
- Maximum stream-edge A/V skew was 64.333 ms for the first clip and
  4.000–14.666 ms for the following nine clips. It stayed below 100 ms with no
  systematic growth.

The harness uses one compact stream/format query, a separate bounded
first-packet IDR query, and a bounded full hardware-assisted decode. It never
requests all packets or all frames. Two observer corrections preceded the
accepted run: encoded width/height were added instead of trusting the sidecar,
and the exact audio-stream `r_frame_rate="0/0"` field was admitted as a
required closed-schema value. Failed observer runs created no acceptance
output.

## Final state and open work

- `dashcamd.service`: inactive/dead after clean status 0, zero restarts
- `dashcam-network-fallback.service`: inactive/dead, zero restarts
- Throttle/undervoltage flags: `0x0`
- Root free: 2,926,575,616 bytes
- Recording-volume free: 14,898,823,168 bytes
- No current-run partial member remains; the 18 retained `pending` files are
  the documented historical diagnostics.

The code now contains exact audio-error source classification and a bounded,
generation-based reconnect coordinator, but production dynamic branch
mutation is deliberately disabled. Releasing and re-requesting
`splitmuxsink`'s audio pad while PLAYING must first pass the exact-Pi experiment
without changing camera/encoder identity or interrupting video. Until that
gate and the owner-assisted physical tests pass, the remaining Milestone 7
checkboxes and exit gate stay open.

Final host validation passed: 1,327 tests with 10 expected platform skips,
Ruff, and strict mypy across 70 source files. `git diff --check` reported only
the repository's normal Windows line-ending warnings.
