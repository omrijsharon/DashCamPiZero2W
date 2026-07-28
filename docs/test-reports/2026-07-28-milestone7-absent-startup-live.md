# Milestone 7 microphone-absent startup — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Release: `0.1.0.dev0-09a6dd3b374d3952`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope decision

The owner clarified that the USB microphone is expected to remain connected in
normal use. The required v1 behavior is that the dashcam works when the
configured microphone is present and also when it is absent at recorder
startup. Physical hot-unplug/replug, runtime restoration, repeated physical
cycling, naturally reassigned-index, and physical wrong-device qualification
are not current acceptance gates. Stable identity and non-substitution remain
required.

The already implemented logical microphone-loss/restoration path and its saved
evidence are retained as additional resilience. They do not need to be repeated
as physical unplug/replug qualification.

## Preconditions

- The exact USB path
  `/sys/devices/platform/soc/3f980000.usb/usb1/1-1/authorized` was absent before
  service start.
- No validation harness or other camera owner was running.
- `dashcamd.service` was inactive with `NRestarts=0`.
- The Pi reported `throttled=0x0`.

## Ordinary production-service result

`dashcamd.service` was started normally at `2026-07-28T15:45:56+03:00`.
It reached `active/running` and lifecycle state `RECORDING` with:

- audio state `UNAVAILABLE`;
- audio reason `not_found`;
- no matched/effective audio device;
- zero encoded AAC access units;
- hardware H.264 through `/dev/video11`;
- 1920×1080 NV12 at 30 fps, High profile, level 4.1;
- target video bitrate 8,000,000 bit/s;
- storage state `READY` on the verified exFAT volume;
- pipeline restart count zero;
- systemd restart count zero; and
- `throttled=0x0`.

The ordinary completed clip was sequence 368:

| Check | Result |
| --- | --- |
| MP4 | `boot-601693e3fa96-000368.mp4` |
| Sidecar | `boot-601693e3fa96-000368.json` |
| Duration | 59.989 seconds |
| Size | 60,022,261 bytes |
| Video | H.264 High/4.1, 1920×1080 |
| Measured bitrate | 8,004,489 bit/s |
| Streams | One video stream; no audio stream |
| First frame | Key frame, I picture |
| Decode | Full `v4l2h264dec` hardware decode exited 0 |
| Sidecar audio truth | `audio.available=false`; codec/rate/channels/bitrate null |
| Sidecar storage path | Finalized under `/srv/dashcam/clips`, not rootfs |

SHA-256:

- sidecar:
  `0b9063943d5fdbee932873308b07ef1db2b5332974b2e9ddbfb4fb7b29253b6e`
- MP4:
  `6e50c88f33e3067551aa2c2674a044fc7b827f77b0ce4c8bda21601ffb006708`

## Shutdown fragment and final state

A normal `systemctl stop dashcamd.service` finalized sequence 369 as a second
video-only MP4/JSON pair. It was 59.055667 seconds and its sidecar also reported
`audio.available=false`. The shutdown clip reported one dropped frame; the
ordinary accepted sequence 368 reported none. This isolated observation does
not affect the microphone-absent startup result and is retained truthfully.

SHA-256:

- shutdown sidecar:
  `18d825a1452eee362f4567ff00c456444bc148ff19d89929e239880dc93efed8`
- shutdown MP4:
  `ca32244530d7478b7e73d7475ed8b598d2b3f2ad6ad17ed9135937939b63c008`

Final service state:

- inactive;
- `Result=success`;
- `ExecMainStatus=0`;
- `NRestarts=0`; and
- `throttled=0x0`.

## Superseded physical-hotplug attempt

Immediately before the scope clarification, one owner-assisted physical-unplug
run exercised release `0.1.0.dev0-09a6dd3b374d3952`. It failed the optional
handoff's strict A/V boundary at 157,590,331 ns, then failed closed and released
the camera; the production service stayed inactive with zero restarts and no
throttling. The result is retained only as a scoped-out diagnostic:

- `artifacts/pi-m7-20260727/m7-production-physical-result-3.json`, SHA-256
  `71a075e5528bb115b5847ba24f284e864ef9c14da78c128632f007726c871ea7`
- `artifacts/pi-m7-20260727/m7-production-physical-result-3.stderr`, SHA-256
  `f1a25b0dedb6e916d51e3f1d3c294862c02b313c95d062c25c676ae391b66c16`

It is not an acceptance blocker because physical hot-unplug/replug is no longer
part of the current product scope.

## Conclusion

Milestone 7 passes under the clarified v1 contract: the configured microphone
produces synchronized AAC when present, and the ordinary production recorder
starts and records truthful video-only clips when it is absent. No further
physical microphone unplug/replug work is required.
