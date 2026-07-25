# Development and test workflow

## Current authorization boundary

Local Phase 0A is complete. The owner authorized Milestone 4 SSH and capability
work on the newly flashed Pi on 2026-07-24. Read-only probing and
evidence-required boot configuration/reboots are allowed; destructive storage,
partitioning, formatting, deployment, and later acceptance work remain gated.
The media path, UART/GPS, USB audio, reference supply capacity, and remaining
power-loss risk are selected from saved Pi evidence; Milestone 4 is complete.
The owner authorized complete erase, reflash, repartition, and format of the
exact 31,457,280,000-byte card with CID
`fe34325344000000200000031a0192d1` on 2026-07-24. Its enabled regular-file
image is independently verified. Physical work still requires shutting down
the Pi, moving the card offline, and re-resolving the target before writing.
Other cards remain unauthorized. Local fixtures and regular-file builds are
not hardware acceptance evidence.

## Local workflow

1. Read `AGENTS.md`, the relevant product-specification sections, and the active
   milestone in `plan.md`.
2. Preserve unrelated work (`git status`) and make the smallest change that
   advances the active milestone.
3. Add or update local tests with the change, including bounded failure behavior.
4. Run the relevant checks. Do not mark a plan item complete until validation and
   any required evidence exist.
5. Record target measurements only after the Pi gate opens; add them to a report
   based on [test-report-template.md](test-report-template.md).

Python 3.11 through 3.13 is supported; `.python-version` selects Python 3.12 for
local development. Create the locked development environment and run the
CI-equivalent quality suite with:

```text
uv sync --frozen --group dev
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --junitxml=artifacts/pytest.xml
```

`uv.lock` is committed and must remain synchronized with `pyproject.toml`.
Hardware and Windows tests use explicit pytest markers and remain blocked by their
respective authorization/environment gates.

## Test categories

| Category | Typical scope | Required environment | Evidence it can provide |
| --- | --- | --- | --- |
| Local unit | Config, state transitions, filename/path validation, NMEA parsing, timezone conversion, retention | Development machine | Pure logic and bounded-input behavior only |
| Local integration | File intents/reconciliation, interfaces with fakes, schema compatibility | Development machine | Cross-module logic only |
| Static quality | Formatting, linting, type checks, package import | Development machine / CI | Source quality and packaging only |
| Pi capability | Camera modes, codecs/caps, audio, UART, OS, muxer, storage throughput | Exact Pi Zero 2 W + approved Raspberry Pi OS image | Target capability report |
| Pi functional/performance | 1080p30 recording, clips, audio sync, GPS, overlay, preview, endurance, faults | Exact Pi/image and connected hardware | Hardware and performance acceptance evidence |
| Pi destructive | First-boot provisioning, partition layout, exFAT fault/recovery and power loss | Expendable Pi media only, explicit approval | Storage safety/recovery evidence |
| Windows interoperability | Read/copy/open completed MP4+JSON from controlled-shutdown media | Validated Windows 10/11 host + tested card | Windows acceptance evidence |

Hardware, performance, power-loss, provisioning, and Windows tests cannot be
passed or implied by local runs.

## Required testing properties

- Keep queues, retries, recovery scans, leases, logs, and shutdown work bounded.
- Exercise invalid input, missing optional GPS/audio, failed storage verification,
  collision handling, and interrupted logical-pair operations.
- Never exercise a destructive path without an exact identified expendable target,
  dry-run/refusal checks, and the applicable authorization.
- Save commands, versions, fixtures, measurements, failures, and deviations in a
  test report. Do not replace evidence with a claim of success.
