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
| Burned-in overlay | Use the recorder-owned native NV12/DMABUF fixed-luma-region renderer before hardware encoding; reject stock `textoverlay` and `gdkpixbufoverlay` | Installed `textoverlay` delivered about 10.4 fps and stock fixed-region composition about 18.3 fps. The native path passed exact-Pi dynamic GPS state, stored-pixel, shared stable-anchor time-model, clip-boundary, and recovery qualification at about 30.006 actual packet fps with zero live drops/restarts/renderer failures. The separate Section C1 paired resource matrix remains open; evidence: `docs/test-reports/2026-08-03-milestone9-overlay-candidate-failure.md` and `docs/test-reports/2026-08-09-milestone9-functional-overlay-live.md` |
| Preview camera path | Select a secondary 640x360 NV12 stream from the same `libcamerasrc`; request 30 fps and drop to 15 fps only after a bounded leaky queue | Dual-stream test retained the 1080p30 recording caps and one camera owner |
| USB audio device identifier and AAC path | Select USB `08bb:2902` plus product/physical path; shared-pipeline-clock 48 kHz mono S16LE through `alsasrc`/bounded queue/`audioresample`/`voaacenc` 128 kbit/s/`aacparse`; production defaults retain bounded three-slot immutable-generation loss isolation and restoration | Ten integrated A/V clips passed IDR, independent decode, exact-zero boundaries, and 4.000–64.333 ms stream-edge skew. The final two-cycle logical loss/restoration run passed audio truth `[true,false,true,false,true]`, IDR-first hardware decode, 71.958–84.291 ms A/V skew, unchanged drops, and zero restart. Release `0.1.0.dev0-09a6dd3b374d3952` then passed ordinary startup without the microphone and finalized truthful video-only media. Physical hot-unplug/replug is not a current acceptance requirement. Evidence: `docs/test-reports/2026-07-27-milestone7-audio-live.md`, `docs/test-reports/2026-07-28-milestone7-production-restoration-live.md`, `docs/test-reports/2026-07-28-milestone7-absent-startup-live.md` |
| GPS baud, NMEA reliability, UTC anchoring, clip telemetry, and wall-clock ownership | Select receive-only 115200-baud NMEA from the FlyFishRC M10 Mini on PL011; accept only checksum/parse-valid RMC/ZDA UTC through configured plausibility/continuity policy; coalesce RMC/GGA by receiver epoch into a bounded 10 Hz, three-minute monotonic history and half-open per-clip windows; retain stock `systemd-timesyncd` as the sole Linux wall-clock owner while all media timing remains pipeline/monotonic; finalize directly with canonical metadata when a trusted anchor already exists, otherwise reconcile provisional sidecars/names through a schema-4 durable intent and bounded same-boot UUID backlog | The production UART, no-GPS, anchor, sidecar, clock-step, late-lock reconciliation, and exact-exFAT collision gates passed. Release `0.1.0.dev0-7fd1e73debb731b6` retained exactly 600 unique samples in a full clip and 431 in its shutdown successor. Release `0.1.0.dev0-6f943f3a4edf7117` reconciled two no-GPS clips from one later trusted anchor while preserving UUIDs, truthful empty historical navigation, full hardware/AAC decode, and zero drops/restarts. Source commit `864bbef` later proved direct first-`FINALIZE` canonical GPS pairs for sequences 65/66 while preserving the late-lock path, but its resource candidate was rejected before matrix work. A controlled +120-second wall-clock step left all sequence-390 video/audio PTS and DTS strictly increasing. Evidence: `docs/test-reports/2026-07-28-milestone8-gps-uart-live.md`, `docs/test-reports/2026-07-28-milestone8-no-gps-live.md`, `docs/test-reports/2026-07-28-milestone8-gps-anchor-live.md`, `docs/test-reports/2026-07-28-milestone8-gps-sidecar-live.md`, `docs/test-reports/2026-07-28-milestone8-clock-step-live.md`, `docs/test-reports/2026-07-28-milestone8-reconciliation-live.md`, and `docs/test-reports/2026-08-09-milestone9-direct-anchor-finalization-rejected.md` |
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
  Milestone 8 is accepted. Milestone 9 later proved that overlay rendering and
  sidecar finalization share the GPS producer and stable-anchor/monotonic time
  model; it does not claim literal Python snapshot identity. The paired
  performance gate remains open.

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

### ADR-P0B-005: Burned-in telemetry overlay

- Rejected decision: stock GStreamer `textoverlay` in the common 1920x1080
  NV12 path negotiated correctly but delivered only about 10.4 fps on the
  exact Pi. Stock `gdkpixbufoverlay` with a pre-rendered small RGBA region
  delivered about 18.3 fps. Neither may be used for the production profile.
- Current path: attach one recorder-owned native NV12/DMABUF pad renderer at
  the exact raw caps before `v4l2h264enc`; it caches bounded mappings and a
  pre-rendered opaque fixed luma region while retaining per-buffer allocation
  and metadata validation. The isolated capability probe delivered 30.006 fps,
  matching a same-session no-overlay arm. A later active GPS/audio/overlay
  runtime-only v7 wheel probe sustained hardware 1080p30 on verified exFAT
  with zero drops/restarts/renderer failures, but p95 process CPU was 100.9876%
  and therefore missed the strict Section C1 ceiling by 0.9876 percentage
  points. Video-only, legacy A/V, and immutable audio-generation graphs must
  still share exactly one camera, overlay path, and hardware encoder.
- Decision: configure the initial overlay while the graph is still stopped so
  the first encoded buffer is never briefly unmarked. Later updates are one
  fixed two-line printable-ASCII payload, deduplicated at 2 Hz with no queue or
  frame copies. A failed live property update is bounded to the optional
  overlay worker and cannot restart or backpressure recording.
- Decision: take one immutable `GpsSnapshot` per update. Project its accepted
  monotonic/UTC anchor with the same `AnchorPolicy` builder used by the GPS
  service, then apply the configured IANA timezone. Navigation values are used
  only when both the service state and sentence say they are valid; stale
  values collapse to `GPS LOST`, and absent/untrusted time collapses to
  `TIME UNSYNCED`.
- Dependency history: release `0.1.0.dev0-e727ddccd94659ff` installed exact
  `gstreamer1.0-x=1.26.2-1+rpt3+deb13u1` to obtain `textoverlay`; that renderer
  is now rejected. Corrected images and application manifests no longer
  declare it or smoke-test its factory. The package remains intentionally
  installed as a harmless legacy extra on the development Pi because the
  fail-closed installer performs exact additions but no removals; removing it
  would require a separate authorized pinned transaction.
- Evidence state: the rejected production candidate, stock fixed-region
  alternative, native-transform capability arm, and matched no-overlay arm are
  recorded in
  `docs/test-reports/2026-08-03-milestone9-overlay-candidate-failure.md`.
  The diagnostic active-GPS resource near-pass and its strict refusal are in
  `docs/test-reports/2026-08-03-milestone9-overlay-resource-limit.md`.
  Dynamic production rendering is implemented. A hash-closed exact-Pi
  functional run decoded five stored-video luma crops covering unsynced,
  valid, stale on both sides of an exact adjacent clip boundary, and recovered
  valid states. Both canonical sidecars used the same stable GPS anchor model,
  frame PTS mapped into their half-open monotonic windows, and valid/stale
  sample ownership matched the displayed state. Live drops, restarts, renderer
  failures, and throttling remained zero; actual media rates were about
  30.006 packets/s. This closes the functional and boundary gates, not the
  longer paired resource comparison. Evidence is in
  `docs/test-reports/2026-08-09-milestone9-functional-overlay-live.md`.
  The exact v7 wheel is now installed as hash-closed release
  `0.1.0.dev0-5f95dd806342ac9e`; deployment does not change its strict resource
  refusal. A later hash-closed fused-validation candidate retained full
  per-buffer safety validation but did not clear the pre-matrix screening
  contract: its same-boot p95 regressed by 0.9989 percentage points
  and an encoder-input-PTS-gap drop was already present at the first
  post-warm-up observation. It was rolled back to `5f95`; the source remains
  rejected Git evidence rather than the installed release. See
  `docs/test-reports/2026-08-09-milestone9-fused-validation-rejected.md`.
  Later serialized-CAPS and frozen-sidecar canonical-byte-cache candidates
  reduced steady renderer and rollover work but still failed the pre-matrix
  absolute screen (best p95 102.9872% with one rollover timestamp-gap drop).
  Exact traces place the remaining burst in the durable GPS-sidecar
  provisional-to-canonical reconciliation worker, not the GPS-window snapshot.
  Both candidates were rolled back to dormant `5f95`. Source commit `864bbef`
  then implemented direct anchored finalization while retaining late-lock
  recovery and catalog durability. Its candidate screen improved same-boot p95
  by 3.0028996 percentage points but still had p95 102.9846293% and two drops,
  so it too was rolled back before screen 2 or the formal matrix. Evidence:
  `docs/test-reports/2026-08-09-milestone9-rollover-optimization-rejected.md`
  and `docs/test-reports/2026-08-09-milestone9-direct-anchor-finalization-rejected.md`.
- Comparative acceptance is fixed before the integrated run: paired arms use
  one warm-up plus at least ten one-minute clips and at least 1 Hz samples.
  Each clip must deliver at least 29.9 fps; drops/restarts may not increase;
  mean process CPU delta is capped at 35 percentage points with overlay p95 at
  100%; mean RSS delta is capped at 16 MiB with 32 MiB maximum within-arm
  growth; non-zram swap remains absent and within-arm zram growth is capped at
  4 MiB; throttle/undervoltage remain zero and temperature stays at or below
  80 C. Product specification Section C1 is authoritative.
- The native element begins in true GStreamer passthrough and returns to
  passthrough when overlay text is disabled or a write fault latches
  isolation. Enabled operation may still require `GstBaseTransform` to make a
  non-writable upstream buffer writable; the exact-Pi comparison must measure
  this cost and record the observed per-buffer video metadata/memory layout.
  A write failure can leave one partially modified frame before isolation;
  version 1 preserves recording continuity, reports the bounded failure, and
  passes all later frames through.

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
  monotonic clip windows at no more than 600 samples per minute. If a trusted
  anchor exists at finalization, the recorder writes the canonical anchored
  pair through its ordinary durable `FINALIZE` intent; otherwise the schema-4
  reconciliation worker later projects eligible provisional clip and sample
  times into UTC/local time, atomically replaces canonical sidecars, and
  no-replace renames pairs while preserving stable UUIDs. The direct-anchor
  candidate established this functional path but remains resource-rejected.
  The approved image keeps stock
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
  Schema 5 additionally contains one UUID-and-capacity-bound threshold latch.
  Before pending-pair reconciliation or camera/backend opening, the runtime
  samples free (`f_bavail * f_frsize`) and total (`f_blocks * f_frsize`) space
  from the same verified recording-volume descriptor, rejects identity/capacity
  drift, and durably applies the configured low/high hysteresis. Equality is
  deliberate: reclaim begins only below the low threshold, remains active below
  the high threshold, and emergency begins only below its threshold; a
  classified no-space write forces emergency. The present monitor provides a
  bounded, privacy-safe advisory directive only (`reclaimer_enabled=false`):
  it has no clip enumeration, unlink, or protected-media authority. A stale or
  invalid bounded observation budget, latch failure, or recording-volume
  `Gst.ResourceError.NO_SPACE_LEFT`/`ENOSPC`/`EDQUOT` leads to a clean
  `STORAGE_SAFETY_STOP` and `FAULTED/STORAGE_FAULT`, deliberately exiting
  successfully so `Restart=on-failure` cannot create a camera loop. A future
  durable `DELETING` transaction must consume that directive, coordinate
  protection and leases, and reconcile it before any media deletion. Current
  storage preflight still refuses `free <= minimum_free_gib`, so it must be
  integrated with this recovery path before low-space restart behavior can be
  accepted. This local source slice has no exact-Pi validation and is not an
  accepted release while Milestone 9 resource qualification remains open.
  Runtime snapshot schema 3 carries the bounded monitor status.
- Overlay formatting consumes one coherent telemetry snapshot and emits bounded
  text/layout data. The first pre-encoder `textoverlay` source slice bound
  initial state before PLAYING and used a queue-free optional worker, but its
  exact-Pi production release is rejected and disabled. Native-NV12
  fixed-region production integration is pending.
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
includes exact-Pi overlay qualification, retention/protection, actual socket
ownership, HTTP serving, preview, and the privileged removal helper.

## Structured logging convention

Application logs are one bounded JSON object per line with UTC timestamps and
stable component/boot/clip context fields. Structured values are accepted only
through an explicit event-data mapping; arbitrary record extras are ignored.
Sensitive key names and common `key=value` secret forms are redacted, strings and
collections are bounded, and exception text is excluded by default. Callers must
still never place credentials in free-form messages.
