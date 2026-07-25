# Milestone 3 local deployment and diagnostics report

## Scope and authorization

- Date: 2026-07-24 local / 2026-07-23 UTC
- Scope: local Phase 0A diagnostics, offline validators, dry-run provisioning,
  and deployment/test-procedure drafts
- Hardware/device access: none
- SSH, Pi commands, real `ffprobe`/`ffmpeg`, block-device discovery, partition
  mutation, formatting, mounting, power interruption, and Windows validation:
  not performed

## Delivered tools and contracts

- A fixed-allowlist, shell-free, read-only capability probe with bounded
  commands/files/output/deadlines, secret redaction, raw observations, explicit
  unknown/error states, and capability-report-v1 output.
- Offline media validation for codecs, independent decoder evidence, first
  keyframe/IDR, duration, bitrate, A/V skew, and monotonic boundary continuity.
- Bounded endurance collection/analysis for RSS, available memory, swap, CPU,
  temperature, throttling, undervoltage, drops, bitrate, and restarts.
- A declarative candidate layout, pure captured-observation verifier, and
  deterministic dry-run provisioning plan with identity/refusal gates.
  Execution is disabled even with exact confirmation.
- Draft systemd, mount, and NetworkManager contracts with timeouts, watchdogs,
  bounded restart, least privilege, unique-secret placeholders, and explicit
  target-validation gates.
- Installation/upgrade/rollback/recovery/log, controlled-removal, abrupt-power,
  retention, and Windows 10/11 procedure drafts.

## Integrated review corrections

- The media probe requests payload for only the first selected video packet;
  it cannot accidentally retain a full-clip hex dump.
- Media command timeout/output CLI values have hard maxima.
- Capability raw observations retain up to 16 KiB while remaining schema-bounded.
- Reconnect, secret-redaction, malformed-input, and runtime type bounds were
  strengthened.
- Provisioning refuses mounted source images, marks every mutation accurately,
  uses valid JSON markers, and makes root partition GUID/PARTUUID preservation
  an unresolved execution prerequisite.
- GPT is explicitly a local fixture/candidate choice; Phase 0B must match the
  exact selected image rather than converting it by assumption.
- The recorder service intentionally starts after a failed soft preflight only
  to publish `STORAGE_FAULT`; its application gate must not open the camera or
  media until a fresh verified mount check succeeds.
- The controlled-removal contract denies the web process direct root/systemd/
  mount privilege and orders recorder finalization before the fixed root helper.

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
uv run python scripts/verify_layout.py --observation tests/fixtures/provisioning/source-ready.json
uv run python scripts/plan_provision.py --observation tests/fixtures/provisioning/source-ready.json --expected-identity tests/fixtures/provisioning/source-identity.json
uv run python scripts/monitor_endurance.py --input tests/fixtures/media/endurance_pass.json --output <new-temporary-report>
```

Results:

- Lock, formatting, linting, and strict typing passed.
- 261 tests passed on each supported Python interpreter.
- Source distribution and wheel built successfully.
- Fixture layout verification accepted only the declared source fixture.
- The provisioning CLI emitted a dry-run plan; every command remained inert
  JSON and no executor exists or was called.
- The endurance fixture CLI emitted a passing bounded report to a new temporary
  path, which was removed after inspection.
- Independent deployment review findings were resolved or explicitly deferred
  to target validation with refusal language and regression tests.

## Limitations

This report proves local parsing, planning, safety gates, and fixture decisions
only. It does not select the production partition-table type, media pipeline,
encoder, muxer, OS architecture, device nodes, UART mapping, AP capabilities, or
service sandbox exceptions. It does not establish Pi performance, media
decodability, exFAT recovery, power-loss safety, provisioning correctness, or
Windows interoperability.

