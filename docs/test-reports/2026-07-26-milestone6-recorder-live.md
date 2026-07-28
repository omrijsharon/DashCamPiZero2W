# Milestone 6 live recorder implementation

Date: 2026-07-26

## Result

The first production video-only recorder path is implemented, installed, and
validated on the declared Pi Zero 2 W. The service owns one IMX219 camera,
records 1920x1080 NV12 at 30 fps through the dynamically selected Raspberry Pi
`v4l2h264enc`, rotates fragmented MP4 at a one-minute closed-GOP boundary, and
stops cleanly through systemd after finalizing the active fragment.

This completes the initial pipeline, production entry point, bounded
fragment-event/finalization path, and ordinary one-minute rotation tasks. It
does not complete Milestone 6: durable MP4+JSON promotion, restart
reconciliation, recovery/backoff, ten-clip continuity, and two-hour endurance
remain open.

## Installed release

- Release: `0.1.0.dev0-cd6b7bfb566787ac`
- Manifest SHA-256:
  `2b930485cd9435b8cff8581e4aff056eeab7d1e7885791af17b69daa07e2d2e0`
- Exact boot ID: `601693e3-fa96-427e-906b-1621463a15cd`
- Exact card CID: `fe34325344000000200000031a0192d1`
- `python3-gst-1.0`: exact installed version `1.26.0-1`
- `dashcam` supplementary groups: `video dashcam-storage`
- Installer replay: zero APT work, same release, and unchanged final root
  headroom
- Final root free space: `3,144,630,272` bytes
- Final exFAT free space: `24,078,843,904` bytes

The authoritative deployment refreshed APT indexes once before the saved plan
and did not refresh them between plan and apply. Both the corrected release and
its idempotency replay used a hash-closed bundle and exact-version/no-upgrade
installer. The installer enabled `dashcamd.service` without starting it;
hardware runs were initiated and stopped separately under observation.

## Production graph

The live graph is:

```text
libcamerasrc
  -> video/x-raw,1920x1080,NV12,30/1
  -> v4l2h264enc
       repeat_sequence_header=1
       video_bitrate=8000000
       h264_i_frame_period=30
       default hardware VBR mode
  -> video/x-h264,profile=high,level=4.1
  -> h264parse config-interval=-1
  -> bounded non-leaky recording queue
  -> splitmuxsink, 60-second requested boundaries
       async-finalize=true
       mp4mux
       fragment-duration=1000 ms
       fragment-mode=dash-or-mss
```

No encoder device number is hard-coded and
`video_bitrate_mode=1` is never requested.

## Exact-target defects and resolutions

Three fail-visible diagnostics were resolved before acceptance:

1. An untyped `first-moov-then-finalise` value was accepted by the GStreamer
   parser but rejected when applied to `mp4mux`. The enum value was made
   explicit.
2. Explicit `first-moov-then-finalise` reproducibly failed to complete the
   shutdown contract, including a 20-second clip with a 25-second EOS bound.
   The exact stack's supported fragmented `dash-or-mss` mode was selected.
3. The exact GStreamer stack posts
   `splitmuxsink-fragment-closed` for the active output after accepting EOS but
   does not post a pipeline-level EOS. An unfiltered bus probe proved the
   closure event and absence of EOS. Shutdown now accepts either EOS or the
   validated closure of the currently active fragment. It refuses a stale
   prior-fragment closure and always proceeds to the bounded `NULL` transition.

The final short and post-rollover service stops both completed with systemd
`Result=success`, main exit status 0, no restart, and `state=STOPPING`.

## One-minute rollover evidence

The final accepted service run remained `state=RECORDING`, with zero restarts
and `vcgencmd get_throttled=0x0`, across the boundary:

| Provisional output | Duration | Size | Bitrate | Start |
| --- | ---: | ---: | ---: | --- |
| `boot-601693e3fa96-000009.partial.mp4` | 59.988667 s | 60,023,584 B | 8,004,656 bit/s | IDR/I at 0.000000 s |
| `boot-601693e3fa96-000010.partial.mp4` | 35.993333 s | 36,030,711 B | 8,008,307 bit/s | IDR/I at 0.000333 s |

Both files report H.264 High Profile, Level 4.1, 1920x1080, and 30/1 fps.
Independent FFmpeg decoding of the first ten seconds of each file completed
without an error. The second file was finalized by the observed systemd stop.

The run proves ordinary split rotation without restarting the service,
camera, or encoder. It does not substitute for the later ten-clip normalized
continuity and two-hour endurance gates.

## Local validation

Focused final validation:

```text
96 recorder entry point, daemon, runtime, and GStreamer tests passed
full suite after the catalog-finalization foundation: 1158 passed, 10
Windows-only POSIX-semantic skips
ruff .: all checks passed
mypy --strict src: no issues in 66 source files
```

The wider recorder/storage/deployment integration selection passed 210 tests
with six Windows-only POSIX-semantic skips before the target deployment.

## Final service state and retained diagnostics

- `dashcamd.service`: enabled, inactive, last accepted result successful
- `dashcam-storage-check.service`: enabled and active
- `dashcam-network-fallback.service`: enabled and inactive; AP activation was
  not performed
- Recorder diagnostic outputs `000000` through `000010` remain under
  `/srv/dashcam/pending`. They are implementation-created evidence, not
  promoted product clips, and have no fabricated JSON sidecars.

The next implementation slice must add durable pair intent, sidecar creation,
collision-safe promotion into `clips`, and restart reconciliation before these
pending artifacts are treated as catalogued recordings.
