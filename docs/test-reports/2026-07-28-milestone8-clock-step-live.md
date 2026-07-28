# Milestone 8 system-clock ownership and clock-step isolation — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Release: `0.1.0.dev0-7fd1e73debb731b6`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope

This result accepts the Milestone 8 system-clock-owner and media-clock-step
task. It identifies exactly one active Linux wall-clock owner on the approved
image, applies a controlled positive wall-clock step while the production
recorder is running, and verifies packet-level PTS/DTS continuity, media
decode, recorder continuity, clean shutdown, and clock-owner recovery.

It does not claim that GPS disciplines the Linux system clock. GPS anchors
remain the canonical product source for media UTC reconciliation, while the
pipeline/monotonic clock remains the only source of media timing. Durable
sidecar and filename reconciliation remains open.

## Exactly one wall-clock owner

The approved Raspberry Pi OS image uses:

- `systemd-timesyncd.service`, package `systemd-timesyncd`
  `257.9-1~deb13u1+rpi1`;
- enabled, active, and synchronized after the test;
- timezone `Asia/Jerusalem`;
- no active `chrony`, `ntp`, `ntpsec`, or `gpsd` service.

This selects the stock `systemd-timesyncd` service as the sole Linux
wall-clock owner for the SSH-first development image. The recorder does not
set wall time and has no broad clock-setting privilege.

## Controlled wall-clock step

The ordinary production recorder was started and sequence 390 was allowed to
span the test. Immediately before the step:

- monotonic time was `214338900412243` ns;
- realtime was `1785257776379165139` ns.

The privileged test command requested a direct `+120 seconds` UTC step and
reported `2026-07-28T16:58:16.487589196Z`. The active time owner restored the
clock before the next bounded observation; restoration was observed within
`254377301` ns. The same sole owner was then explicitly restarted and was
again active and synchronized.

The accepted GPS anchor remained identical in the pre-step, post-step, and
final status:

- monotonic time `214303957097844` ns;
- UTC `2026-07-28T16:55:41.400Z`;
- source `GPS_RMC_VALID`;
- uncertainty 250 ms.

System-minus-GPS observations were 35.851 ms before the step, 28.643 ms after
restoration, and 28.642 ms later. These observations prove recovery of the
system wall clock; they do not make it a media clock.

## Media continuity

Sequence 390 covered monotonic interval
`[214303924665525, 214363915858018)` and had a sidecar duration of
`59991192493` ns. Its canonical sidecar retained 425 GPS samples, audio
availability, and nullable UTC values with `MONOTONIC_ONLY` quality.

Packet inspection found:

- 1,800 H.264 video packets on time base `1/3000`;
- video PTS and DTS deltas of 99–101 ticks, with no non-positive delta;
- fixed video packet duration of 100 ticks and an IDR first packet;
- 2,815 AAC packets on time base `1/48000`;
- exact audio PTS and DTS deltas of 1,024 ticks, with no non-positive delta;
- H.264 High/4.1 plus AAC-LC mono 48 kHz;
- full hardware `h264_v4l2m2m` video decode and audio decode passed;
- format duration 60.053333 seconds and measured bitrate 8,130,853 bit/s.

The deliberate wall-clock step therefore produced no PTS/DTS jump, reversal,
or discontinuity.

## Recorder and shutdown result

Lifecycle remained `RECORDING` and time status remained `GPS_TIME_VALID`
before, during, and after the step. Final counters were 2,705 raw frames,
2,705 encoded frames, zero drops, zero pipeline restarts, zero service
restarts, and `throttled=0x0`.

The service stopped with `Result=success`, `ExecMainStatus=0`, and
`NRestarts=0`. `systemd-timesyncd` remained enabled, active, and synchronized.

## Evidence

Privacy-safe accepted result:

- `artifacts/pi-m8-20260728/m8-clock-step-result.json`
- SHA-256
  `ab3070953f3ac0796b7f7f25161a328789d81fa52b442e5d9840f25b14186a0b`

Supporting ignored evidence:

- packet metadata SHA-256
  `b272d2c0a71c25e53e11af68103e0386248a66d2a96a9feca3fd23eebe8323e0`
- ffprobe SHA-256
  `912b48dc7770c6db3a5cbd4717dcf57ccf4a18738b6c8d811f42e4de20273a96`
- pre-step status SHA-256
  `94fcde22abebd86173d79966743951b92fdcc073b2fb265ba501282fde7a6163`
- post-step status SHA-256
  `de32cc1b0ee97a48b3439924e0d39fc6af1b960aa2dc11f4495afbc898d77798`
- final status SHA-256
  `91ba31744fba5dfd56ed63cef29b5db5431aea65a2745da5cb5d6fae776f7b36`

No coordinate-bearing sidecar copy was retained on Windows. The canonical
production sidecars remain on the verified dashcam volume.

## Conclusion

The approved image has one selected Linux wall-clock owner, and a deliberate
two-minute wall-clock step did not alter media PTS/DTS or disturb recording.
This checks the system-clock ownership and media clock-step task only.
