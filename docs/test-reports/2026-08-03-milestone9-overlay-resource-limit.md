# Milestone 9 native-overlay resource-limit result (2026-08-03)

## Scope and status

This report records diagnostic, production-integrated exact-Pi measurements of
the recorder-owned native NV12/DMABUF overlay.  They used the IMX219 at the
unchanged 1920x1080/30 profile, `v4l2h264enc`, optional USB AAC, the configured
GPS UART, and verified `DASHCAM` exFAT storage.  The tested runtime path was
the lightweight v7 wheel (SHA-256
`12761d42144abf776868582d2b6308de5a497e2b8df9ab873bb4fa7617cd7e98`) built
from Git `d4741f9` plus the committed GPS/overlay hot-path optimizations.

After the runtime-only measurement, the exact v7 wheel was installed dormant
as hash-closed release `0.1.0.dev0-5f95dd806342ac9e`.  Its manifest,
`SHA256SUMS`, and wheel SHA-256 values are respectively
`619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655`,
`a42983edbf0c85acc44609c7961fe48ab9847ff03d339ab05e8c40dbed1c24c8`,
and `12761d42144abf776868582d2b6308de5a497e2b8df9ab873bb4fa7617cd7e98`.
Exact-version apply and the separate idempotent plan/apply made zero package
changes and started no services.  Deployment does not convert the short
diagnostic run into the required Section C1 paired ten-clip acceptance matrix.

## Result

The active GPS/audio/overlay run took 75 one-Hz samples after a 30-second
warm-up.  It kept hardware 1080p30 recording, storage `READY`, audio
`MATCHED`, and overlay `ACTIVE`; it advanced 2,103 encoded/renderer frames and
had zero dropped frames, pipeline/service restarts, renderer/sync failures,
or throttling.  RSS mean was 61,685.6 KiB (408 KiB growth; 61,880 KiB maximum)
and temperature ranged from 45.084 to 48.312 C.

Its recorder-process CPU mean was **97.3591%**, p95 was **100.9876%**, and
maximum was 104.9857%.  Section C1 requires overlay-arm p95 at or below 100%.
Therefore this is a strict near-pass but **fails** the CPU p95 gate; it must
not be rounded or reported as an acceptance pass.  Privacy-safe result SHA-256
is `5c2ee308496728736b5aa9e6e3af106d080a249f6a4e59dca2cde7be4a0e6b55`.

The repeated v6 arm independently reached mean 97.3988%, p95 100.9879%,
maximum 107.9863%, with zero drops.  Its privacy-safe result SHA-256 is
`910de9a43b062436ca5f1cc141033a032b2f2a25a8dc1e7fc0f250dc5f4aa841`.
The earlier v4 arm was worse (mean 102.7721%, p95 106.9867%) and recorded
three drops, so it is rejected historical evidence.

A separate 30-sample diagnostic after final installation measured mean CPU
96.9534%, p95 99.9871%, and maximum 101.9844%, while advancing 902 encoded
frames with zero drops, pipeline/service restarts, renderer/sync failures, or
throttling.  Its privacy-safe result SHA-256 is
`597c394e8604aaa8ca6facc905903df1a8d0c601db4c89997968a014b14ba27e`.
This short observation demonstrates that the installed wheel runs normally;
it neither overrides the reproducible 75-s p95 failures nor substitutes for
the paired ten-clip acceptance matrix.

## Attribution and bounded follow-up evidence

Contemporaneous configured-GPS-absent arms retained the same production media
path and measured:

| Arm | Mean CPU | p95 CPU | Drops |
| --- | ---: | ---: | ---: |
| Overlay on | 90.7865% | 93.9792% | 0 |
| Overlay off | 80.9548% | 82.9893% | 0 |
| Earlier optimized overlay on | 90.1196% | 91.9880% | 0 |

This supports a bounded overlay cost but does not replace the active-GPS
Section C1 comparison.  GPS-only receive tests reduced process CPU from phase
2's 10.4467% to phase 3's 8.3741%, then 7.8989%, and 5.7699% with a 100 ms
coalescing limit, while preserving approximately 10 Hz valid throughput.  A
privacy-safe five-second sentence-shape observation retained headers and
integrity classes only: all 850 checksums were valid (50 GGA, 50 RMC, 750
other sentences).

An unsafe identity-cache proposal was refused: a DMABUF identity does not bind
per-buffer `GstMemory` geometry or video metadata.  The renderer therefore
retains per-buffer validation rather than weakening the fail-isolated media
contract.

## Next acceptance work

Keep Milestone 9 and its formal resource/exit tasks unchecked.  Before an exit
decision, run the prespecified warm-up plus at least ten consecutive one-minute
clips per no-overlay and overlay arm under the same active GPS, audio, storage,
power, and ambient conditions.  All Section C1 gates, including p95 CPU at or
below 100%, remain mandatory.

Raw privacy-safe evidence is ignored under
`artifacts/pi-m9-20260803/dashcam-m9-dmabuf-evidence-7a29e2c2b24e74b0/`.
