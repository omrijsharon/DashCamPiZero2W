# Milestone 6 local durable finalization

Date: 2026-07-26

## Result

The recorder now has a locally validated, production-wired path from a
GStreamer `fragment-closed` event to a recoverable MP4+JSON clip pair. No Pi
deployment or hardware mutation was performed for this slice, so both
finalization-related Pi checklist items remain open.

## Implemented contract

1. Bind the finalizer filesystem to the device identity from the fresh storage
   preflight and refuse a replaced mount, symlinked managed directory, path
   escape, non-regular MP4, empty MP4, or bounded collision-scan overflow.
2. Flush the closed provisional MP4.
3. Generate a stable clip UUID and a finalized-unsynced filename that retains
   boot/sequence identity but removes the active `.partial` suffix.
4. Write the canonical sidecar through a no-replace temporary-file rename,
   flush it and `pending`, then read it back through the strict bounded parser.
5. Commit the `FINALIZING` catalog row and explicit pending-to-clips
   `FINALIZE` intent together in SQLite/WAL with `synchronous=FULL`.
6. Revalidate sidecar/intent identity and promote each member with atomic
   Linux `renameat2(RENAME_NOREPLACE)`, flushing affected directories.
7. Mark the intent complete, lifecycle `FINALIZED`, and pair reconciled only
   after both target members are observed.
8. On restart, replay only bounded, already-durable `FINALIZE` intents.
   Source/target conflicts, missing members, malformed sidecars, and
   case-insensitive collisions latch refusal without overwrite.
9. Supervise finalizer failure as a recorder failure and wait for every emitted
   closure, including the shutdown fragment, before cancelling the worker.

The previously retained MP4-only implementation diagnostics are not scanned
into fabricated sidecars or catalog rows.

## Timing and metadata

The first validated fragment-open event anchors GStreamer running time to the
host monotonic domain. Consecutive closure boundaries provide clip start/end
times. Bitrate is calculated from the verified file size and monotonic
duration. Frame/drop counters are currently recorded as unavailable with a
bounded warning and zero placeholders required by the current v1 schema; those
zeros are not accepted measurements, and the production metrics task remains
unchecked.

## Local validation

```text
uv run pytest -q
1175 passed, 10 skipped in 26.07s

uv run ruff check .
All checks passed!

uv run mypy --strict src
Success: no issues found in 67 source files
```

The ten skips are the existing Windows-host exclusions for POSIX shell,
symlink, directory-fsync, and Linux device semantics. Dedicated finalizer tests
cover commit failure, crashes before moves and after either member move,
completed replay, corrupt JSON, missing members, case collisions, ambiguity,
bounded scans, and preservation of MP4-only diagnostics.

## Still open

- Deploy the new release through a reviewed hash-closed Pi bundle.
- Prove ordinary promotion, target collision refusal, clean shutdown of the
  active fragment, and bounded interruption replay on the exact exFAT volume.
- Complete recorder metrics, ten-clip continuity, and endurance acceptance.
