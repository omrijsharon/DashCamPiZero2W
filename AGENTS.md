# AGENTS.md

## Current gate

- On 2026-07-25 the owner replaced the immediate custom-image build with an
  **SSH-first development deployment**. Use an official Raspberry Pi OS Lite
  32-bit Trixie flash customized by Raspberry Pi Imager for the declared
  hostname, user, home Wi-Fi, and SSH public key.
- Before that image's first Pi boot, remove exactly one standalone stock
  `resize` token from FAT `cmdline.txt` and preserve every other byte/token.
  The authorized card was checked on the Windows host as an MBR,
  31,457,280,000-byte device with a 512 MiB FAT32 `bootfs` p1 and
  4,161,536-sector ext4 p2. The edit was read back with zero remaining
  standalone `resize` tokens and the Windows volume cache was flushed.
- Do not install large dependencies into the stock approximately 2 GiB root.
  After the first SSH login, transfer only the minimal, reviewed provisioning
  payload. Adapt the exact-card Stage A contract to grow stock p2 directly to
  6 GiB and create p3; validate a dry run and exact CID/layout immediately
  before the authorized transaction. Stage B then grows ext4 online, formats
  only the newly proven p3 as exFAT `DASHCAM`, configures its verified mount,
  and completes before full application/dependency installation.
- The SSH-development contract uses the distinct boot marker
  `dashcam.bootstrap=ssh-dev-v1`. Add exactly one marker to FAT `cmdline.txt`,
  preserve the absent stock `resize` token and every unrelated token, read it
  back, and perform a preparatory reboot before the final Stage A dry run.
- Stock Imager writes do not sanitize later unallocated sectors. For journal
  schema 2, stream-hash exactly 4 MiB at the future p3 start before Stage A,
  bind the hash to the journal, and require the identical hash plus no
  `blkid`/`wipefs` signature after Stage A and before format intent. Arbitrary
  hash drift or a recognized filesystem latches refusal. Do not zero raw
  sectors merely to manufacture blankness. The deferred release-image schema
  1 contract retains its independently authored all-zero-prefix requirement.
- On the current pinned Trixie image, cloud-init terminates with one exact
  recoverable missing-`cc_netplan_nm_patch` warning and exit 2. Only the
  SSH-development contract may accept the exact reviewed machine-readable
  `done_known_degraded` shape; any additional/different warning or error
  remains non-ready, and the release-image contract still requires clean
  `done`.
- Add and maintain one repeatable, idempotent Pi installation entry point for
  the repository, locked Python environment, OS/media dependencies, config,
  and systemd units. Copy or clone the full repository only after controlled
  storage provisioning leaves sufficient verified root space.
- The reviewed live-Pi application path is `deploy/ssh-dev-app`. Build its
  hash-closed bundle outside the working tree with the exact `uv.lock` tzdata
  wheel. Explicitly refresh APT indexes before the authoritative dry-run, then
  never refresh between the saved plan and apply. Apply is exact-version,
  no-upgrade, bounded, and must be repeated through a new dry-run to prove
  idempotency.
- The installed exact-Pi release is the hash-closed final-safe Milestone 9
  candidate `0.1.0.dev0-5f95dd806342ac9e`. Its manifest, `SHA256SUMS`, and
  wheel SHA-256 values are respectively
  `619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655`,
  `a42983edbf0c85acc44609c7961fe48ab9847ff03d339ab05e8c40dbed1c24c8`,
  and `12761d42144abf776868582d2b6308de5a497e2b8df9ab873bb4fa7617cd7e98`.
  Exact-version apply and the separate idempotent plan/apply passed with zero
  package changes and no service starts; `dashcamd` remains enabled and
  inactive. The rejected
  `textoverlay` candidate remains historical (about 10.4 fps; stock
  `gdkpixbufoverlay` about 18.3 fps). The recorder-owned native NV12/DMABUF
  overlay is implemented and its v7 runtime-only active GPS/audio/hardware
  H.264/exFAT probe advanced 2,103 frames with zero drops, restarts, renderer
  failures, sync failures, or throttle. Its 75x1-Hz resource measurement had
  mean CPU 97.3591% but p95 100.9876%, which strictly fails the Section C1
  100% maximum. Do not round, waive, or mark the Milestone 9 resource/exit
  gates passed. A separate 30-sample post-install diagnostic had mean CPU
  96.9534% and p95 99.9871% with zero drops/restarts/failures/throttle, but is
  too short to override the repeated 75-s failures or the unrun paired
  ten-clip matrix; its privacy-safe result SHA-256 is
  `597c394e8604aaa8ca6facc905903df1a8d0c601db4c89997968a014b14ba27e`.
  The v7 wheel SHA-256 is
  `12761d42144abf776868582d2b6308de5a497e2b8df9ab873bb4fa7617cd7e98`, from
  Git `d4741f9`; that exact wheel is now the installed release. GPS-only phase 2/3/final
  probes reduced CPU from 10.4467% to 8.3741%/7.8989% and 5.7699% with 100 ms
  coalescing while retaining about 10 Hz valid throughput. An identity-only
  overlay cache is unsafe because a DMABUF identity does not bind per-buffer
  `GstMemory` geometry/video metadata; retain per-buffer validation. Evidence:
  `docs/test-reports/2026-08-03-milestone9-overlay-resource-limit.md`.
  On 2026-08-09, same-boot v2 evidence on the verified Pi established a
  substantially cooler accepted `5f95` baseline (mean 85.4950%, p95 92.9883%)
  and identified `task0` at 43.4711% with renderer work averaging about
  0.544 ms/frame. The hash-closed fused per-frame-validation candidate from
  Git `a9871cf` (release `0.1.0.dev0-850b8609266e3aaf`, manifest
  `251294bd4d9bdb295a16a46a4f23df25cc17b25848462c86c03c73e999a6860d`,
  `SHA256SUMS`
  `c6b6b36cb66ca79453e178d532d68862a5da542a11b2b2b0366963667fc365f8`,
  wheel
  `b90a707b683ed81fda0545654f88208503400b8762b357b1212fcba2f9f69e18`)
  applied and applied
  idempotently with zero package changes/no service starts, but is rejected:
  its one allowed 75-second run had p95 93.9872%, a 0.9989 percentage point
  same-boot regression rather than the required 2 percentage point improvement,
  and an encoder-input-PTS-gap drop had already occurred at the first
  post-warm-up observation. Do not run candidate two or the formal matrix;
  rollback through the verified `5f95` bundle restored the accepted dormant
  release, exact managed config hash, and zero-restart state. The candidate
  installed directory was recoverably removed; its bundle, Git commit, and
  privacy-safe evidence remain. Evidence:
  `docs/test-reports/2026-08-09-milestone9-fused-validation-rejected.md`.
- The Milestone 9 functional overlay gates passed on the verified `.112` Pi
  with the accepted installed `5f95` release. Hash-closed harness manifest
  `e38b54ea71268f1cd82a50b1a2ef85891ac68c9a5124599e2d37ef2bd88f4ff5`
  produced privacy-safe result SHA-256
  `3f60f366555869c913be66e5535f22b9c62d69923beeb0abf15dc12cb025ef97`.
  Five actual on-Pi decoded crops proved burned `TIME UNSYNCED`, valid
  navigation/time, `GPS LOST` with navigation hidden on both sides of an exact
  adjacent clip boundary, and recovered valid state in the successor. The two
  canonical sidecars and selected frame PTS proved a shared GPS producer and
  stable-anchor/monotonic model; never describe this as literal Python snapshot
  identity. Sequences 46/47 had actual packet rates 30.005668/30.005591 fps,
  zero live drops/restarts/renderer failures/throttle, and clean pre-analysis
  service stop. Their sidecar `frames_written` observer values did not equal
  MP4 packet counts (`1799/1800` and `1799/1771`); retain this truthful
  diagnostic and do not claim counter equality. It does not waive or complete
  the still-open Section C1 paired resource matrix or Milestone 9 exit gate.
  Evidence: `docs/test-reports/2026-08-09-milestone9-functional-overlay-live.md`.
- The last M8-qualified exact-Pi deployment ran hash-closed release
  `0.1.0.dev0-921164f96ad53e0b`; its manifest, final `SHA256SUMS`, and wheel
  SHA-256 values are respectively
  `2421ce2595815814c6de91c0ae55f8c5ca4a9f5dc05871caafcad34be81264f6`,
  `9e77ac1a7a71194b8b2864e73016e860b4c6b4d316bf44c2603846766df13eca`,
  and `09714c688566ef45338e2ac20056d653afc71356250b81ce972b6ad9c219b0ea`.
  Exact-version plan/apply and a second idempotent plan/apply passed with zero
  package changes and no service starts. Root free space after final evidence
  cleanup was 3,084,972,032 bytes. Its
  declared direct packages are recorded in
  `/var/lib/dashcam/app-install-v1.json`; `dashcam-storage-check.service` is
  enabled, completed successfully, and reached `READY`.
  `dashcam-network-fallback.service` is installed and enabled but inactive and
  has never been started by systemd.
  At the accepted Milestone 8 handoff `dashcamd.service` was installed and
  enabled; its final ordinary-recorder run ended cleanly with status 0 and it
  was inactive. The newer Milestone 9 safety state above supersedes that
  historical service state.
  A later bounded no-GPS reboot/restore validation left the Pi on boot ID
  `0c5464fe-25a1-4a76-973f-e73d38287e06`; the exact managed config hash was
  restored, its temporary backup paths are absent, and the recorder was again
  stopped cleanly.
  The GPS UART is restored to mode `0660`.
  Web/prepare-removal units
  remain absent until their production entry points and lifecycle contracts
  exist. The USB microphone is currently connected. Stable identity, native PCM,
  non-silent signal, standalone AAC-LC, and ten consecutive integrated A/V
  clips pass on the final image. Production logical microphone-loss isolation
  and bounded three-slot restoration are enabled. Milestone 7 is complete:
  startup without the microphone produced truthful video-only media. Physical
  hot-unplug/replug, runtime restoration, naturally reassigned-index, and
  physical wrong-device qualification are outside current v1 acceptance and
  must not be initiated. Input-gain
  calibration also remains open because
  the quiet-ambient test observed the minimum capture step with AGC enabled.
- The UART-qualified predecessor release was
  `0.1.0.dev0-d72d3067350d3552`. Its hash-closed bundle manifest was
  `c79723...ca05`, `SHA256SUMS` was `38a330...cd1c`, and wheel was `0c40f2...ab9`;
  initial apply and byte-idempotent repeat made zero package changes and
  started no service. The final PL011 Linux adapter is receive-only,
  nonblocking raw 115200 8N1 with `VMIN=1`/`VTIME=0`; unexpected empty reads
  fail boundedly and the ninth selector-ready no-progress wakeup forces GPS
  reconnect/backoff. The 72-second live run exited 0 with `NRestarts=0`, no
  throttling, `RECORDING`/storage `READY`/hardware H.264/audio `MATCHED`, and
  GPS `NAVIGATION_VALID` (latest GN GGA, nine satellites). It recorded 706,122
  bytes, 12,807 lines, 1,339 valid sentences/fixes, 102 checksum failures,
  6,763 unsupported sentences, and one recovered transport
  error/disconnect/reconnect with zero pipeline restart. A subsequent
  privacy-safe 120-second receive-only comparator accepted 1,199 GGA and
  1,200 active RMC records, 12 satellites, two checksum failures, and zero
  malformed records; coordinates were not retained. Releases `bc1`
  (`VMIN=0`: four watchdog restarts, about
  38 CPU-seconds/20 seconds) and `959c` (`b""` selector retry: about 150% CPU
  and status starvation) are rejected historical failures. Clips 381/382 are
  finalized A/V media, but their sidecars remain intentionally `UNSYNCED` with
  `gps.available=false`: those clips predate anchor integration, and sidecar
  integration remains open.
  Evidence: `docs/test-reports/2026-07-28-milestone8-gps-uart-live.md`.
- Hash-closed release `0.1.0.dev0-75947a15db03f4b3` added the production
  RMC/ZDA anchor adapter and configured plausibility, continuity, conflict,
  reacquisition, interval, provenance, and uncertainty policy. On the exact Pi
  it accepted one GN RMC monotonic/UTC anchor followed by 199 continuity
  confirmations with 250 ms uncertainty, zero anchor rejection, zero media
  drops/restarts, clean service shutdown, and `throttled=0x0`. Runtime status
  contains no coordinates. That predecessor did not yet feed anchors to
  sidecars or filenames, and no recorder release feeds them to the system
  clock. Evidence:
  `docs/test-reports/2026-07-28-milestone8-gps-anchor-live.md`; accepted status
  SHA-256
  `499ce4fb37fd40c93ec10da3ccd57052087185dbb17681faee722c24370293d3`.
  A bounded follow-up on that release `0.1.0.dev0-7fd1e73debb731b6`
  remained `GPS_TIME_VALID` with one accepted anchor, 183 confirmations,
  zero media/service restart, and clean shutdown, but also exposed 17
  transient policy rejections whose individual reasons are not retained in
  the bounded status. Preserve that diagnostics gap for the remaining GPS
  fault matrix; do not describe the follow-up as an error-free NMEA run.
- Hash-closed release `0.1.0.dev0-7fd1e73debb731b6` adds the bounded
  receiver-epoch-coalesced telemetry history and half-open clip windows.
  Sequence 388 retained exactly 600 unique, ordered monotonic-only GPS samples;
  shutdown sequence 389 retained 431 with zero overlap. Runtime observed 1,601
  valid navigation sentences, 801 emitted samples, 800 RMC/GGA coalesces, and
  zero rate-limit loss, eviction, monotonic/source-time regression, media drop,
  pipeline restart, service restart, or throttling. Those predecessor sidecars
  intentionally remain provisional with null UTC; the current release now
  performs durable anchor/name reconciliation. The
  earlier safe release `ce80` retained only 469 samples in a full clip and is
  refused for native cadence. No coordinate-bearing Windows evidence copy was
  retained. Evidence:
  `docs/test-reports/2026-07-28-milestone8-gps-sidecar-live.md`; accepted
  privacy-safe result SHA-256
  `a168d8574a0f28bca1111a44955de9c501e4b70d77b461f884c49733bbeab9d9`.
- The approved image uses stock `systemd-timesyncd` version
  `257.9-1~deb13u1+rpi1` as the sole active Linux wall-clock owner; `chrony`,
  `ntp`, `ntpsec`, and `gpsd` are inactive or absent. A controlled +120-second
  realtime step during production sequence 390 was restored by the owner
  within the next 254,377,301 ns observation. All 1,800 H.264 packet PTS/DTS
  and 2,815 AAC packet PTS/DTS remained strictly increasing, full hardware
  video/audio decode passed, and drops, pipeline restarts, service restarts,
  and throttling stayed zero. The recorder does not set system time: GPS
  anchors are the canonical UTC-reconciliation source, and media timing
  remains pipeline/monotonic. No coordinate-bearing Windows evidence was
  retained. Evidence:
  `docs/test-reports/2026-07-28-milestone8-clock-step-live.md`; privacy-safe
  result SHA-256
  `ab3070953f3ac0796b7f7f25161a328789d81fa52b442e5d9840f25b14186a0b`.
- Hash-closed release `0.1.0.dev0-6f943f3a4edf7117` completes the accepted
  durable UTC/name reconciliation slice. Catalog schema 4 retains a canonical
  replacement sidecar plus expected provisional-source SHA-256 before
  device-bound, no-replace MP4/JSON moves. A bounded 64-UUID same-boot backlog
  attempts at most two entries per fragment. Exact-Pi late lock reconciled
  provisional sequences 398/399 from one later trusted RMC anchor, kept their
  stable UUIDs, invented no historical navigation, retained AAC, passed full
  hardware H.264/audio decode, and produced zero drops/restarts/throttling.
  Result SHA-256:
  `cb0177a9a19754fcf2c881df47f39db2790abf770032df8bff9388c8d3ca1b20`.
  An isolated exact-exFAT case-variant collision refused before source,
  separate catalog, or intent mutation; the production catalog hash was
  unchanged and the fixture was removed. Collision result SHA-256:
  `0c4cea7c475b03127d5fe2208dd36ebe9ff25ec3e35f30ba1d45f6e07c343d1f`.
  Evidence:
  `docs/test-reports/2026-07-28-milestone8-reconciliation-live.md`.
- Milestone 8 is complete. Privacy-safe exact-Pi production-wheel result
  `m8-gps-fault-matrix-result-73.json` proved silence and transport loss clear
  navigation while retaining only `GPS_TIME_STALE`, bounded reconnect restores
  navigation only after a valid fix, malformed/checksum/unsupported/oversized
  input remains bounded, implausible UTC and anchor conflict refuse without
  replacing the accepted anchor, and UTC-midnight plus `Asia/Jerusalem`
  DST/standard projections are correct. Result SHA-256:
  `84b63cd5bef42e4056e04263abef945f2440734cf8ac4ac6261ec94310c08040`.
  A stronger uncontested integrated run used the installed production daemon,
  real camera/hardware encoder, exFAT/catalog/reconciliation path, and a
  PTY-backed GPS source. It advanced 1 -> 2,179 encoded frames through silence,
  conflict, transport replacement, and recovered navigation with zero drops,
  pipeline restarts, service restarts, or throttling. Privacy-safe result
  `m8-fault-matrix-result-9ea3a712-passed.json` has SHA-256
  `a89df67c2c9735127bf360718738f9d1b09049bcb429034e25793b1f2be17ffd`.
  A controlled boot into the deliberately absent configured GPS path then
  reached ordinary `RECORDING` with `UART_UNAVAILABLE`/`UNSYNCED`, no current
  navigation, 2,106 encoded frames, zero drops/restarts, and storage `READY`.
  Boot ID was `0c5464fe-25a1-4a76-973f-e73d38287e06`; privacy-safe status
  SHA-256 is
  `f29ede9e2ccba2c4aa64608d14206ec4c00b2706b6a34bb0f88f2d7534136fec`.
  The normal configuration was restored byte-for-byte to SHA-256
  `1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`,
  the temporary backup was removed, and the final ordinary run stopped
  cleanly with status 0.
  The checked harness now acquires a nonblocking kernel lock before live work;
  concurrent validators must refuse before touching its transient unit, PTY,
  or camera.
  Final release `921164f96ad53e0b` adds consecutive and one-second parse-error
  rate guards; the actual M10 stream remained below the guard. Its one-minute
  ordinary-recorder sequence 418 passed full hardware H.264 and AAC decode at
  1080p30, with zero pipeline/service restart and `throttled=0x0`. Later
  transient fault-harness runs used synthetic anchors and must not be cited as
  physical-receiver UTC/filename evidence; sequences 398/399 remain the accepted
  late-lock reconciliation authority.
- Historical Milestone 9 predeployment state: the first local slice used
  `textoverlay`, and the full exFAT volume initially blocked deployment. The
  owner subsequently authorized the exact archived media/catalog reset and
  obsolete-root cleanup recorded in the current Milestone 9 report. That
  authorization was one transaction, not a general permission to delete later
  recording/evidence. The installed final-safe release and dormant enabled
  service state at the top of this file are authoritative.
- The earlier exact-Pi configured-GPS-absence simulation also passed on
  release `d72`: a temporary config used
  `/dev/dashcam-gps-deliberately-absent` while the physical module stayed
  connected, then `/etc` was restored to exact bundle hash `f7d25b...f6555`.
  At 15 seconds, `RECORDING` reported `UART_UNAVAILABLE`, zero GPS
  connections/bytes/lines/fixes, six bounded unavailable errors, storage
  `READY`, audio `MATCHED`, hardware H.264, and zero pipeline restarts. Final
  status retained recording/GPS-unavailable through nine bounded attempts;
  sequences 383 (60 seconds) and 384 (clean shutdown) preserved boot ID,
  sequence, and the exact monotonic boundary, with nullable UTC/local,
  `UNSYNCED`/`MONOTONIC_ONLY`, and `gps.available=false`. Shutdown returned
  success with zero service restarts/throttling. This is configured-device
  absence only, not physical unplug or reboot; the later accepted late-lock and
  privacy-safe production-wheel fault matrix close the remaining M8 cases.
  Evidence:
  `docs/test-reports/2026-07-28-milestone8-no-gps-live.md`.
- On 2026-07-28 the owner clarified the audio scope: the configured microphone
  is expected to remain connected in normal use, while the dashcam must operate
  when it is present or absent at recorder startup. Do not spend further work
  on physical microphone unplug/replug qualification. Stable USB identity and
  non-substitution remain required. The exact-Pi absent-startup run finalized
  sequence 368 as a 59.989-second, IDR-first, hardware-decoded 1080p High/4.1
  video-only clip at 8.004 Mbit/s with `audio.available=false`; storage stayed
  on verified exFAT, service restarts stayed zero, shutdown returned status 0,
  and throttling stayed `0x0`. Evidence:
  `docs/test-reports/2026-07-28-milestone7-absent-startup-live.md`.
- The 2026-07-28 final two-cycle logical loss/restoration qualification passed
  the real production recorder. Atomic generation-EOS reservation closes the
  late source-EOS race; an exact open-or-already-closed ownership proof closes
  the asynchronous fragment-observer race. Result 72 proved bounded three-slot
  recycling twice, five truthful clips with audio
  `[true,false,true,false,true]`, IDR-first hardware decode, A/V skews
  76.001/71.958/84.291 ms, zero drops/restarts/throttling, and direct use of
  the generation-EOS fallback. Evidence:
  `docs/test-reports/2026-07-28-milestone7-production-restoration-live.md`;
  ignored result
  `artifacts/pi-m7-20260727/m7-production-restoration-result-72.json`,
  SHA-256
  `e3f16644c6aac8bf847bbd12feeb931fa818b0320a1fa6eb1f9aed486fe2910b`.
  Earlier 124.666 ms and 100.666 ms runs remain refused; never weaken the
  strict 100 ms gate. This is logical sysfs loss/reconnect evidence only.
- The 2026-07-27 Milestone 7 connected-microphone baseline passed exact stable
  discovery, shared-clock/resampling graph negotiation, truthful AAC sidecars,
  ten independent H.264+AAC decodes, ten IDR starts, and nine exact-zero
  normalized boundaries. Stream-edge A/V skew was 4.000–64.333 ms with no
  systematic growth. Evidence is in
  `docs/test-reports/2026-07-27-milestone7-audio-live.md`; the accepted result
  SHA-256 is
  `8a4c103a99cae87a04f662ab02288944bf35fdd8b921e422bc0f95e905bd9436`.
  Recorder/AP are inactive. The hash-closed, non-mutating exact-Pi hotplug
  probe then refused direct `splitmuxsink` `audio_%u` release/re-request:
  GStreamer 1.26.2 exposes no public pre-switch drain/old-closure/new-mux
  barrier, and async finalization leaves a context race. It made zero camera,
  encoder, request-pad, service, network, or exFAT mutations. Evidence:
  `docs/test-reports/2026-07-27-milestone7-hotplug-refusal.md` (probe manifest
  `1374c5d664749ed685e59309f7bb8f3284525174af4264a02752695ea140275c`,
  result `d71c329f032472de8b26001ef4ccae3673dfe714e5d59bab4cbac2093ad51236`).
  Do not claim this probe proves hot-unplug survival; later immutable-generation
  evidence, not live request-pad mutation, is the basis for reconnect.
- The subsequent hash-closed immutable-generation capability harness passed
  twice unchanged on the exact Pi. Each run produced 3 A/V, 3 video-only, and
  4 restored-A/V clips through one continuous camera/hardware encoder; every
  clip was IDR-first and hardware-decoded, restored stream-edge skew remained
  below 100 ms, block/closure operations were bounded, and there were no
  warnings, errors, restarts, or throttle flags. Evidence:
  `docs/test-reports/2026-07-27-milestone7-generation-handoff-live.md`; ignored
  results are in `artifacts/pi-m7-20260727/`.
  This initial capability result proved only one connected-microphone
  programmatic A/V -> video-only -> A/V sequence using three preconstructed
  generations retained until parent `NULL`. The later production-restoration
  result above is the authority for dead-ingress rebuild, slot recycling, and
  repeated logical reconnect.
- The final normal-default production-loss qualification passed on the exact Pi
  with release `0.1.0.dev0-2439b9fc544ffffc`. Controlled deauthorization of
  the exact matched microphone exercised the enabled one-way A/V-to-video-only
  isolation: two stable `NOT_FOUND` confirmations, IDR-held immutable-generation
  handoff, truthful `UNAVAILABLE/microphone_loss_isolated`, two IDR-first
  hardware-decoded clips with audio `[true,false]`, no drop increase, and zero
  camera/pipeline restarts. The result is
  `artifacts/pi-m7-20260727/m7-production-loss-result-9.json`, SHA-256
  `8c113bc4bda8fcb1d5017fa63ad510b75ba7984ef8f88c0dc8810cb5485c2ffb`;
  report: `docs/test-reports/2026-07-27-milestone7-production-loss-live.md`.
  This is the historical one-way controlled-deauthorization baseline, not
  physical-unplug evidence. The later result 72 supersedes its restoration
  limitation. Physical hot-unplug/replug and related physical qualification are
  no longer M7 acceptance work; absent-at-startup and the M7 exit gate passed
  in `docs/test-reports/2026-07-28-milestone7-absent-startup-live.md`.
- The 2026-07-26 live Milestone 5 matrix passed storage-preflight refusal,
  disposable clean/repairable/failed `fsck.exfat`, real exFAT write/finalize,
  IMX219/NV12 plus Raspberry Pi hardware-H.264, receive-only GPS UART traffic,
  open-sky GPS fix/trusted UTC, USB microphone PCM/AAC capability, and
  home-Wi-Fi/local-route checks. Evidence is in
  `docs/test-reports/2026-07-26-milestone5-live-validation.md` and ignored raw
  text artifacts are in `artifacts/pi-validation-20260726/`. The final tested
  boot ID is `601693e3-fa96-427e-906b-1621463a15cd`; root had
  3,250,245,632 bytes free after fallback installation/validation.
- The current GPS fix/UTC gate passed after the antenna was moved into the
  open. A privacy-safe 120-second receive-only run reported 6–7 satellites and
  1,153 trusted active-RMC anchors; an independent 15-second repository-parser
  check accepted 150/150 RMC anchors with seven satellites and
  0.032–0.037-second system-minus-GPS observations. Coordinates were never
  retained.
- Keep `v4l2h264enc` as the selected and live-validated production recorder
  backend. Raspberry Pi's documented
  `repeat_sequence_header=1` control plus an explicit H.264 level cap passed
  bounded 640x360 and 1920x1080 IMX219 tests. The production-cap
  constrained-VBR variant also passed at 1920x1080/30, High/4.1, 8 Mb/s target,
  GOP 30, repeated headers, one-second keyframes, and independent decode. Its
  measured transport bitrate was 8,221,871 bit/s and the Pi remained
  unthrottled. Never omit the explicit level cap: the earlier level-1
  negotiation was invalid at 1080p. Never set `video_bitrate_mode=1` on this
  exact stack: that control alone reproducibly causes `STREAMON`/kernel
  `ret -3`; bitrate and GOP controls pass individually and together under the
  default hardware VBR mode. The product contract permits constrained VBR.
  The installed `dashcamd` passed one continuous 59.988667-second split into an
  IDR-started successor, independent bounded decode, zero restarts, clean
  post-rollover systemd shutdown, and no throttling. The exact stack accepts
  EOS and posts active `splitmuxsink-fragment-closed` but omits pipeline EOS;
  accept only EOS or the identity-validated active closure, never a stale prior
  closure. Use explicit `dash-or-mss` fragment mode; do not use
  `first-moov-then-finalise`, which failed the bounded shutdown contract.
  The exact-Pi finalization path now passed canonical sidecar readback,
  device-bound no-replace MP4+JSON promotion, case-collision refusal, clean
  shutdown-fragment promotion, and an injected one-member interrupted-pair
  recovery. The synthetic recovery MP4 is state-machine evidence only, not
  playable production media; preserved diagnostic pending outputs (including
  sequence `000011`) are not catalogued. Milestone 6 then passed truthful
  runtime metrics, one bounded camera/encoder recovery, ten independently
  decodable clips (sequences 30–39), and a 7,200-second endurance run. The
  accepted recovery evidence SHA-256 is
  `610258c6e41a6c2b70cfb1d65b37055f62b960c6cdff1bf3b812a951dc95a407`.
  The endurance pass is bound to source SHA-256
  `ce3bb587b15678a23028b2f82ff14ff19ecb50a71884e7310cdd7cd37931ad7f`
  and its strict zram-only/no-growth reanalysis SHA-256
  `4f0857f8106efcad691938628dbe5e1fc0b94a439683c2ea915a09852bc905e6`;
  the recorder stopped cleanly afterward. Evidence:
  `docs/test-reports/2026-07-26-gstreamer-explicit-caps.md`,
  `docs/test-reports/2026-07-26-milestone6-recorder-live.md`,
  `docs/test-reports/2026-07-26-milestone6-finalization-live.md`, and
  `docs/test-reports/2026-07-27-milestone6-metrics-recovery-endurance-live.md`.
  The acceptance-harness manifest SHA-256 is
  `2854a7e48b2607b6bdf6ccfad048d46d8ec4641ee967cc370380d9bade49cdb4`;
  its final media result SHA-256 is
  `3cb0a862e1bc4e63d4abe2c3cd671716bcaad7494d6bf0052aaed5b97aeefcf6`.
  The reverted
  128 MiB GPU-memory experiment remains historical evidence; final
  `config.txt` SHA-256 is
  `59efe771dfd2544a2a0eabe190559b70a3b210fc02f79a0f338e7ffb1286eeef`.
- AP fallback remains the only owner-assisted Milestone 5 test, and the owner
  explicitly deferred it on 2026-07-26. Do not initiate it until the owner
  resumes it; keep Milestone 5 open while proceeding with later milestones.
  The reviewed current-path service is installed and enabled without having
  started; its ordinary client path passed in 1.151 seconds with unchanged
  home Wi-Fi, absent AP artifacts, and SSH/NetworkManager active. Preserve a
  recovery path and coordinate the eventual deliberate AP
  activation/reconnection steps with the owner. Never activate AP
  unexpectedly over the sole SSH path.
- The custom compressed Bootstrap image and its pinned Trixie-class builder
  are deferred release-engineering work, not the current development
  prerequisite. Keep the existing builder code/evidence, but do not spend time
  resolving its container identities until the SSH-first Pi implementation
  and hardware gates pass.
- Stage A is an exact-identity-gated, one-write `sfdisk --no-reread`
  transaction followed by raw-MBR readback, durable commit, sync, and exactly
  one controlled reboot. Stage B runs on a different boot ID, revalidates the
  target, grows mounted ext4 online with `resize2fs`, verifies the contract-
  specific p3 provenance/signature gate and format intent, formats it once as
  exFAT `DASHCAM`, configures UUID mounting and the sentinel, and writes
  completion last. Foreign/torn/refused states latch: never auto-restore,
  auto-format, or destructively retry.
- The authorized card's SSH-first Stage A and Stage B passed. The exact layout
  is p2 6 GiB ext4 plus p3 24,468,520,960-byte exFAT `DASHCAM`, UUID
  `7EED-3EA7`, mounted at `/srv/dashcam`. Validate ext4 total size from bounded
  `dumpe2fs` block count multiplied by block size; `lsblk FSSIZE` is usable
  data capacity, not total ext4 geometry. After durable format intent, the
  exact normal exFAT `wipefs` signature shape is one `exfat` plus one `dos`
  boot signature; before format intent, blankness still requires no signature.
- Two live observer defects were recovered through separate exact-journal-hash
  and exact-live-identity-bound one-off helpers. Their refused journals and
  durable audits are retained. Never add a general force/unlatch path, reuse
  either helper for another refusal, or reformat the completed volume.
- Application deployment also exposed fail-closed observer defects before any
  unsafe apply: Windows short reads/text-mode wheel corruption, service
  ownership/traversal, Trixie APT simulation shape, the stock exact
  `/etc/os-release` symlink, and sysfs CID nominal size. Keep their dedicated
  strict readers/contracts and regression tests; do not weaken generic file or
  package-state gates. Evidence is in
  `docs/test-reports/2026-07-26-ssh-first-app-install.md`.
- Bootstrap services must run before storage verification/dashcam writes but
  independently of networking. Failure must leave NetworkManager, SSH, and AP
  fallback available; `dashcamd` reports `STORAGE_FAULT` and must not write
  until the verified exFAT mount is present.
- Every boot tries configured home Wi-Fi for at most 60 seconds. Association
  plus a local route is success; internet is not required. On failure, use a
  stable NetworkManager AP at `192.168.50.1/24`, SSID
  `Dashcam-<short-device-id>`, and a unique WPA secret until reboot or explicit
  retry. Never oscillate between client and AP modes.
- The owner authorized this official Lite flash, the pre-first-boot cmdline
  edit, SSH access after the Pi is powered, and continued installation work on
  the exact expendable 31,457,280,000-byte card with CID
  `fe34325344000000200000031a0192d1`. Destructive partitioning remains limited
  to that card, the reviewed stock-to-6-GiB contract, and an immediately
  preceding exact identity/layout preflight. General-release destructive
  authorization remains unresolved and must be explicit.
- The current Pi is reachable as `dashcamadmin@192.168.68.112`; the verified
  Wi-Fi MAC is `2c:cf:67:98:4c:49`, board serial is
  `00000000db28ffe4`, and pinned SSH ED25519 fingerprint is
  `SHA256:iNlz0NDhUbn+GfH5Nbb5v9nImSX+zFujVDSqvcHSMOg`. Use the ignored
  project-scoped `artifacts/pi-ssh-known-hosts` with strict host-key checking.
  `192.168.68.107` is foreign (MAC `88:a2:9e:84:b3:a5` and a different SSH
  key) and must be refused rather than treated as a replacement Pi.
- The exact Pi evidence remains the target contract: Raspberry Pi OS Lite
  32-bit Trixie; IMX219 `libcamerasrc`/NV12; `/dev/video11` hardware H.264;
  fragmented `splitmuxsink`/`mp4mux`; PL011 `/dev/ttyAMA0` with Bluetooth
  disabled; M10 Mini GPS receive-only at 115200; and USB audio identity
  `08bb:2902` selected by USB identity plus physical path. Do not hard-code
  media-node numbering. Details and measured limits are in
  `docs/architecture.md` and the 2026-07-24 test reports.
- The reference Pi serial is `00000000db28ffe4`. The reference supply is an
  unspecified regulated 5 V / 2.5 A source; there is no hold-up/safe-shutdown
  controller. Retain that power-loss risk explicitly.
- **Historical pointer:** v1-v4 are retired. Their offline/build/forensic
  evidence remains in `docs/test-reports/2026-07-24-*`,
  `docs/test-reports/2026-07-25-authorized-exact-card-image-v2-failure.json`,
  `docs/test-reports/2026-07-25-authorized-exact-card-image-v3-failure.json`,
  and `docs/test-reports/2026-07-25-authorized-exact-card-image-v4.json`.
  V4 was flashed and powered, then observed for more than ten minutes with no
  expected MAC, hostname, IP, or SSH availability. No SSH session or
  laptop-initiated Pi/storage mutation occurred during that observation. Its
  post-boot card state was not captured, so do not claim a partition result or
  exact V4 failure cause. The raw v4 artifact was deleted by the owner; do not
  flash that architecture again.
- Keep every target-dependent choice provisional until saved evidence exists
  for this exact Pi/image. Local fixtures/fakes are logic evidence only.

## Source of truth

1. `Pizero_dashcam_PROJECT.md` is the product and acceptance contract.
2. `plan.md` is the ordered execution checklist and progress record.
3. This file defines repository working behavior.

If they conflict, stop, preserve evidence, and resolve the documents explicitly
instead of silently choosing one.

## Product summary

Build an autonomous Raspberry Pi Zero 2 W dashcam that continuously records
1080p30 hardware-H.264 one-minute MP4 clips with optional USB-mic AAC, UART
GPS time/navigation, burned-in telemetry, JSON sidecars, protected events,
exFAT ring retention, a secured local AP/web UI, low-latency preview, and
controlled shutdown. Recording reliability outranks every optional subsystem.

## Non-negotiable invariants

- `dashcamd` is the only camera owner; never open the camera from
  preview/web/helpers.
- Keep camera/encoder continuous across ordinary segment boundaries and split
  on closed-GOP IDR/keyframes.
- Never use software H.264 for the production 1080p30 profile or silently
  lower required settings.
- Use pipeline/monotonic time for media; use trusted GPS anchors for UTC and
  IANA zones for display.
- Start without GPS or microphone, but never record without a verified writable
  exFAT `DASHCAM` mount at `/srv/dashcam`.
- Never fall back to the root filesystem and never auto-format a failed/unknown
  volume.
- Treat MP4+JSON as a recoverable logical pair, not an atomic pair; use durable
  intent and idempotent reconciliation.
- Keep every queue, retry, lease, log, recovery pass, and shutdown step bounded.
- Optional GPS/audio/AP/web/preview failures must not terminate or backpressure
  recording.
- Protect secrets, reject path traversal, keep the web process unprivileged,
  and use a narrow shutdown/time helper.
- Hardware/performance claims require measurements on the exact Pi/image; local
  mocks are not evidence.
- exFAT power-loss behavior is a tested target, never an absolute
  data-integrity guarantee.

## Work routine

1. Read this file, the relevant specification sections, and the active
   milestone in `plan.md`.
2. Inspect `git status`; preserve user changes and do not overwrite unrelated
   work.
3. Work on the smallest unchecked task that advances the active milestone.
4. Add or update tests with the change; validate failure paths and bounds, not
   only the happy path.
5. Run proportional local/hardware checks and save evidence where the plan
   requires it.
6. Check a task only after its validation passes. Add a concise evidence
   path/note when useful.
7. Check a milestone only when all nested tasks and its exit gate are checked.
8. Leave blocked, mocked-only, flaky, or unmeasured tasks unchecked and state
   why.
9. Keep `plan.md`, architecture/config/API/schema docs, and the specification
   synchronized with accepted changes.
10. When proposing “the next step,” continue and perform it in the same turn if
    it is within the current authorization, can be done by the agent, and does
    not require the owner's physical action, a new owner decision, additional
    authority, or a destructive-operation approval. Do not stop after merely
    suggesting work that the agent can safely complete autonomously.

## Delegation and context discipline

- The main agent should conserve its context window by delegating concrete,
  bounded, independently reviewable subtasks when delegation costs less context
  than doing the work directly.
- Use a `gpt-5.6-terra` agent with **high** reasoning for straightforward
  execution such as focused repository inspection, isolated mechanical changes,
  test-case implementation, or documentation updates with clear acceptance
  criteria.
- Use a `gpt-5.6-sol` agent with **high** reasoning for complex debugging,
  architecture, cross-component analysis, ambiguous failures, or work that
  requires resolving substantial technical problems.
- Give each agent the minimum sufficient context, exact scope, constraints,
  expected artifacts, and validation criteria. Prefer parallel agents only for
  independent work with no overlapping file ownership.
- Before spawning parallel work, maintain a small orchestration map of task to
  agent, owned files/directories, dependencies, expected output, and integration
  order. Do not assign overlapping writes concurrently.
- Treat delegated file ownership as exclusive while that agent is active. If
  tasks become coupled, contracts drift, or user changes touch owned files,
  pause and re-plan ownership.
- Require agents to inspect current repository state, preserve user/other-agent
  changes, stay within scope, and report changed files, validation commands,
  assumptions, and unresolved issues.
- The main agent owns integration: inspect every returned diff, re-read affected
  interfaces, run relevant cross-component checks, resolve conflicts, and never
  check a plan task solely because a sub-agent reports success.
- Do not delegate trivial/tightly coupled work when handoff costs more context,
  or owner decisions, authorization gates, destructive hardware actions, and
  unresolved scope choices.

## Implementation discipline

- Continue through the authorized SSH-first live-Pi route. Do not reflash,
  repartition, reformat, or alter the completed storage layout without a new
  exact destructive preflight and authorization.
- Probe actual device nodes, plugins, caps, OS architecture, UART mapping, and
  muxer behavior; do not hard-code laptop assumptions.
- Prefer typed Python for the control plane and native camera/media components
  for frame movement/encoding.
- Avoid full-frame Python processing, unbounded in-memory telemetry, per-frame
  logs, and SD-card swap dependence.
- Keep lifecycle state separate from protection/download attributes and
  subsystem states.
- Use stable UUID clip IDs; filenames are Windows-safe human labels and must
  never overwrite on collision.
- For destructive storage work: resolve the exact target, support dry-run and
  refusal paths, back up the partition table, and use expendable media.
- Never hide a failed acceptance gate. Document the measurement, impact,
  options, and requested decision.

## Definition of a finished task

A task is finished only when its requested artifact exists, relevant checks
pass, failure behavior is covered, no required evidence is missing, and the
plan checkbox has been updated. A hardware-tagged task additionally requires
saved Pi/Windows measurements from the declared reference setup.
