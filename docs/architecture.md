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
foundation below. Live GPS receive, the connected USB-audio encode branch, the
exact-card recording storage, and the video-only recorder are now measured.
Connected-microphone A/V muxing and synchronization are also measured. Active
logical audio loss/restoration is implemented and measured, and ordinary
startup without the configured microphone has passed video-only acceptance.
Physical hot-unplug/replug is not a current acceptance requirement.
Phone-preview transport remains open.

| Decision | Current status | Evidence |
| --- | --- | --- |
| Raspberry Pi OS architecture | Select 32-bit `armhf` Trixie image dated 2026-06-18 for the first implementation slice | Exact image runs the required 1080p30 hardware pipeline with 321 MiB idle memory available after reboot |
| Camera source and buffer format | Select one in-process GStreamer `libcamerasrc`; use 1920x1080 NV12 at 30 fps for recording | IMX219 modes/caps and live pipeline measurements |
| H.264 encoder and caps | Select dynamically discovered `v4l2h264enc` for Milestone 6 with an explicit level cap, High/4.1, 8 Mbit/s target, GOP 30, repeated headers, and the default hardware VBR mode; never request `video_bitrate_mode=1` on this stack | Control matrix plus installed continuous recorder/rollover evidence in `docs/test-reports/2026-07-26-gstreamer-explicit-caps.md` and `docs/test-reports/2026-07-26-milestone6-recorder-live.md` |
| MP4 muxer/finalization profile | Select asynchronous `splitmuxsink` with `mp4mux` in explicit `dash-or-mss` fragmented mode at 1-second intervals; ordinary target is a 60-second IDR boundary | The installed service promoted a 59.988667-second IDR-started production pair, finalized its active 24.528667-second shutdown fragment, refused a collision, and reconciled an interrupted pair operation; evidence: `docs/test-reports/2026-07-26-milestone6-finalization-live.md` |
| UART | Select PL011 `/dev/ttyAMA0` through `/dev/serial0`; disable Bluetooth and remove the serial console | Boot-file hashes, reboot, device-link and GPIO-function verification |
| Burned-in overlay | Keep GStreamer NV12 `textoverlay` as the first measured candidate | 1080p30 negotiation succeeded; added about 45 percentage points of one-core CPU and about 9 MiB RSS in a short run |
| Preview camera path | Select a secondary 640x360 NV12 stream from the same `libcamerasrc`; request 30 fps and drop to 15 fps only after a bounded leaky queue | Dual-stream test retained the 1080p30 recording caps and one camera owner |
| USB audio device identifier and AAC path | Select USB `08bb:2902` plus product/physical path; shared-pipeline-clock 48 kHz mono S16LE through `alsasrc`/bounded queue/`audioresample`/`voaacenc` 128 kbit/s/`aacparse`; production defaults retain bounded three-slot immutable-generation loss isolation and restoration | Ten integrated A/V clips passed IDR, independent decode, exact-zero boundaries, and 4.000–64.333 ms stream-edge skew. The final two-cycle logical loss/restoration run passed audio truth `[true,false,true,false,true]`, IDR-first hardware decode, 71.958–84.291 ms A/V skew, unchanged drops, and zero restart. Release `0.1.0.dev0-09a6dd3b374d3952` then passed ordinary startup without the microphone and finalized truthful video-only media. Physical hot-unplug/replug is not a current acceptance requirement. Evidence: `docs/test-reports/2026-07-27-milestone7-audio-live.md`, `docs/test-reports/2026-07-28-milestone7-production-restoration-live.md`, `docs/test-reports/2026-07-28-milestone7-absent-startup-live.md` |
| GPS baud, NMEA reliability, UTC anchoring, clip telemetry, and wall-clock ownership | Select receive-only 115200-baud NMEA from the FlyFishRC M10 Mini on PL011; accept only checksum/parse-valid RMC/ZDA UTC through configured plausibility/continuity policy; coalesce RMC/GGA by receiver epoch into a bounded 10 Hz, three-minute monotonic history and half-open per-clip windows; retain stock `systemd-timesyncd` as the sole Linux wall-clock owner while all media timing remains pipeline/monotonic; reconcile provisional sidecars/names through a schema-4 durable intent and bounded same-boot UUID backlog | The production UART, no-GPS, anchor, sidecar, clock-step, late-lock reconciliation, and exact-exFAT collision gates passed. Release `0.1.0.dev0-7fd1e73debb731b6` retained exactly 600 unique samples in a full clip and 431 in its shutdown successor. Release `0.1.0.dev0-6f943f3a4edf7117` reconciled two no-GPS clips from one later trusted anchor while preserving UUIDs, truthful empty historical navigation, full hardware/AAC decode, and zero drops/restarts. A controlled +120-second wall-clock step left all sequence-390 video/audio PTS and DTS strictly increasing. Evidence: `docs/test-reports/2026-07-28-milestone8-gps-uart-live.md`, `docs/test-reports/2026-07-28-milestone8-no-gps-live.md`, `docs/test-reports/2026-07-28-milestone8-gps-anchor-live.md`, `docs/test-reports/2026-07-28-milestone8-gps-sidecar-live.md`, `docs/test-reports/2026-07-28-milestone8-clock-step-live.md`, and `docs/test-reports/2026-07-28-milestone8-reconciliation-live.md` |
| exFAT tooling, layout and provisioner | Select the exact-card SSH-first 6 GiB ext4 p2 plus UUID-mounted exFAT `DASHCAM` p3 contract | Authorized Stage A/B completed on CID `fe34325344000000200000031a0192d1`; storage preflight, bounded fsck, throughput, and recorder finalization passed. Any different card or destructive layout change requires a new exact preflight and authorization |
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

- Decision for Milestone 6 implementation: encode 1920x1080 NV12 at 30 fps with the dynamically
  discovered `v4l2h264enc` factory, High Profile Level 4.1, repeated sequence
  headers, 8 Mbit/s target, GOP 30, and `h264parse config-interval=-1`. Require
  explicit downstream H.264 level caps, retain the driver's default VBR mode,
  and do not assign the plugin's read-only `device` property.
- Control constraint: never set `video_bitrate_mode=1` on the exact Trixie
  stack. Isolated runs proved that this CBR-mode control alone reproduces
  `STREAMON`/kernel `ret -3`, regardless of bitrate-control ordering. The
  8 Mbit/s bitrate and GOP-30 controls pass individually and together under
  default VBR, which satisfies the specification's constrained-VBR allowance.
- Decision: use asynchronous `splitmuxsink` plus `mp4mux` with a 1-second
  fragment duration and explicit `dash-or-mss` fragment mode. The ordinary
  split target remains approximately 60 seconds on requested IDR boundaries.
- Shutdown constraint: this exact stack accepts EOS and posts a validated
  `splitmuxsink-fragment-closed` event for the active output, but it does not
  post a pipeline-level EOS. The backend therefore accepts either EOS or exact
  closure of the currently active fragment, rejects stale prior-fragment
  closure as completion, and always performs the bounded `NULL` transition.
  `first-moov-then-finalise` is not selected because it also failed to complete
  EOS under a 25-second diagnostic bound.
- Basis: the final bounded production-cap transport stream negotiated High/4.1,
  1920x1080 at 30/1 with no B frames, measured 8,221,871 bit/s, decoded without
  error, began on an independently verified IDR keyframe, and emitted keyframes
  at one-second intervals.
  Direct earlier PTS evidence measured 30.005 fps and about 8.02 Mbit/s. Every
  clean short segment and the abruptly interrupted active fragmented segment
  decoded independently and began with an IDR. Standard non-fragmented MP4 was
  unplayable after the equivalent abrupt kill. Periodically updated robust MP4
  also survived, but fragmented MP4 better matches the product recovery rule.
- Constraint: the short capability clips do not satisfy the later ten-clip,
  60-second continuity, two-hour, or power-loss acceptance tests.
- Local implementation status: `src/dashcam/recorder/gstreamer.py` now builds
  this exact graph without opening hardware at import time. The composed runtime
  first binds a successful live storage preflight, allocates a collision-free
  provisional name only under `/srv/dashcam/pending`, starts one process-local
  camera owner, waits for a validated first-fragment-opened event, and
  continuously drains bounded fragment-closed events. Startup rollback and
  cooperative shutdown both force the graph to `NULL` within bounded time.
- Target implementation status: hash-closed release
  `0.1.0.dev0-ce028ba96d40fb9d` installs the composed service. It sustained
  ordinary one-minute rollovers without camera/encoder/service restart,
  promoted independently decodable High/4.1 plus AAC-LC production pairs, and
  passed the accepted two-cycle logical microphone-loss/restoration run.
- Finalization implementation status: a validated fragment closure is mapped into the
  host monotonic domain, its closed MP4 is verified and flushed, and a canonical
  sidecar is staged and read back under `pending`. The ext4 catalog commits a
  `FINALIZING` row plus explicit `FINALIZE` intent before either exFAT member
  moves. Promotion removes the active `.partial` suffix, uses Linux
  `renameat2(RENAME_NOREPLACE)`, flushes affected directories, validates
  canonical sidecar identity again during replay, and marks `FINALIZED` only
  after both target members exist. The exact Pi refused a case-colliding target
  without a pair mutation. An injected interruption after one member moved
  retained its intent and reconciled to a pair on the next normal service
  start. MP4-only diagnostics, including the earlier fail-closed sequence
  `000011`, are preserved rather than synthesized.
- Milestone 6 acceptance: runtime metrics, no-progress watchdog reporting,
  bounded 1/2/4-second camera/encoder recovery, ten independently decodable
  consecutive clips (30–39), and a 7,200-second run passed on the exact Pi.
  The endurance result is bound to the raw-result hash and a strict zram-only,
  no-growth reanalysis. The synthetic recovery MP4 still validates only the
  pair state machine; it is not playable-production-media evidence. See
  `docs/test-reports/2026-07-27-milestone6-metrics-recovery-endurance-live.md`.

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
- Constraint: later integration must still complete the wider GPS
  loss/reconnect, malformed-input, date/conflict/rollover, and overlay
  shared-snapshot acceptance matrix.
- Implementation update (2026-07-28): the production Linux adapter opens only
  read-only/nonblocking with `O_NOCTTY` and `O_CLOEXEC`, requires a character
  device, configures raw 8N1 at 115200 with flow control disabled and
  `VMIN=1`/`VTIME=0`, and never writes. A zero-byte read fails as bounded `EIO`;
  the ninth consecutive selector-ready no-progress read also fails boundedly so
  the GPS supervisor reconnects with capped backoff rather than starving the
  recorder. The exact-Pi 72-second run recovered one such transport event with
  no pipeline restart; see `docs/test-reports/2026-07-28-milestone8-gps-uart-live.md`.
- Implementation update (2026-07-28): the production GPS service now feeds
  checksum/parse-valid RMC/ZDA candidates into one bounded anchor tracker. The
  configured policy enforces UTC plausibility, ordinary and reacquisition
  disagreement limits, interval bounds, explicit source/provenance, and
  uncertainty. Release `0.1.0.dev0-75947a15db03f4b3` accepted one GN RMC
  anchor plus 199 continuity confirmations with zero rejection and no media
  restart; see
  `docs/test-reports/2026-07-28-milestone8-gps-anchor-live.md`.
- Implementation update (2026-07-28): receiver-epoch-coalesced RMC/GGA
  navigation now feeds a bounded three-minute history and half-open per-clip
  windows. The full exact-Pi clip retained exactly 600 unique, ordered samples;
  its successor retained 431 with zero overlap. Provisional sidecars retain the
  samples as `MONOTONIC_ONLY` with null UTC for later reconciliation; see
  `docs/test-reports/2026-07-28-milestone8-gps-sidecar-live.md`.
- Implementation update (2026-07-28): schema-4 durable intents retain the
  replacement canonical sidecar and expected provisional-source hash before
  device-bound, no-replace pair moves. A 64-entry same-boot backlog attempts at
  most two UUIDs per fragment after a trusted anchor. Exact-Pi late lock
  reconciled two no-GPS clips while preserving stable UUIDs and media
  continuity; an isolated case-variant exFAT collision refused before mutation.
  See `docs/test-reports/2026-07-28-milestone8-reconciliation-live.md`.
- Scope boundary: GPS anchors feed metadata reconciliation but never discipline
  the Linux wall clock. Stale/lost navigation clears current position/speed
  while retaining only a bounded stale UTC anchor. The exact-Pi production
  wheel passed loss/reconnect, malformed/checksum/oversized input,
  implausible-date and conflict refusal, UTC-midnight rollover, and
  `Asia/Jerusalem` DST/standard-offset cases. A later integrated transient run
  kept the real camera, hardware encoder, exFAT catalog, and reconciliation
  active through silence, conflict, transport replacement, and recovered
  navigation with zero drops/restarts; the validator is kernel-lock serialized.
  Milestone 8 is accepted; overlay rendering and its shared-snapshot
  performance gates remain Milestone 9 work.

### ADR-P0B-004: Optional USB audio branch

- Decision: identify the intended microphone by USB VID/PID `08bb:2902`,
  product identity, and configured physical path. Never substitute a device
  merely because it occupies the expected ALSA card index. The device has no
  unique USB serial.
- Decision: capture native 48 kHz mono S16LE through `alsasrc`, isolate it behind
  a bounded queue, use pipeline timestamps and `audioresample`, then encode
  AAC-LC with `voaacenc bitrate=128000` and parse before the selected MP4 muxer.
- Basis: live PCM and standalone M4A captures passed on the exact Pi/device;
  ten integrated one-minute clips then passed AAC-LC 48 kHz mono at
  approximately 128 kbit/s, independent full A/V decode, IDR starts,
  exact-zero boundaries, and sub-100 ms stream-edge skew without growth.
- Decision: production defaults enable logical microphone-loss isolation and
  restoration through three prebuilt immutable recording slots. Two stable
  exact-identity `NOT_FOUND` confirmations trigger a bounded worker-dispatched,
  IDR-held switch from A/V to video-only. Stable rediscovery rebuilds the dead
  ALSA ingress and restores A/V at the next proven safe boundary. Retired slots
  are recycled only after exact closure, finalization, and topology proof;
  request-pad counts and the parent camera/hardware encoder stay constant.
- Decision: the serialized EOS arbiter atomically reserves a unique
  generation-EOS seqnum before dispatch. It accepts that exact event, may drop
  at most one proven late source EOS racing the reservation, and refuses
  duplicates or any other drift. Video closure may reuse the boundary only
  when fragment ownership proves either exactly one open retiring fragment or
  that exact fragment already closed before the observer ran.
- Basis: final release `0.1.0.dev0-ce028ba96d40fb9d` passed two controlled
  sysfs loss/restoration cycles through ordinary production construction:
  audio truth `[true,false,true,false,true]`, five IDR-first independently
  hardware-decoded clips, A/V skew below 100 ms, exact identity rematch,
  constant topology, no drops or pipeline restarts, and no throttling. The
  generation-EOS fallback path was observed directly. See
  `docs/test-reports/2026-07-28-milestone7-production-restoration-live.md`.
- Constraint: controlled software deauthorization is not owner-assisted
  physical unplug. It does not itself prove microphone-absent startup, physical
  USB topology recovery, a naturally reassigned ALSA index, or physical
  wrong-device behavior. Microphone-absent startup passed separately; the
  remaining physical cases are not current acceptance requirements. The exact-
  Pi non-mutating refusal probe rejected direct live
  `splitmuxsink` `audio_%u` release/re-request: its public API has no
  pre-switch request-pad drain/old-closure/new-mux barrier and async finalization
  leaves a context race. Do not mutate a live generation.
- Constraint: the selected production path never dynamically mutates a live
  generation. The direct pad-mutation refusal remains applicable.
- Scope: logical loss isolation/restoration remains implemented and evidenced,
  but physical hot-unplug/replug recovery is not a current product acceptance
  requirement. The configured microphone is expected to remain connected in
  normal use. Stable identity and non-substitution remain mandatory, and
  ordinary startup with the configured microphone absent must select truthful
  video-only recording; that gate passed on the exact Pi.

## Implemented hardware-independent boundaries

The local implementation keeps target-dependent operations behind injectable
interfaces:

- The recorder daemon owns lifecycle, cancellation, readiness/watchdog
  notification, and a composed single-owner GStreamer runtime. The runtime
  enforces the verified storage gate before opening media, validates the fixed
  production caps and hardware encoder identity, and writes only provisional
  fragments under the verified recording mount.
- The GPS service supervises a timeout-bounded receive-only PL011 transport,
  parses bounded NMEA lines, clears stale navigation, and reconnects with capped
  backoff. Its Linux selector adapter opens only the configured `/dev` character
  device with `O_RDONLY|O_NOCTTY|O_NONBLOCK|O_CLOEXEC`, raw 8N1, flow control
  disabled, `VMIN=1`/`VTIME=0`, and no write interface. Zero-byte and repeated
  ready-without-data reads fail boundedly; a quiet unready UART still times out.
  GPS starts only after recording is active, persists across camera recovery,
  publishes no coordinates in runtime status, and is stopped within bounded
  recorder shutdown. Checksum/parse-valid RMC/ZDA candidates now feed the
  configured plausibility/continuity tracker, and its accepted monotonic/UTC
  anchor, provenance, uncertainty, and counters feed privacy-safe status.
  A bounded receiver-epoch-coalesced navigation history feeds half-open,
  monotonic clip windows at no more than 600 samples per minute. Once a trusted
  anchor exists, the schema-4 reconciliation worker projects eligible clip and
  sample times into UTC/local time, atomically replaces canonical sidecars,
  and no-replace renames provisional pairs under durable catalog intents while
  preserving their stable UUIDs. The approved image keeps stock
  `systemd-timesyncd` as its sole Linux wall-clock owner;
  the recorder neither disciplines wall time nor derives PTS/DTS from it. A
  controlled +120-second wall-clock step during production recording left
  video/audio packet timestamps strictly increasing and caused no drop or
  restart. Exact-Pi configured-device-absence recording and a later controlled
  boot with the same deliberately absent GPS path both reached truthful
  `UART_UNAVAILABLE`/`UNSYNCED` recording without media restart. These are not
  physical-unplug qualifications, which are outside the current requirement.
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

The GStreamer adapter and production process entry point have bounded exact-Pi
rollover/shutdown, recovery, video-only endurance, connected-microphone ten-clip
A/V, and repeated logical microphone-loss/restoration evidence. Direct dynamic
audio-pad mutation remains refused; immutable-generation/IDR handoff with
bounded three-slot recycling is selected. Microphone-absent startup has passed;
physical hot-unplug/replug is outside current acceptance. Remaining work
includes the GPS/time fault matrix, actual socket ownership, HTTP
serving, overlay rendering, and the privileged removal helper.

## Structured logging convention

Application logs are one bounded JSON object per line with UTC timestamps and
stable component/boot/clip context fields. Structured values are accepted only
through an explicit event-data mapping; arbitrary record extras are ignored.
Sensitive key names and common `key=value` secret forms are redacted, strings and
collections are bounded, and exception text is excluded by default. Callers must
still never place credentials in free-form messages.
