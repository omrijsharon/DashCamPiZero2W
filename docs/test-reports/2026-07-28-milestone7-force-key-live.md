# Milestone 7 unscheduled force-key capability — exact Pi

Date: 2026-07-28  
Reference Pi release: `0.1.0.dev0-a566fafd3e04c7fc`  
Final harness manifest SHA-256:
`70ac5ac25adc220b8995e870d8c351e09329227f335470fba4884bfa63e12b89`

## Accepted result

The final hash-closed capability run passed on the exact Pi. Its retained
result is
`artifacts/pi-m7-20260727/m7-forcekey-result-11.json`, SHA-256
`b2a5668a06356c90a87e1646387fe2814d1fac71669cbd1e1c2e9f7ab0b87ea8`.
The retained stderr contains only expected libcamera information and has
SHA-256
`8e74a35955bf1a134e7daced510b7e7ac599545284ba6b2680bd82f59a76635b`.

The harness used the production IMX219 1920x1080 NV12 at 30 fps and
Raspberry Pi hardware-H.264 High/4.1 at 8 Mbit/s with repeated headers. It
sent three official upstream `GstForceKeyUnit` requests directly on the
hardware encoder source pad at different ordinary-GOP phases. Each unique
request count produced a downstream `all_headers=true` event and a first
non-delta NAL type 5 buffer:

| Request | Media-time response | Wall-clock response | Seqnum behavior |
| --- | ---: | ---: | --- |
| 1 | 99,984,603 ns | 98,776,605 ns | request 130, downstream 131 |
| 2 | 99,980,428 ns | 94,390,721 ns | request 164, downstream 165 |
| 3 | 66,653,074 ns | 59,684,546 ns | request 168, downstream 169 |

All three media-time responses were strictly below the literal 100 ms
capability ceiling. The encoder assigns a new downstream seqnum, so production
must correlate the event by its unique count and `all_headers` flag rather than
requiring seqnum preservation.

Raw and encoded counters both observed 148 frames with zero PTS regression or
large gap. Three IDR-started MP4s were finalized and independently decoded
through `h264_v4l2m2m`. The camera, encoder, pipeline clock, and base time
remained unchanged. `dashcamd.service` stayed inactive/dead with `NRestarts=0`;
no audio, sysfs, service, or production-catalog operation occurred;
`throttled=0x0` held before and after. Root free space after the run was
2,757,521,408 bytes.

This result proves only the bounded forced-IDR capability. Production
microphone-loss containment must separately prove that forced-IDR PTS minus
the last AAC access-unit end is strictly below 100 ms, then prove the
three-slot fragment handoff and restoration lifecycle.

## Refused iterations retained

Earlier result/stderr pairs are retained beside the accepted result. They
exposed only bounded diagnostic-observer defects or a deliberately stricter
engineering target:

- the first run selected a rootfs evidence directory not writable by the
  unprivileged account;
- two runs compared the typed storage state to lowercase text;
- one run polled more slowly than its selected phase window;
- one run parsed only Annex-B while the finalized MP4 packet was
  length-prefixed AVC;
- the 70 ms provisional margin rejected measured three-frame responses around
  99.98 ms; and
- direct encoder injection proved that downstream seqnum is not preserved.

Each refusal relinquished the camera/encoder, left the recorder service
inactive with no restart, and left throttling at zero. The harness was not
weakened to claim production restoration: its final
`safe_to_integrate_production` value remains `false`.
