# Architecture status

## Intended boundaries

The intended control plane is typed Python, while media movement and encoding use
native camera/media components. `dashcamd` is the only camera owner. Web, preview,
GPS, audio, retention, and helper processes must be isolated so optional failures
cannot terminate or backpressure continuous recording.

Media timing is monotonic/pipeline based. GPS may anchor UTC when trusted, while
IANA timezone data supplies display-local time. Canonical clip metadata and names
remain UTC. A clip has a stable UUID and separate lifecycle/protection/download
state; its MP4 and JSON sidecar are a recoverable logical pair.

Recording is allowed only after `/srv/dashcam` is verified as the writable exFAT
`DASHCAM` volume. A missing, unknown, damaged, or read-only mount is a storage
fault—never a reason to write to rootfs or format automatically.

## Phase 0B decision status

The owner-approved 2026-07-24 Pi capability run selected the media and UART
foundation below. Live GPS receive and the connected USB-audio encode branch
are now measured. Recording storage, audio fault recovery, full A/V integration,
and phone-preview transport remain open.

| Decision | Current status | Evidence |
| --- | --- | --- |
| Raspberry Pi OS architecture | Select 32-bit `armhf` Trixie image dated 2026-06-18 for the first implementation slice | Exact image runs the required 1080p30 hardware pipeline with 321 MiB idle memory available after reboot |
| Camera source and buffer format | Select one in-process GStreamer `libcamerasrc`; use 1920x1080 NV12 at 30 fps for recording | IMX219 modes/caps and live pipeline measurements |
| H.264 encoder and caps | Select `/dev/video11` through `v4l2h264enc`, 8 Mbit/s CBR, High Profile Level 4.1, GOP 30, no B frames | Device controls, open file descriptors, PTS measurement, and validated clips |
| MP4 muxer/finalization profile | Select asynchronous `splitmuxsink` with `mp4mux` fragmented at 1-second intervals; ordinary target is 60-second IDR boundaries | Clean segments and the active fragment remained independently decodable; standard MP4 did not survive the same abrupt kill |
| UART | Select PL011 `/dev/ttyAMA0` through `/dev/serial0`; disable Bluetooth and remove the serial console | Boot-file hashes, reboot, device-link and GPIO-function verification |
| Burned-in overlay | Keep GStreamer NV12 `textoverlay` as the first measured candidate | 1080p30 negotiation succeeded; added about 45 percentage points of one-core CPU and about 9 MiB RSS in a short run |
| Preview camera path | Select a secondary 640x360 NV12 stream from the same `libcamerasrc`; request 30 fps and drop to 15 fps only after a bounded leaky queue | Dual-stream test retained the 1080p30 recording caps and one camera owner |
| USB audio device identifier and AAC path | Select USB `08bb:2902` plus product/physical path; 48 kHz mono S16LE through `alsasrc`/bounded queue/`audioresample`/`voaacenc` 128 kbit/s/`aacparse` | Live PCM, AAC-LC, bounded absence, and reconnect captures passed; integrated active-recording hot-unplug recovery remains Milestone 7 |
| GPS baud and NMEA reliability | Select receive-only 115200-baud NMEA from the FlyFishRC M10 Mini on PL011 | 1,308 checksum-valid records in a short live capture, including required GGA/RMC fixes; loss/reconnect and endurance remain later gates |
| exFAT tooling, layout and provisioner | Open | The flashed image auto-expanded ext4 across the card; no destructive storage authorization exists |
| Phone preview transport | Open | Requires phone/browser latency and resource measurements in Milestone 11 |

## Architecture records

### ADR-P0B-001: GStreamer owns the single camera pipeline

- Decision: `dashcamd` will embed one GStreamer graph with `libcamerasrc`.
- Basis: the installed GStreamer 1.26.2 stack negotiated IMX219 NV12 recording
  and preview streams, opened the V4L2 hardware encoder, and produced validated
  MP4 files. `rpicam-vid` was useful as an independent hardware proof but is not
  selected as the production camera owner. Picamera2 is not installed.
- Constraint: both libcamera output streams must be requested at 30 fps. Asking
  the preview source pad for 15 fps reduced the recording pad to 15 fps; preview
  rate reduction belongs downstream of its leaky queue.
- Risk: the manual dual-stream fakesink graph hung while waiting for EOS after
  `SIGINT`. The production graph needs bounded shutdown/finalization tests before
  preview is enabled.

### ADR-P0B-002: Hardware H.264 and fragmented split muxing

- Decision: encode 1920x1080 NV12 at 30 fps with `/dev/video11` via
  `v4l2h264enc`, 8 Mbit/s CBR, High Profile Level 4.1, GOP 30, repeated sequence
  headers, and `h264parse config-interval=-1`.
- Decision: use asynchronous `splitmuxsink` plus `mp4mux` with a 1-second
  fragment duration. The ordinary split target remains approximately 60 seconds
  on requested IDR boundaries.
- Basis: direct PTS evidence measured 30.005 fps and about 8.02 Mbit/s. Every
  clean short segment and the abruptly interrupted active fragmented segment
  decoded independently and began with an IDR. Standard non-fragmented MP4 was
  unplayable after the equivalent abrupt kill. Periodically updated robust MP4
  also survived, but fragmented MP4 better matches the product recovery rule.
- Constraint: the short capability clips do not satisfy the later ten-clip,
  60-second continuity, two-hour, or power-loss acceptance tests.

### ADR-P0B-003: 32-bit OS and PL011 GPS UART

- Decision: retain the tested 32-bit `armhf` Raspberry Pi OS Lite image for the
  first target implementation. A 64-bit reflash comparison is unnecessary unless
  a later dependency or performance result invalidates this proven path.
- Decision: make PL011 primary on GPIO 14/15 with `dtoverlay=disable-bt`,
  enable the UART, and remove the serial console. Bluetooth is deliberately
  unavailable to the product.
- Basis: the selected image ran the video path without sustained swap or
  throttling. After reboot `/dev/serial0` resolves to `/dev/ttyAMA0`, GPIO 14/15
  resolve to TXD0/RXD0, and the kernel command line has no serial console.
- Basis: the connected FlyFishRC M10 Mini produced checksum-valid NMEA at
  115200 baud, including required GGA/RMC fix data at about 10 Hz, through
  receive-only wiring. Exact location data is excluded from repository evidence.
- Constraint: later integration must still prove bounded loss/reconnect,
  malformed-input handling, trusted time anchors, and endurance behavior.

### ADR-P0B-004: Optional USB audio branch

- Decision: identify the intended microphone by USB VID/PID `08bb:2902`,
  product identity, and configured physical path. Never substitute a device
  merely because it occupies the expected ALSA card index. The device has no
  unique USB serial.
- Decision: capture native 48 kHz mono S16LE through `alsasrc`, isolate it behind
  a bounded queue, use pipeline timestamps and `audioresample`, then encode
  AAC-LC with `voaacenc bitrate=128000` and parse before the selected MP4 muxer.
- Basis: live PCM and standalone M4A captures passed on the exact Pi/device;
  the encoded stream measured AAC-LC, 48 kHz mono, 128 kbit/s.
- Constraint: standalone audio capability does not prove A/V skew, mux
  integration, hot-unplug isolation, restoration at a safe clip boundary, or
  repeated reconnect behavior. Those gates remain open.

## Implemented hardware-independent boundaries

The local implementation deliberately stops at injectable target interfaces:

- The recorder daemon owns lifecycle, cancellation, readiness/watchdog
  notification, and a future single-owner media runtime.
- The GPS service supervises an injected timeout-bounded byte transport, parses
  bounded NMEA lines, clears stale navigation, and reconnects with capped backoff.
- Stable ALSA selection matches USB identity/physical topology and refuses
  volatile numeric-index selectors.
- The SQLite/WAL catalog on the future ext4 state volume coordinates bounded
  leases, retention eligibility, durable pair-operation intents, event windows,
  and bounded startup reconciliation against an injected recording filesystem.
- Overlay formatting consumes one coherent telemetry snapshot and emits bounded
  text/layout data; the target renderer is not selected.
- The unprivileged web policy layer owns sessions, CSRF, reauthentication, input
  bounds, and secret redaction. It talks only through a closed, bounded
  Unix-socket protocol; recorder-approved downloads carry leases and are released
  on completion, error, or client disconnect.

These interfaces and local fakes prove control behavior only. The camera/media
backend, UART/ALSA discovery adapters, actual socket ownership, HTTP serving,
overlay renderer, and privileged removal helper still require Phase 0B evidence
and target integration.

## Structured logging convention

Application logs are one bounded JSON object per line with UTC timestamps and
stable component/boot/clip context fields. Structured values are accepted only
through an explicit event-data mapping; arbitrary record extras are ignored.
Sensitive key names and common `key=value` secret forms are redacted, strings and
collections are bounded, and exception text is excluded by default. Callers must
still never place credentials in free-form messages.
