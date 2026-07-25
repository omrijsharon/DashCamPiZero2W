# Troubleshooting and safe recovery

## Validation status

The fault model and read-only evidence commands are drafted locally. Service
behavior and recovery remain unvalidated until their implementation and Pi
milestones.

## Preserve evidence first

Do not format, repartition, delete unknown files, disable storage verification,
open the camera from a second process, or write into the root filesystem. Capture
the bounded evidence in [`operations.md`](operations.md): service state, mount
identity, free space, versions, and operation intents.

## Fault priorities

- `STORAGE_FAULT`, missing distinct mount, wrong filesystem/identity/sentinel,
  read-only storage, or protected-only exhaustion blocks recording. It never
  triggers auto-format or rootfs fallback.
- Camera/encoder faults use bounded recovery and must not claim `RECORDING` until
  durable writes resume.
- GPS, microphone, AP, web, and preview faults remain visible but cannot
  backpressure or terminate recording.
- Dirty exFAT repair uses only the later validated bounded `fsck.exfat` policy.
  Failed repair preserves the volume and requests operator action.

## Evidence-led decisions

- If `/srv/dashcam` is a plain rootfs directory, leave it untouched and keep the
  recorder stopped.
- If a pair is half-moved/deleted, preserve its durable intent and run only
  idempotent reconciliation; never make the orphan a retention candidate.
- If GPS time conflicts, retain monotonic timestamps and rejection diagnostics.
  Do not force the system clock.
- If the AP fails, use authorized local/SSH diagnostics when available; do not
  restart recording merely to recover networking.
- If an upgrade fails, apply the rollback compatibility gate. Never restore an
  incompatible catalog blindly.
