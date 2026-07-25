# DashCam Pi Zero 2 W

An autonomous dashcam project for Raspberry Pi Zero 2 W. The acceptance contract is
[Pizero_dashcam_PROJECT.md](Pizero_dashcam_PROJECT.md); implementation progress is
recorded in [plan.md](plan.md).

The intended product continuously records one-minute, independently playable MP4
clips at 1080p30 using target-verified hardware H.264, with optional USB audio,
UART GPS, telemetry, protected events, exFAT ring retention, and a local web UI.
Recording reliability takes priority over optional features.

## Status

Local Phase 0A is complete, and the Pi Phase 0B capability gate is in progress.
The IMX219, 32-bit OS, PL011 UART, GStreamer camera path, hardware H.264 encoder,
and fragmented MP4 candidate are now measured and selected on the reference Pi.
GPS, USB audio, recording-volume provisioning, deployment, and full acceptance
remain blocked or incomplete. The repository now includes the local
development foundation, hardware-independent domain contracts, read-only
diagnostic/provisioning tools, and locally tested recorder, GPS, catalog,
retention, overlay, audio-selection, control-socket, and secured web-service
components. It is not yet installable or a usable dashcam image.

The detailed target measurements and remaining blockers are in
[the Milestone 4 progress report](docs/test-reports/2026-07-24-milestone4-progress.md).

## Development workflow

Python 3.11 through 3.13 is supported; local development defaults to Python 3.12.
Install the locked development environment with:

```text
uv sync --frozen --group dev
```

Run the complete local quality suite before checking a plan task:

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --junitxml=artifacts/pytest.xml
```

Keep unit tests deterministic and place recorded fixtures behind hardware
interfaces. A local fake may demonstrate control logic; it is never evidence of
Pi functionality, performance, exFAT power-loss behavior, provisioning safety,
or Windows interoperability.

See [docs/development.md](docs/development.md) for the test matrix and expected
workflow, and [docs/test-report-template.md](docs/test-report-template.md) for
the evidence format.

## Architecture guardrails

- `dashcamd` is the sole camera owner; preview and web components must not open it.
- The camera/encoder must stay continuous across ordinary clip boundaries and
  segmentation must occur on closed-GOP IDR/keyframe boundaries.
- The production 1080p30 profile must use measured hardware H.264—never a silent
  software fallback or reduced profile.
- Recording may start without GPS or audio, but never without a verified writable
  exFAT `DASHCAM` mount at `/srv/dashcam`; root-filesystem fallback is forbidden.
- MP4 and JSON sidecars are a recoverable logical pair, with durable intent and
  idempotent recovery rather than an assumed atomic pair write.

The current architecture status and pending decisions are in
[docs/architecture.md](docs/architecture.md).

## Documentation

- [Architecture status](docs/architecture.md)
- [Development and test workflow](docs/development.md)
- [Hardware](docs/hardware.md) — placeholder; not validated
- [Installation](docs/installation.md) — placeholder; not validated
- [Configuration](docs/configuration.md)
- [Version 1 API contract](docs/api.md)
- [Capability probe](docs/capability-probe.md)
- [Validation tools](docs/validation-tools.md)
- [Provisioning safety](docs/provisioning.md)
- [Operations](docs/operations.md)
- [Test procedures](docs/test-procedures.md)
- [Prepare-removal design](docs/prepare-removal.md)
- [Versioned JSON Schemas](schemas/README.md)
- [Milestone 3 local evidence](docs/test-reports/2026-07-24-milestone3-local.md)
- [Pre-Pi implementation evidence](docs/test-reports/2026-07-24-pre-pi-implementation.md)
- [Milestone 4 Pi progress evidence](docs/test-reports/2026-07-24-milestone4-progress.md)
- [Troubleshooting](docs/troubleshooting.md) — placeholder; not validated
- [Test report template](docs/test-report-template.md)

## License

Copyright © 2026 Tami Pinhasi. All rights reserved. No open-source license has
been granted; see [LICENSE](LICENSE).
