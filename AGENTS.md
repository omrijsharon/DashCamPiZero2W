# AGENTS.md

## Current gate

- The owner authorized local implementation of **DashCam Bootstrap v1** on
  2026-07-25. It replaces the retired v1-v4 initramfs image mechanism.
- Bootstrap v1 is a compressed custom Raspberry Pi OS Lite 32-bit image with
  the app, environment, dependencies, and first-boot payload preinstalled.
  Keep only the `.img.xz`, checked Imager manifest, and hashes as release
  artifacts; a raw image is a temporary build product.
- Bootstrap v1 uses ordinary post-root systemd Stage A/Stage B services. It
  must remove exactly one standalone stock `resize` cmdline token, add
  `dashcam.bootstrap=v1`, and preserve every unrelated token. The pinned
  2026-06-18 Trixie image uses Imager `cloudinit-rpi`: order storage/network
  policy after `cloud-final.service`, and require terminal successful
  cloud-init completion before storage mutation. Keep legacy
  `systemd.run`/`firstrun` detection as a defensive check. Readback must prove
  cloud-init has no `growpart`/`resizefs`; Stage A is the only runtime
  partition grower. No custom initramfs, kernel table reread, or `partprobe` is
  permitted.
- Verify both official source hashes before customization. Offline-grow the
  build-time p2/ext4 source to exactly 4 GiB for the preinstalled payload, while
  retaining the 6 GiB Stage A target. The image must author and read back an
  all-zero 4 MiB prefix at the future p3 start; Stage B also requires no known
  filesystem signatures before its one authorized format.
- Stage A is an exact-identity-gated, one-write `sfdisk --no-reread`
  transaction followed by raw-MBR readback, durable commit, sync, and exactly
  one controlled reboot. Stage B runs on a different boot ID, revalidates the
  target, grows mounted ext4 online with `resize2fs`, verifies blank p3/format
  intent, formats it once as exFAT `DASHCAM`, configures UUID mounting and the
  sentinel, and writes completion last. Foreign/torn/refused states latch:
  never auto-restore, auto-format, or destructively retry.
- Bootstrap services must run before storage verification/dashcam writes but
  independently of networking. Failure must leave NetworkManager, SSH, and AP
  fallback available; `dashcamd` reports `STORAGE_FAULT` and must not write
  until the verified exFAT mount is present.
- Every boot tries configured home Wi-Fi for at most 60 seconds. Association
  plus a local route is success; internet is not required. On failure, use a
  stable NetworkManager AP at `192.168.50.1/24`, SSID
  `Dashcam-<short-device-id>`, and a unique WPA secret until reboot or explicit
  retry. Never oscillate between client and AP modes.
- **No new card flash is authorized yet.** The Pi is powered down and the SD
  card is in the laptop; keep it read-only. Local implementation and local
  tests must be reviewed before requesting an identity-checked Bootstrap v1
  flash.
- The exact 31,457,280,000-byte card with CID
  `fe34325344000000200000031a0192d1` remains authorized for a future
  destructive Pi trial only. General-release destructive authorization policy
  remains unresolved and must be explicit.
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

- Begin with authorized local work; do not flash or mutate the inserted card
  until the current no-flash gate changes explicitly.
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
