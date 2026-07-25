# DashCam Pi Zero 2 W implementation plan

Last updated: 2026-07-24

This is the execution checklist for `Pizero_dashcam_PROJECT.md`. The product specification is the acceptance contract; this plan orders the work and records verified progress.

## Checklist rules

- Check a task (`[x]`) only after its validation is complete and the evidence is saved in the repository or linked test report.
- Check a milestone only after every task beneath it, including its exit gate, is checked.
- Local fixtures and mocks can prove local logic, but they cannot prove Pi hardware, performance, provisioning, power-loss, or Windows compatibility.
- Keep target-dependent decisions provisional until measured on the exact Raspberry Pi OS image and Pi Zero 2 W.
- Record deviations in `docs/architecture.md` or a short architecture-decision record with the reason, evidence, risk, and remedy.
- Tags: **LOCAL** = development machine; **PI** = Pi required; **WIN** = Windows host required; **DESTRUCTIVE** = expendable-card/destructive operation.

## Milestone 0 — Specification approval and implementation authorization

- [x] **Milestone 0 complete**
  - [x] **LOCAL:** Review the full product specification for feasibility, contradictions, and missing reliability constraints.
  - [x] **LOCAL:** Correct `Pizero_dashcam_PROJECT.md` without writing implementation code.
  - [x] **LOCAL:** Create this checkbox-based implementation plan.
  - [x] **LOCAL:** Create repository-specific `AGENTS.md`.
  - [x] Owner reviews the revised specification and plan.
  - [x] Owner explicitly authorizes local implementation to begin (2026-07-24).
  - [x] Exit gate: implementation scope and unresolved product choices are accepted as provisional pending the later Pi capability gate.

## Milestone 1 — Local repository foundation

- [x] **Milestone 1 complete**
  - [x] **LOCAL:** Add `README.md`, license decision, Python/package layout, and supported Python version.
  - [x] **LOCAL:** Add `pyproject.toml` with pinned or bounded runtime/development dependencies.
  - [x] **LOCAL:** Configure formatter, linter, type checker, and unit-test runner.
  - [x] **LOCAL:** Add CI for formatting, linting, type checks, unit tests, and artifact retention.
  - [x] **LOCAL:** Create the planned source, test, config, script, deploy, systemd, network, and docs directories.
  - [x] **LOCAL:** Add application/build version reporting and a reproducible version source.
  - [x] **LOCAL:** Define machine-readable test-result and capability-report formats.
  - [x] **LOCAL:** Add bounded structured logging conventions with secret redaction.
  - [x] **LOCAL:** Document the development workflow and which tests require Pi/Windows hardware.
  - [x] **LOCAL:** Run the complete local quality suite from a fresh isolated source copy; a literal Git checkout awaits the initial commit.
  - [x] Exit gate: the minimal package imports/builds, the CI-equivalent suite passes, and no target-hardware capability is claimed. Evidence: `docs/test-reports/2026-07-24-milestone1-local.md`.

## Milestone 2 — Local domain model and hardware-independent logic

- [x] **Milestone 2 complete**
  - [x] **LOCAL:** Define versioned TOML configuration models, defaults, validation, atomic update, and migration behavior.
  - [x] **LOCAL:** Keep AP/session secrets outside world-readable configuration and redact them from reads/logs.
  - [x] **LOCAL:** Define separate recorder, storage, time, GPS, audio, and device-operation state models.
  - [x] **LOCAL:** Define clip lifecycle, orthogonal protection flag, bounded download lease, and stable UUID clip identity.
  - [x] **LOCAL:** Implement Windows-safe provisional/final filename generation with boot-ID and sequence collision protection.
  - [x] **LOCAL:** Define the versioned sidecar schema, including nullable UTC fields and time-anchor provenance.
  - [x] **LOCAL:** Build NMEA fixtures for valid, bad-checksum, malformed, mixed-talker, stale, invalid-fix, ZDA, RMC, and GGA cases.
  - [x] **LOCAL:** Implement/test sentence-specific NMEA trust rules and bounded input handling.
  - [x] **LOCAL:** Implement/test monotonic-to-UTC anchoring, plausibility checks, conflict handling, and IANA timezone conversion.
  - [x] **LOCAL:** Unit-test `Asia/Jerusalem` around both daylight-saving transitions, UTC midnight, and local-date rollover.
  - [x] **LOCAL:** Implement/test pure retention selection and low/high/emergency threshold calculations.
  - [x] **LOCAL:** Model durable intents and idempotent reconciliation for finalize, rename, protect/unprotect, and pair deletion.
  - [x] **LOCAL:** Define versioned public/internal API schemas, stable error codes, and authentication/CSRF expectations.
  - [x] **LOCAL:** Add property/fuzz tests for filenames, paths, configuration boundaries, NMEA input, and state transitions.
  - [x] Exit gate: all hardware-independent logic passes locally with bounded-input tests and no direct device access. Evidence: `docs/test-reports/2026-07-24-milestone2-local.md`.

## Milestone 3 — Local deployment, diagnostics, and test harnesses

- [x] **Milestone 3 complete**
  - [x] **LOCAL:** Author a read-only Pi capability probe covering OS/kernel, architecture, memory, camera, encoders, media stacks, audio, UART, filesystems, thermals, throttling, and undervoltage.
  - [x] **LOCAL:** Make the probe report raw evidence plus a machine-readable summary; it must not alter the Pi.
  - [x] **LOCAL:** Author media validation tooling for streams, codecs, decodability, IDR starts, duration, normalized boundary continuity, bitrate, and A/V skew.
  - [x] **LOCAL:** Author endurance monitoring for RSS, available memory, swap, CPU, temperature, throttling, frame drops, bitrate, and service restarts.
  - [x] **LOCAL:** Draft a declarative partition layout and a non-destructive layout verifier.
  - [x] **LOCAL:** Draft provisioning logic with dry-run output, device-identity invariants, idempotency markers, and explicit refusal cases.
  - [x] **LOCAL:** Draft installation, upgrade, rollback, recovery, and log-collection commands.
  - [x] **LOCAL:** Draft `systemd` units with ordering, restart backoff, watchdog notification, privilege restrictions, and bounded shutdown.
  - [x] **LOCAL:** Draft NetworkManager AP configuration without a universal password.
  - [x] **LOCAL:** Add scripts/test instructions for power-loss recovery, small-filesystem retention, and Windows interop.
  - [x] **LOCAL:** Review every destructive command path and ensure dry-run/refusal behavior is testable without a block device.
  - [x] Exit gate: tools are locally tested against fixtures; destructive tooling has not been run and hardware results remain unknown. Evidence: `docs/test-reports/2026-07-24-milestone3-local.md`.

## Milestone 4 — Pi access and capability decision gate

- [x] **Milestone 4 complete**
  - [x] Obtain owner authorization for Pi access and the connection details; do not SSH before this task is approved. Authorized 2026-07-24; evidence: `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Record Pi model/revision, camera model, GPS model, microphone identity, card model/capacity, power supply, and power controller. Reference supply is an unspecified-model regulated 5 V / 2.5 A unit; no hold-up/shutdown controller exists. Evidence: `docs/test-reports/2026-07-24-milestone4-progress.md` and `docs/test-reports/2026-07-24-pi-gps-audio.md`.
  - [x] **PI:** Record the exact Raspberry Pi OS Lite image/release date, 32/64-bit architecture, kernel, packages, and boot configuration. Evidence: `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Run the read-only capability probe and save its full report. Evidence: `docs/test-reports/2026-07-24-pi-capability-post-uart.json`.
  - [x] **PI:** Compare 32-bit and 64-bit image memory/media compatibility if the release choice is not already proven. The exact 32-bit image proved the required media path with resource margin; re-open only if a dependency or endurance result invalidates it.
  - [x] **PI:** Probe camera modes, raw/ISP stream formats, buffer paths, and hardware H.264 encoder controls. Evidence: `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Prove 1920×1080 at 30 fps hardware H.264 at the target bitrate with no software fallback. Evidence: direct `/dev/video11` ownership, PTS, and validated MP4 artifacts in `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Probe SPS/PPS repetition, IDR request/interval control, supported H.264 profiles/levels, and closed-GOP behavior. Evidence: controls plus IDR-starting fragmented segments in `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Compare viable GStreamer, Picamera2, and/or `rpicam` integration paths and record the selected camera owner. Selected one in-process GStreamer graph owned by `dashcamd`; evidence: `docs/architecture.md`.
  - [x] **PI:** Compare standard MP4, fragmented MP4, and robust muxing/finalization behavior. Evidence: `docs/test-reports/2026-07-24-pi-mux-recovery-validation.json`.
  - [x] **PI:** Verify USB audio stable identity, formats/rates, disconnect behavior, and USB power/topology. Identity, native formats, direct-root topology, PCM/AAC capture, bounded absent-device failure, subsequent video-only capture, and reconnect identity/AAC capture passed. Evidence: `docs/test-reports/2026-07-24-pi-gps-audio.md`.
  - [x] **PI:** Resolve `/dev/serial0`; validate fixed-clock mini UART or deliberate PL011 remapping and document Bluetooth consequences. Selected `/dev/ttyAMA0` PL011 with Bluetooth disabled; evidence: `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Measure baseline filesystem throughput, CPU, memory, temperature, throttling, and undervoltage. The non-destructive baseline is recorded; exFAT write/endurance measurements remain later storage gates.
  - [x] **PI:** Probe overlay-capable formats and preview stream/encoder candidates without starting a second camera owner. Evidence: NV12 overlay and same-`libcamerasrc` dual-stream measurements in `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] **PI:** Record architecture decisions, rejected alternatives, raw measurements, and remaining risks. Evidence: `docs/architecture.md` and `docs/test-reports/2026-07-24-milestone4-progress.md`.
  - [x] Exit gate: the exact production media path, OS architecture, UART configuration, GPS baud/protocol/UTC, USB-audio path and capability fault behavior, candidate muxer, and reference supply capacity are evidence-backed.

## Milestone 5 — Recording-volume provisioning and mount safety

- [ ] **Milestone 5 complete**
  - [x] **DESTRUCTIVE AUTHORIZATION:** The owner authorized complete erase, reflash, repartition, and format only for the 31,457,280,000-byte card with CID `fe34325344000000200000031a0192d1`; general-release destructive authorization remains unresolved. Evidence: `docs/test-reports/2026-07-24-milestone5-progress.md`.
  - [x] **HISTORICAL/RETIRED:** The v1-v4 initramfs-based image experiments, their local evidence, and v2/v3 forensic captures are retained as historical evidence only. They are not Bootstrap v1 validation and must not be flashed again.
  - [x] **HISTORICAL/RETIRED:** V4 was flashed and powered, then observed for more than ten minutes with no expected MAC, hostname, IP, or SSH availability. No SSH session or laptop-initiated Pi/storage mutation occurred during that observation. Its post-boot card state was not captured, so no partition result or exact failure cause is claimed. The architecture is retired and its raw artifacts were deleted by the owner.
  - [ ] **LOCAL:** Write the Bootstrap v1 image-build contract: Raspberry Pi OS Lite 32-bit base, preinstalled application/environment/dependencies/payload, temporary raw build image, retained `.img.xz`, and checked Imager manifest/hashes.
  - [x] **LOCAL:** Implement and test cmdline customization that removes exactly one standalone stock `resize` token, adds `dashcam.bootstrap=v1`, preserves Imager first-run tokens, and rejects ambiguous/missing token layouts. Evidence: `docs/test-reports/2026-07-25-bootstrap-v1-local.md`.
  - [x] **LOCAL:** Implement/test normal-post-root service ordering: preserve Imager `cloudinit-rpi`, order after `cloud-final.service`, require terminal successful cloud-init completion, retain defensive legacy-firstrun deferral, run storage independently of network, precede storage verification/dashcam writes, and become a no-op only after verified completion. Evidence: `docs/test-reports/2026-07-25-bootstrap-v1-local.md`.
  - [x] **LOCAL:** Implement/test Stage A planner and executor: derive mounted root/back disk, exact CID/layout gate, ext4+FAT MBR backup/hash, durable intent, one `sfdisk --no-reread` target write, raw MBR readback/commit, sync, and exactly one controlled reboot. Model/fake-runtime evidence: `docs/test-reports/2026-07-25-bootstrap-v1-local.md`; exact-card execution remains below.
  - [x] **LOCAL:** Implement/test Stage B: require a different boot ID and exact revalidation; run online `resize2fs` only and re-read its exact size; require newly-created p3 provenance, no known signatures, and the image-authored all-zero 4 MiB prefix before format intent; format once as exFAT `DASHCAM`; persist UUID mount/sentinel/directories/fstab/environment; completion marker last. Model/fake-runtime evidence: `docs/test-reports/2026-07-25-bootstrap-v1-local.md`; exact-card execution remains below.
  - [x] **LOCAL:** Add fault-injection/reconciliation tests for every Stage A/B boundary and power cut. Foreign, torn, conflicting, or destructive-refusal states must latch without auto-restore, auto-format, or destructive retry. Evidence: `tests/unit/test_bootstrap_storage_fault_matrix_v1.py` and `docs/test-reports/2026-07-25-bootstrap-v1-local.md`.
  - [x] **LOCAL:** Implement/test every-boot NetworkManager policy: bounded 60-second home-Wi-Fi association/local-route attempt, stable `Dashcam-<shortid>` AP fallback at `192.168.50.1/24` with unique WPA secret, and explicit retry/no oscillation. Network/AP failure must not block storage fault reporting or recording once storage is valid. Evidence: `docs/test-reports/2026-07-25-bootstrap-v1-local.md`.
  - [ ] **LOCAL:** Resolve and pin an executable Linux builder, qemu/binfmt/chroot environment, package-repository contract, and tool identities; replace every fail-closed placeholder in `deploy/bootstrap/image/build-requirements.json`.
  - [ ] **LOCAL:** Build the application wheel and complete dependency wheelhouse from one clean committed source identity; bind their complete hashes and installed records to `uv.lock`.
  - [ ] **LOCAL:** Complete deep independent readback for the exact app/payload/unit inventories, final package versions, cloud-init identities, and builder/verifier provenance.
  - [ ] **LOCAL:** Build the Bootstrap v1 `.img.xz`, independently re-read its FAT/ext4 contents, verify its manifest hashes and payload, and save the local evidence. No custom initramfs may be included.
  - [ ] **PI/READ-ONLY:** With the Pi powered down and the card inserted in the laptop, capture only the current card identity/layout as needed for the Bootstrap v1 flashing plan. Do not mutate the inserted card.
  - [ ] **PI/DESTRUCTIVE:** Re-resolve the exact authorized card offline immediately before flash; verify all available identity evidence; flash the verified Bootstrap v1 artifact through its manifest; then insert it into the reference Pi.
  - [ ] **PI/DESTRUCTIVE:** On the exact authorized 32 GB card, verify Imager first-run deferral if applicable, Stage A's one committed table and controlled reboot, then Stage B's online root growth and one-time exFAT formatting. Preserve durable evidence after each stage.
  - [ ] **PI:** Verify rootfs retains at least 2 GiB free after the full installation; adjust the target size if required.
  - [ ] **PI:** Verify partition-table backup, alignment, UUID capture, sentinel creation, completion-marker-last semantics, and idempotent later boots.
  - [ ] **PI:** Mount `/srv/dashcam` by UUID with validated exFAT options and service-account access.
  - [ ] **PI:** Prove the recorder preflight rejects an unmounted directory, wrong filesystem, wrong label/UUID/sentinel, read-only volume, and rootfs fallback while NetworkManager, SSH, and AP fallback remain usable.
  - [ ] **PI:** Test bounded `fsck.exfat` behavior for clean, dirty/repairable, and failed/unrepairable volumes; prove no auto-format.
  - [ ] **PI:** Measure sustained and burst write/finalization performance with reserve space enforced.
  - [ ] **PI/DESTRUCTIVE:** Repeat provisioning and acceptance on a nominal 64 GB card and each later explicitly authorized capacity class.
  - [ ] Exit gate: Bootstrap v1 provisioning is safe/idempotent on supported cards, recording cannot target the underlying rootfs directory, and the retained `.img.xz`/manifest process is reproducible.

## Milestone 6 — Reliable video-only recorder

- [ ] **Milestone 6 complete**
  - [x] **LOCAL:** Implement the recorder daemon skeleton, configuration load, state reporting, cancellation, and `systemd` notification. Evidence: `docs/test-reports/2026-07-24-pre-pi-implementation.md`.
  - [ ] **PI:** Implement the selected single-owner continuous camera and hardware H.264 pipeline.
  - [ ] **PI:** Implement bounded queues and non-blocking segment finalization.
  - [ ] **PI:** Rotate near 60 seconds on closed-GOP IDR boundaries without restarting the camera/encoder.
  - [ ] **PI:** Write to `pending`, flush/finalize, collision-check, and rename to `clips`.
  - [ ] **PI:** Persist clip lifecycle and reconcile the ext4 index with exFAT after clean/unclean restarts.
  - [ ] **PI:** Expose effective settings, frames, drops, bitrate, clip timing, restart count, and storage preflight state.
  - [ ] **PI:** Add bounded camera/encoder recovery with backoff and accurate `FAULTED`/`DEGRADED` reasons.
  - [ ] **PI:** Validate 10 consecutive independently playable clips, IDR starts, duration, and normalized boundary continuity.
  - [ ] **PI:** Pass a two-hour video-only endurance test with stable memory and no sustained throttling.
  - [ ] Exit gate: reliable 1080p30 hardware-encoded video-only segmentation passes Phase 1 evidence requirements.

## Milestone 7 — USB audio and A/V synchronization

- [ ] **Milestone 7 complete**
  - [x] **LOCAL:** Implement stable ALSA identity matching and prevent volatile-index substitution. Evidence: `docs/test-reports/2026-07-24-pre-pi-implementation.md`.
  - [ ] **PI:** Add mono 48 kHz AAC-LC at 128 kbit/s to the selected muxing pipeline.
  - [ ] **PI:** Timestamp audio from the pipeline clock and add required resampling/drift correction.
  - [ ] **PI:** Record video-only when the configured microphone is absent.
  - [ ] **PI:** Survive microphone unplug without camera restart or recording loss.
  - [ ] **PI:** Restore audio at the earliest supported safe boundary and report per-clip availability accurately.
  - [ ] **PI:** Test repeated disconnect/reconnect and reject a different device with a matching card index.
  - [ ] **PI:** Verify per-clip A/V skew below 100 ms with no systematic growth in an endurance run.
  - [ ] Exit gate: audio is synchronized when available and every declared audio fault leaves video recording operational.

## Milestone 8 — GPS, time, metadata, and filename reconciliation

- [ ] **Milestone 8 complete**
  - [ ] **PI:** Integrate the UART reader with bounded buffers, reconnect/backoff, and explicit GPS states.
  - [ ] **PI:** Validate supported NMEA talkers/sentences and checksum/error counters with the actual receiver.
  - [ ] **PI:** Start recording with no GPS and preserve boot ID, sequence, and monotonic clip/sample times.
  - [ ] **PI:** Accept trusted GPS UTC anchors using sentence-specific validity, date plausibility, continuity, source, and uncertainty.
  - [ ] **PI:** Configure exactly one system-clock owner and prove a clock step does not alter media PTS/DTS.
  - [ ] **LOCAL/PI:** Generate one bounded, versioned, atomic JSON sidecar per finalized clip.
  - [ ] **LOCAL/PI:** Keep UTC/local fields nullable while unsynced, then reconcile sidecars and filenames idempotently after a trusted anchor.
  - [ ] **LOCAL/PI:** Guarantee filename collision refusal and stable UUID API identity across reconciliation.
  - [ ] **PI:** Mark stale/lost GPS without repeating coordinates or speed as current.
  - [ ] **PI:** Test no-GPS boot, late lock, loss, reconnect, malformed input, implausible date, anchor conflict, UTC midnight, and DST transitions.
  - [ ] Exit gate: metadata remains internally consistent through unsynced startup and GPS/time faults without disturbing media capture.

## Milestone 9 — Burned-in overlay

- [ ] **Milestone 9 complete**
  - [x] **LOCAL:** Define overlay field formatting, numeric UTC offset, units, stale/invalid states, and layout bounds. Evidence: `docs/test-reports/2026-07-24-pre-pi-implementation.md`.
  - [ ] **PI:** Implement the measured fixed-region/pre-rendered overlay path before the recording encoder.
  - [ ] **PI:** Show `TIME UNSYNCED`, `GPS LOST`, and hidden/marked stale navigation values correctly.
  - [ ] **PI:** Prove the overlay and sidecar use the same telemetry snapshot/time model.
  - [ ] **PI:** Measure no-overlay versus overlay CPU, memory, temperature, encoded frame rate, and drop counts.
  - [ ] **PI:** Run clip-boundary, GPS-loss/recovery, and changing-text stress tests.
  - [ ] **PI:** If the overlay misses the 1080p30 gate, stop and document evidence/options; do not silently lower the product profile.
  - [ ] Exit gate: the burned-in overlay sustains the default recording profile with no statistically meaningful recording regression.

## Milestone 10 — Retention, protection, and crash reconciliation

- [ ] **Milestone 10 complete**
  - [x] **LOCAL:** Implement the durable clip catalog/migrations on ext4 and startup reconciliation against exFAT. Evidence: `docs/test-reports/2026-07-24-pre-pi-implementation.md`.
  - [ ] **LOCAL/PI:** Implement exact low/high/emergency free-space threshold behavior.
  - [ ] **LOCAL/PI:** Implement oldest-first retention using durable `DELETING` intent and idempotent two-file cleanup.
  - [ ] **LOCAL/PI:** Implement bounded download leases and expiry after crashed/abandoned clients.
  - [ ] **LOCAL/PI:** Implement event protection for previous 2, current, and next 1 clips with catalog/retention coordination.
  - [ ] **LOCAL/PI:** Implement protect/unprotect moves as recoverable two-file operations.
  - [ ] **PI:** Verify unknown/Windows-created files are ignored and preserved.
  - [ ] **PI:** Inject interruption after every finalize/protect/unprotect/delete step and verify deterministic reconciliation.
  - [ ] **PI:** Fill a small test filesystem, cycle retention repeatedly, and prove active/finalizing/protected/leased clips are never selected.
  - [ ] **PI:** Fill the volume with protected clips and verify explicit critical behavior before evidence destruction.
  - [ ] **PI:** Prove retention/reconciliation do not create recording gaps or unbounded startup delay.
  - [ ] Exit gate: ring retention is race-safe and crash-recoverable within the documented exFAT limitations.

## Milestone 11 — Access point, web UI, preview, and controlled shutdown

- [ ] **Milestone 11 complete**
  - [ ] **PI:** Configure the NetworkManager AP, DHCP, unique WPA passphrase, fixed direct IP, and optional mDNS.
  - [ ] **PI:** Prove AP/DHCP/mDNS failure cannot stop or restart recording.
  - [ ] **LOCAL/PI:** Implement the unprivileged web service and versioned API over the restricted recorder socket.
  - [ ] **LOCAL/PI:** Add authenticated sessions, CSRF protection, input allow-lists, secret redaction, and rate/size limits.
  - [ ] **LOCAL/PI:** Implement status, settings, clip list/detail, protected filters, event, restart, and structured errors.
  - [ ] **LOCAL/PI:** Implement controlled downloads through recorder-approved clip IDs and bounded leases; reject traversal and stale IDs.
  - [ ] **PI:** Benchmark preview candidates on the reference phone and select one from measured results.
  - [ ] **PI:** Implement on-demand preview with bounded/leaky queues, one client by default, and no second camera owner.
  - [ ] **PI:** Meet median preview latency below 500 ms and 95th percentile below 1 second without recording regression.
  - [ ] **LOCAL/PI:** Implement the confirmed/re-authenticated prepare-removal flow through a narrow privileged helper.
  - [ ] **PI:** Verify the flow rejects new operations, finalizes with a timeout, flushes, stops writers, unmounts, reports shutdown-in-progress, and powers down.
  - [ ] **PI:** Document and validate the physical ACT-LED/power-controller cue for safe card removal.
  - [ ] **PI:** Restart/crash the web service and connect slow/repeated clients without camera or recorder restart.
  - [ ] Exit gate: the complete phone workflow is secure, bounded, and operationally independent from recording.

## Milestone 12 — Release image, interoperability, and hardening

- [ ] **Milestone 12 complete**
  - [ ] **PI:** Produce the repeatable release image/customization process with exact versions and checksums.
  - [ ] **PI:** Verify fresh-install, upgrade, migration, uninstall/recovery, and second-boot idempotency procedures.
  - [ ] **WIN:** After controlled shutdown, validate `DASHCAM` visibility, MP4 seek/playback, JSON readability, and pair copying on Windows 10.
  - [ ] **WIN:** Repeat the interoperability validation on Windows 11 and representative multi-partition card readers.
  - [ ] **WIN/PI:** Reinsert a Windows-used card and prove Windows metadata is preserved/ignored and recording resumes without formatting.
  - [ ] **PI:** Run the full 12-hour endurance test with GPS, microphone, AP, periodic preview, and at least two retention cycles.
  - [ ] **PI/DESTRUCTIVE:** Run the declared randomized abrupt-power matrix on named reference card models and preserve every result/log.
  - [ ] **PI:** Run camera, encoder, storage, UART, thermal, undervoltage, AP, web, and microphone fault injection.
  - [ ] **PI:** Verify bounded recovery time, durable catalog usability, no rootfs fallback, and no automatic exFAT formatting.
  - [ ] **PI:** Record CPU, RSS/memory, swap, temperature, throttling, undervoltage, drops, bitrate, A/V skew, preview latency, and storage throughput.
  - [ ] **LOCAL:** Complete installation, configuration, hardware, architecture, Windows, troubleshooting, backup, and recovery documentation.
  - [ ] **LOCAL:** Map every requirement and acceptance test in `Pizero_dashcam_PROJECT.md` to a passing evidence artifact or documented accepted deviation.
  - [ ] **LOCAL:** Run final formatting, lint, type, unit, integration, security, packaging, and documentation checks.
  - [ ] Exit gate: every definition-of-done item has evidence, all deviations are explicitly accepted, and the release artifact is reproducible.
