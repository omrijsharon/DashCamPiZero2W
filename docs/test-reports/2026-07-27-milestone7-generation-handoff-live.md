# Milestone 7 immutable-generation handoff — exact-Pi capability

Date: 2026-07-27  
Reference boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Installed release: `0.1.0.dev0-011a148e085da278`

## Scope and result

The hash-closed isolated capability harness passed twice without changing its
bundle between runs. It used one continuous IMX219 camera and one continuous
Raspberry Pi hardware-H.264 encoder while routing their encoded output through:

1. an immutable A/V recording generation;
2. an immutable video-only recording generation;
3. a new immutable A/V recording generation.

Each generation owned a complete fixed `splitmuxsink` pad topology before it
received data. No live splitmux generation gained, lost, or replaced a request
pad. Retired request pads and bins were released only after the whole parent
pipeline reached `NULL`.

This is programmatic routing evidence. It does not yet prove physical USB
unplug/reconnect, repeated unbounded generation creation/reclamation, or
production integration. `safe_to_integrate_production` therefore remains
deliberately `false` in both result documents.

## Closed bundle

- Directory:
  `deploy/ssh-dev-validation/milestone7-generation-handoff/`
- `run.py` SHA-256:
  `e25fcc67f723a407520f402db11b215d0e60599396918c19f45c09da7aec4db2`
- `SHA256SUMS` SHA-256:
  `ba780c442491ee0f278daaadd2df11d48c3f5ac7adce802d3634a536e07c1013`
- Exact installed interpreter:
  `/opt/dashcam/releases/0.1.0.dev0-011a148e085da278/venv/bin/python`

## Accepted repetitions

| Evidence | Media shape | Restored A/V edge skew | Steady spread | Total video block |
|---|---:|---:|---:|---:|
| `20260727j` | 3 A/V + 3 video-only + 4 A/V | 98.334, 14.709, 6.292, 9.000 ms | 8.417 ms | 171.412, 177.462 ms |
| `20260727k` | 3 A/V + 3 video-only + 4 A/V | 76.959, 6.958, 6.666, 8.958 ms | 2.292 ms | 169.714, 168.382 ms |

Both repetitions additionally proved:

- ten exact MP4s with the declared generation stream sets;
- a real H.264 IDR NAL in every first video packet;
- independent Raspberry Pi hardware-H.264 decode of every clip and AAC decode
  of every A/V clip;
- normalized video handoff gaps within one frame, with successor first buffers
  on IDRs;
- monotonic per-generation and parent counters with no large gaps;
- stable camera, encoder, parser, clock, and base-time identity;
- exact fragment-open/fragment-closed matching and bounded EOS/NULL cleanup;
- zero recorded GStreamer warnings/errors, service restarts, throttling, or
  undervoltage;
- unchanged inactive `dashcamd` and AP-fallback services.

Ignored raw result files and their SHA-256 values:

- `artifacts/pi-m7-20260727/dashcam-m7-generation-20260727j.json`:
  `263ea7a674fe45c307b25ec7df1c84ee9fd851c14b19e1f9b2a69790dd0fcc6e`
- `artifacts/pi-m7-20260727/dashcam-m7-generation-20260727k.json`:
  `e579376c15032d84877ab985f96cbf7a8561553de8359099142a55f6601c1fea`

## Fail-closed corrections made before acceptance

Earlier exclusive targets were retained as failed evidence. They exposed and
closed these harness defects without touching production paths:

- PyGObject `Gst.Bin.add/remove` return-value ambiguity: authoritative parent
  postconditions now decide success.
- mixed event/buffer probe access and composite `BLOCK_DOWNSTREAM`: observers
  are type-specific and handoff probes use `BLOCK | BUFFER`.
- typed `Gst.BufferFlags` handling and ffprobe ASCII-column contamination in
  real NAL parsing.
- probe-acquisition cleanup, queued safety-bus draining, startup rollback, and
  true callback-to-release block-duration measurement.
- sequential audio-probe installation: both inputs are now coordinated in the
  same pipeline-running-time domain.
- recovery settling was separated from steady-state drift while preserving the
  strict requirement that every restored clip remain below 100 ms.

## Remaining gate

Production code must adopt the immutable-generation ownership model without
weakening pending-path, finalization, sidecar, or camera-ownership contracts.
After local integration and deployment, the owner-assisted physical
unplug/reconnect test must still prove video continuity, per-clip audio truth,
stable device-identity rejection, and bounded recovery. Repeated lifecycle and
retired-generation resource bounds remain open until specifically measured.
