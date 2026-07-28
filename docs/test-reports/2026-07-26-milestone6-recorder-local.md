# Milestone 6 local recorder implementation

Date: 2026-07-26

## Result

The first production-shaped video-only recorder slice is implemented and
locally validated. This is hardware-independent logic evidence, not acceptance
of the exact Pi pipeline, service activation, clip continuity, recovery, or
endurance gates.

No SSH session, Pi package installation, service activation, or storage
mutation was performed for this slice.

## Implemented scope

- A lazily loaded PyGObject/GStreamer adapter builds one continuous graph:
  `libcamerasrc` -> 1920x1080 NV12 at 30/1 -> `v4l2h264enc` -> explicit
  High/4.1 H.264 caps -> `h264parse config-interval=-1` -> bounded non-leaky
  recording queue -> asynchronous `splitmuxsink`/fragmented `mp4mux`.
- The encoder contract uses an 8 Mbit/s target, GOP 30, repeated headers, and
  the driver's default VBR mode. It never assigns the read-only encoder device
  property or the known-bad `video_bitrate_mode=1` control.
- Segment rotation targets 60 seconds without restarting the camera or encoder;
  `mp4mux` uses one-second fragments for bounded recovery exposure.
- The runtime requires matching live `READY` storage evidence before camera
  acquisition and binds output to `/srv/dashcam/pending`.
- Pending-name allocation is bounded, refuses unsafe directory contents and
  MP4/JSON collisions, and never silently overwrites the initial pair.
- Readiness is withheld until `splitmuxsink` reports the first validated
  provisional fragment open. Early backend failure and readiness timeout both
  roll back the session.
- Fragment-close events are validated and continuously drained into bounded
  runtime counters so an unconsumed event queue cannot stall recording.
- Camera ownership is process-local and exclusive. Startup, run, stop, bus
  failure, timeout, and cancellation paths serialize driver calls and retain a
  bounded route to the GStreamer `NULL` state.
- The production `python -m dashcam.daemon` entry point composes the storage
  gate and recorder runtime, installs cooperative signal handling, and preserves
  systemd readiness/watchdog semantics.
- The future SSH-development bundle includes the GStreamer Python override
  package and validates a fractional GStreamer caps value during installation.

## Validation

```text
uv run pytest -q
1153 passed, 10 skipped in 44.28s

uv run ruff check .
All checks passed!

uv run mypy --strict src
Success: no issues found in 66 source files
```

The ten skips are platform-specific POSIX shell, symlink, and directory
semantics that are unavailable on the Windows development host. The full suite
otherwise includes recorder graph, PyGObject adapter, runtime orchestration,
entry point, daemon, storage preflight, installer, and deployment artifact
coverage.

## Still open

- Run the composed graph on the exact Pi and prove the effective caps, dynamic
  hardware encoder identity, continuous camera ownership, and first segment.
- Implement durable provisional MP4+JSON finalization, rename into `clips`, and
  restart reconciliation.
- Add no-progress detection and bounded camera/encoder recovery.
- Enable and start the reviewed `dashcamd.service` only after the next
  hash-closed Pi deployment is reviewed and applied.
- Pass ten-clip continuity and two-hour endurance acceptance.
