# GStreamer explicit-caps follow-up — 2026-07-26

## Scope and safety

The owner authorized a bounded rerun of Raspberry Pi's documented GStreamer
encoder caps on the current exact Pi. The test:

- used boot ID `601693e3-fa96-427e-906b-1621463a15cd`;
- used kernel `6.18.34+rpt-rpi-v7` and GStreamer 1.26.2;
- confirmed the camera/encoder were unused and the recorder unit inactive;
- wrote bounded H.264 streams only to RAM-backed temporary storage;
- did not change packages, boot configuration, GPU memory, services, or exFAT;
- copied the exact temporary logs into the ignored evidence directory; the
  Pi's per-user session cleanup removed the RAM-backed source directories;
- retained compact logs under ignored
  `artifacts/pi-validation-20260726/`;
- left the Pi unthrottled at `gpu=64M` with no camera/encoder owner.

## Passing documented-caps matrix

Every level-4 case used:

```text
v4l2h264enc extra-controls="controls,repeat_sequence_header=1"
! video/x-h264,level=(string)4
```

The High-profile case changed the downstream caps to explicit
`profile=(string)high,level=(string)4.1`. No `device` property was assigned.

| Case | Negotiated result | EOS / exit | Independent validation |
| --- | --- | --- | --- |
| Synthetic 640x480 NV12 at 30 fps | Baseline, level 4 | Natural EOS / 0 | 150 frames; full decode passed |
| IMX219 640x480 NV12 at 30 fps | Baseline, level 4 | Natural EOS / 0 | 149 frames; full decode passed |
| IMX219 1920x1080 NV12 at 30 fps | Baseline, level 4 | Natural EOS / 0 | 149 frames; full decode passed |
| IMX219 1920x1080 NV12 at 30 fps | High, level 4.1 | Natural EOS / 0 | 149 frames; full decode passed |

The one-frame difference is the bounded `identity eos-after=150` behavior on
the live-camera cases, not evidence of a capture-rate result. The negotiated
caps in each verbose log report 30/1. Raw elementary-stream probing is not used
as timing evidence because it does not retain container timestamps.

The High/4.1 stream was 6,161,187 bytes and had SHA-256
`c6ed232809665f8a134ecb92f25b83a51048be5c0fad5952427b8f6754c8a2b2`.
All four outputs had exit-zero full FFmpeg decodes and successful frame-count
probes. The post-test kernel delta contained no entries.

## Production-control boundary and isolation

A subsequent exclusive 1920x1080 test retained the passing source and caps but
added this combined control structure:

```text
controls,repeat_sequence_header=1,video_bitrate_mode=1,
video_bitrate=8000000,h264_i_frame_period=30
```

That pipeline failed on its first frame with GStreamer
`STREAMON 3 (No such process)` and kernel
`bcm2835_codec_start_streaming: Failed enabling i/p port, ret -3`. It produced
zero media bytes. The Pi was unthrottled and its validated 64 MiB GPU-memory
configuration was unchanged.

The result proves that the exact Trixie image can run GStreamer's hardware
encoder with explicit valid caps. It does **not** prove that level caps were the
only earlier problem: an earlier failed graph had already negotiated High/4.1.

A same-boot matrix retained 1920x1080/30 High/4.1 and varied only the V4L2
control structure:

| Controls after `repeat_sequence_header=1` | Result |
| --- | --- |
| none | pass |
| `video_bitrate=8000000` | pass |
| `h264_i_frame_period=30` | pass |
| bitrate 8 Mb/s plus GOP 30 | pass |
| `video_bitrate_mode=1` | fail: `STREAMON` / kernel `ret -3` |
| CBR mode then bitrate 8 Mb/s | fail: `STREAMON` / kernel `ret -3` |
| bitrate 8 Mb/s then CBR mode | fail: `STREAMON` / kernel `ret -3` |

This isolates `video_bitrate_mode=1` as the reproducible trigger on the exact
stack. The specification permits constrained VBR, so the selected Milestone 6
candidate retains the hardware encoder's default VBR mode and sets only:

```text
controls,repeat_sequence_header=1,video_bitrate=8000000,
h264_i_frame_period=30
```

The final bounded stream with those controls and explicit High/4.1 caps:

- negotiated H.264 High Profile, Level 4.1, 1920x1080 at 30/1;
- reported no B frames;
- ran for 9.464811 seconds and produced 9,727,308 bytes;
- measured 8,221,871 bit/s at the transport-container level;
- began on an independently verified IDR keyframe and emitted keyframes at
  approximately one-second intervals;
- completed a full independent FFmpeg decode with zero stderr bytes;
- produced no kernel-log delta and left throttling at `0x0`;
- preserved boot ID `601693e3-fa96-427e-906b-1621463a15cd` and `config.txt`
  SHA-256
  `59efe771dfd2544a2a0eabe190559b70a3b210fc02f79a0f338e7ffb1286eeef`.

The ten-second timeout wrapper returns 124 by design, but GStreamer received
the interrupt, emitted EOS, finalized the MPEG-TS stream, and shut down cleanly.
This is bounded encoder evidence, not continuous recorder, splitmux/MP4,
recovery, ten-clip, or endurance acceptance.

## Evidence

Passing rerun:

- `artifacts/pi-validation-20260726/dashcam-gst-explicit-caps-3guKDJlK/`
  (`evidence-notes.txt` identifies two wrapper-generated files that are excluded
  from acceptance evidence)

Independent documented-pipeline corroboration and production-control failure:

- `artifacts/pi-validation-20260726/dashcam-gst-official.Kyd9xJ/`
- `artifacts/pi-validation-20260726/dashcam-gst-official-1080p.kwWOPz/`
- `artifacts/pi-validation-20260726/dashcam-gst-production-1080p.O97xft/`

Final cap/control isolation and selected constrained-VBR stream:

- `artifacts/pi-validation-20260726/dashcam-gst-cap-matrix.TK2qtT/`
- `artifacts/pi-validation-20260726/dashcam-gst-control-matrix.WkAFwK/`
- `artifacts/pi-validation-20260726/dashcam-gst-production-1080p.YuN0ay/`
  (includes the passing repository media-validator result)

One attempted 1080p run never constructed a pipeline because the installed
`libcamerasrc` lacks the proposed `num-buffers` property. It is retained as
harness-failure evidence only:

- `artifacts/pi-validation-20260726/dashcam-gst-official-1080p.OlHO3g/`

No Milestone 6 task or exit gate is checked by this diagnostic. The first
Milestone 6 task remains unchecked until the selected graph is integrated into
the single-owner continuous `dashcamd` pipeline and its required acceptance
checks pass.
