# Milestone 2 local domain-model report

## Scope and authorization

- Date: 2026-07-24 local / 2026-07-23 UTC
- Scope: hardware-independent Phase 0A domain models and contracts
- Hardware access: none
- Raspberry Pi, SSH, provisioning, device I/O, destructive storage, media
  performance, and Windows card interoperability: not evaluated

## Delivered contracts

- Strict versioned TOML configuration, migration dispatch, secret separation,
  and failure-safe atomic updates.
- Independent recorder, storage, GPS, GPS-time, system-clock, audio, and
  device-operation states.
- UUID clip identity, lifecycle transitions, orthogonal protection, and bounded
  monotonic download leases.
- Portable provisional/final clip names with collision and traversal refusal.
- Checksum-verified, bounded RMC/ZDA/GGA parsing and recorded fixtures.
- Monotonic-to-UTC anchoring with plausibility, conflict, reacquisition, and
  uncertainty policies, plus IANA timezone conversion.
- Versioned, bounded clip-sidecar model and Draft 2020-12 JSON Schema.
- Pure retention thresholds/selection and versioned idempotent pair-operation
  reconciliation plans.
- Version 1 public API route/security/error contracts.
- Generated property tests for names, paths, configuration bounds, lifecycle
  transitions, and arbitrary NMEA byte input.

## Environment

- Host: Microsoft Windows 11 Pro 10.0.26200, AMD64
- Compatibility interpreters: CPython 3.11, 3.12.6, and 3.13
- uv: 0.11.31
- Ruff: 0.16.0
- mypy: 1.20.2
- pytest: 9.1.1
- Hypothesis: 6.161.1

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

- Lockfile synchronized.
- Formatting, linting, and strict typing passed.
- 172 tests passed on each of Python 3.11, 3.12, and 3.13.
- The sidecar schema passed Draft 2020-12 meta-schema validation, and both
  anchored and unsynchronized canonical documents validated against it.
- Source distribution and wheel built successfully.
- Source/test audit found no serial, camera, GStreamer, SSH, or subprocess
  device access. `/dev/serial0` appears only as validated configuration data.

## Limitations

This report proves only local deterministic logic. Fixtures are not evidence of
the GPS receiver, UART mapping, camera pipeline, encoder, audio device, exFAT
durability, AP behavior, preview latency, Pi resource usage, or Windows media
compatibility. Those remain behind the explicit Pi-access and hardware gates.

