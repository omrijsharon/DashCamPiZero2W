# Pre-Pi hardware-independent implementation report

## Scope and authorization

- Date: 2026-07-24 local / 2026-07-23 UTC
- Scope: hardware-independent recorder and control-plane implementation using
  injected target interfaces and local fakes
- Hardware/device access: none
- SSH, Pi commands, camera/media-device access, UART/ALSA enumeration,
  block-device access, mounting, provisioning execution, power interruption,
  phone testing, and Windows validation: not performed

## Delivered local implementation

- Recorder lifecycle, configuration/state reporting, cancellation,
  readiness/watchdog notification, bounded supervision, a single-owner media
  runtime contract, closed-GOP segment decisions, and isolated optional branches.
- Strict recording-volume preflight using injected mount, space, sentinel, and
  write-probe facts; every failure is fail-closed and rootfs fallback is rejected.
- Bounded GPS transport supervision and NMEA ingestion, stable ALSA USB identity
  selection, coherent overlay formatting, and constant-memory health metrics.
- SQLite/WAL catalog migrations, bounded clip queries, durable filesystem
  intents, event protection windows, leases, retention eligibility, and bounded
  startup reconciliation against an injected exFAT namespace.
- Strict versioned sidecar parsing and idempotent post-anchor metadata/filename
  reconciliation with stable UUIDs and collision refusal.
- Closed, bounded recorder socket protocol and dispatcher with UUID-only clip
  operations, approved-path download leases, secret-safe snapshots, and explicit
  administrative operation states.
- Unprivileged web policy and WSGI adapters with scrypt password verification,
  bounded sessions, CSRF, recent reauthentication, independent login throttling,
  strict endpoint input, secret redaction, and traversal/symlink-resistant
  download streaming.

## Integration corrections

- Segment rotation now faults on a late IDR or overlong final artifact instead
  of silently accepting a clip outside the declared duration contract.
- Optional pipeline branch shutdown and GPS reads have explicit time bounds;
  stale GPS navigation is cleared rather than repeated as current.
- Catalog scans and result sets are bounded, retention order is unique, and
  canonical sidecars are strictly parsed before recovery import.
- Protection revisions prevent an event that overlaps a pending intent from
  being undone by stale finalize/unprotect/name-reconciliation completion.
- Web login has a limiter independent of general request throttling. Download
  paths come only from recorder/catalog approval, stay inside managed clip
  directories, reject symlinks and non-regular files, and always release leases.
- Platform health parsing distinguishes unavailable from malformed facts and
  retains only a closed set of bounded counters and gauges.

## Validated commands and results

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
uv build
uv run --python 3.11 --isolated --frozen pytest -q
uv run --python 3.12 --isolated --frozen pytest -q
uv run --python 3.13 --isolated --frozen pytest -q
```

Results:

- Dependency lock, formatting, linting, and strict typing passed.
- 567 tests passed on each supported Python interpreter: 3.11.15, 3.12.6, and
  3.13.14.
- The source distribution and universal wheel built successfully.
- Focused control/web/catalog/storage/GPS integration validation passed before
  the full matrix.
- Five purely local plan tasks were checked. Mixed LOCAL/PI tasks remain open
  because local fakes do not satisfy their target evidence requirements.

## Limitations and next gate

This is not a usable dashcam or installable Pi image. It does not select or prove
the camera backend, hardware encoder, muxer, actual segment finalization,
UART/ALSA device adapters, overlay renderer, preview transport, HTTP server
deployment, socket ownership, privileged removal helper, exFAT behavior, or
performance on a Pi Zero 2 W.

The next step is owner-controlled OS flashing followed by explicit SSH
authorization. Milestone 4 must measure the exact image and hardware before
target-dependent adapters or acceptance claims are implemented.
