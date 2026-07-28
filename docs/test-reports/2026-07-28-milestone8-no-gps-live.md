# Milestone 8 configured-GPS-absence recording — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Release: `0.1.0.dev0-d72d3067350d3552`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope

This is an exact-Pi configured-device-absence simulation for the Milestone 8
task “Start recording with no GPS and preserve boot ID, sequence, and monotonic
clip/sample times.” The physical GPS module remained connected; no user action,
physical unplug, or reboot occurred. The test temporarily configured the
deliberately nonexistent device
`/dev/dashcam-gps-deliberately-absent`, then restored `/etc` to the exact
bundle hash
`f7d25b1c24be3dba7c7a6bd75d7cdf75d79f3f31baa86933d6dfa6a296af6555`
after the run.

It qualifies that one task, not the later no-GPS boot/late-lock/loss/reconnect
matrix task.

## Recording behavior

At 15 seconds, the ordinary production recorder entry point under the bounded
test configuration remained `RECORDING` with GPS `UART_UNAVAILABLE`, zero GPS
connections/bytes/lines/fixes, six bounded unavailable errors, zero pipeline
restarts, storage `READY`, audio `MATCHED`, and hardware H.264.

The final status was still `RECORDING` with GPS unavailable and nine bounded
attempts/errors. Its active/final clip sequence was 383 and
`pipeline_restart_count=0`.

## Finalized media and metadata

Sequence 383 finalized as a full 60-second clip and sequence 384 as the clean
shutdown fragment. Independent `ffprobe` reported durations of 60.032000 and
21.396666 seconds. Both have boot ID
`601693e3-fa96-427e-906b-1621463a15cd`, are sequential, and preserve the exact
boundary
`end_monotonic(383) == start_monotonic(384)`.

Both sidecars retain nullable UTC/local fields, `UNSYNCED`,
`MONOTONIC_ONLY`, and `gps.available=false`. Both clips are A/V media with
1080p H.264 High/4.1 plus 48 kHz mono AAC-LC. Status, sidecar, and `ffprobe`
evidence is ignored under `artifacts/pi-m8-20260728`.

## Shutdown and boundary

The bounded recorder process ended after clean finalization. The installed
`dashcamd.service` remained inactive with its prior `Result=success` and
`NRestarts=0`; the Pi reported `throttled=0x0`. The simulation proves
configured-GPS absence does not disturb recording, identity progression,
monotonic media metadata, or clean shutdown. It does not prove physical unplug
behavior, startup across a reboot, late GPS lock, loss/reconnect, or the wider
final M8 fault matrix.
