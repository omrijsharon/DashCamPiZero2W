# Local validation tools

These tools are safe harnesses for later Pi validation. Their local fixture tests prove
parsing, bounds, and decision logic only. They do not prove any Raspberry Pi, camera,
encoder, microphone, storage, thermal, or power capability.

## Media validation

`scripts/validate_media.py` accepts one or more explicit media paths and writes one
versioned JSON report:

```powershell
uv run python scripts/validate_media.py clip-0001.mp4 clip-0002.mp4 `
  --timeline capture-timeline.json `
  --output media-validation.json
```

For each file the tool runs fixed, shell-free `ffprobe` and `ffmpeg` argument
vectors. Each command is capped at 120 seconds and 8 MiB retained output; larger
CLI values are refused. A metadata probe requests only the required stream,
packet, frame, duration, and bitrate fields. A second probe requests payload for
only the first selected video packet, avoiding an accidental hex dump of the
entire clip. A separate `ffmpeg` run must successfully decode the selected video
and optional audio streams before the report can mark `decoder_run` as `pass`.
Probe success alone is never called decodability.

IDR evidence comes from the first video packet's H.264 NAL data. A keyframe flag
without NAL data yields `indeterminate`, not a guessed IDR pass.

Adjacent MP4 files commonly start their own timestamp domains near zero. Boundary
validation therefore requires capture intervals from the recorder's monotonic
timeline. It never compares raw timestamps from separate MP4 files. The optional
timeline manifest is:

```json
{
  "schema_version": 1,
  "clips": [
    {
      "path": "clip-0001.mp4",
      "start_monotonic_ns": 1000000000,
      "end_monotonic_ns": 61000000000
    },
    {
      "path": "clip-0002.mp4",
      "start_monotonic_ns": 61020000000,
      "end_monotonic_ns": 121020000000
    }
  ]
}
```

A missing timeline makes the corresponding boundary `indeterminate`. A positive
delta larger than one frame is a gap; a negative delta whose magnitude is larger
than one frame is an overlap. Report outcomes are `pass`, `fail`, `indeterminate`,
and, for checks that do not apply, `not_applicable`.

The command refuses to overwrite an existing output unless `--overwrite` is
specified. It exits 0 only for a fully passing report, 1 for a validation failure or
indeterminate result, and 2 for invocation/input errors.

## Endurance analysis

`scripts/monitor_endurance.py` analyzes an explicit bounded sample document:

```powershell
uv run python scripts/monitor_endurance.py `
  --input endurance-samples.json `
  --output endurance-report.json
```

The input is a versioned object containing at most 43,200 samples. Every sample has
the following nullable fields; unavailable platform evidence must be `null`, never a
fabricated healthy value:

```json
{
  "schema_version": 1,
  "samples": [
    {
      "monotonic_ns": 1000000000,
      "rss_bytes": 50000000,
      "memory_available_bytes": 300000000,
      "swap_used_bytes": 0,
      "cpu_percent": 48.0,
      "temperature_c": 58.0,
      "throttled": false,
      "undervoltage": false,
      "dropped_frames": 0,
      "bitrate_bps": 7900000,
      "restart_count": 0
    }
  ]
}
```

The library collector accepts an injected sample source, monotonic clock, and sleep
function. It retains exactly the configured bounded sample count. The report checks
RSS growth, minimum available memory, maximum swap/CPU/temperature, any reported
throttling or undervoltage, dropped-frame and restart counter increases, and average
bitrate. Missing evidence produces `indeterminate`; it never passes silently.

The analyzer only reads its explicit input and writes its explicit output. It does
not invoke hardware commands, restart services, alter device state, or create an
unbounded history. It refuses to overwrite an existing report unless `--overwrite`
is supplied.
