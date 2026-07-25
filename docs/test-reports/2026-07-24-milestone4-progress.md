# Milestone 4 Pi capability progress

## Status and scope

- Date: 2026-07-24
- Target hostname: `dashcam-pi`
- Initial target address during the run: `192.168.68.107`
- Replacement reference board address after the owner swap: `192.168.68.110`
- SSH ED25519 fingerprint:
  `SHA256:raSpiKKrQ/VoZlbgBw+3amZgTz8uj1EvWvSbEqX+DNw`
- Authorization: owner-approved SSH, capability probes, evidence-required boot
  configuration, and reboots
- Excluded: repartitioning, formatting, exFAT provisioning, application
  deployment, power interruption, and acceptance claims

Milestone 4 is complete. The media foundation, OS architecture, camera, UART
mapping, GPS protocol/baud/UTC, USB-audio encode and reconnect path, muxer
candidate, overlay format, same-owner preview stream, and reference power
capacity are recorded. Recording-volume provisioning remains a separate
destructive gate.

## Reference hardware and image

| Item | Observed value |
| --- | --- |
| Board | Raspberry Pi Zero 2 W Rev 1.0, revision `902120`; replacement reference serial `00000000db28ffe4` |
| Camera | IMX219, 3280x2464 10-bit RGGB, CSI |
| Card | nominal 32 GB / 29.3 GiB, CID `fe34325344000000200000031a0192d1`, date 02/2025 |
| GPS | FlyFishRC M10 Mini; receive-only UART at 115200 baud, checksum-valid live GGA/RMC fixes |
| USB capture audio | `08bb:2902` PCM2902 / C-Media USB PnP Sound Device; mono S16LE at 44.1/48 kHz; no unique serial |
| Power supply/controller | Unspecified-model regulated 5 V / 2.5 A supply; owner confirmed there is no hold-up or safe-shutdown controller |
| Image | Raspberry Pi reference image 2026-06-18, pi-gen `314262cb286b8f33327a6f0cbabe14c625021ca0`, stage2 |
| Distribution | Raspbian GNU/Linux 13.4 (`trixie`) |
| Architecture | 32-bit `armhf`, `armv7l`; current kernel is the v7 build |
| Kernel | `6.18.34+rpt-rpi-v7`, package `1:6.18.34-1+rpt1` |
| Python/systemd | Python 3.13.5; systemd 257 |
| Camera stack | rpicam-apps 1.12.0; libcamera 0.7.1+rpt20260609 |
| GStreamer | 1.26.2; libcamera, base, good, bad, X/Pango plugins |
| FFmpeg/Picamera2 on Pi | Not installed; local FFmpeg 7.1 was used for independent artifact validation |

The GStreamer capability packages installed with `--no-install-recommends`
required 204 packages and about 405 MB. The later release image should review
whether this can be reduced without removing required source, encoder, parser,
muxer, audio, overlay, and network functionality.

## Boot configuration changes

### IMX219

The flashed image had `camera_auto_detect=1`, but no camera enumerated. The
original file was saved before changing only the camera selection:

```text
camera_auto_detect=0
dtoverlay=imx219
```

After the reboot, libcamera enumerated the IMX219. The relevant local snapshots
and SHA-256 values are:

- `2026-07-24-pi-config-before-imx219.txt`:
  `1A312A2C82554D6D9BB49B09C66F1E137284C0676E84EC6501E9A013C27AA928`
- `2026-07-24-pi-config-imx219.txt`:
  `B0B863A5B34F0205242D15BF4EEA7AA6A1AF76CBCB25E0949869098554B7884F`
- Pi backup: `/boot/firmware/config.txt.pre-imx219-20260724T0725Z`

### GPS UART foundation

The initial image had no `/dev/serial0` and still included
`console=serial0,115200`. The selected reliable GPS foundation uses the
hardware-timed PL011 and deliberately gives up unused Bluetooth:

```text
enable_uart=1
dtoverlay=disable-bt
```

The serial console entry was removed from `cmdline.txt`. After reboot:

- `/dev/serial0` resolves to `/dev/ttyAMA0`.
- GPIO 14/15 resolve to TXD0/RXD0.
- The live kernel command line contains only `console=tty1`.
- The camera still enumerates.
- `get_throttled=0x0`.

Snapshots:

- `2026-07-24-pi-config-uart-pl011.txt`:
  `DCCA090418E2B8C03165F66B53D62D92336D4AF4B25E6CBD315E16B2EFF226A7`
- `2026-07-24-pi-cmdline-uart-pl011.txt`:
  `8C3210E4D74FFF8C15E0D97C2AEB36CA368DB2887F195B631F6A90409B587461`
- Pi backups:
  `/boot/firmware/config.txt.pre-uart-20260724T0750Z` and
  `/boot/firmware/cmdline.txt.pre-uart-20260724T0750Z`

The connected FlyFishRC M10 Mini was read receive-only at 115200 baud. A bounded
capture contained 1,308 checksum-valid records with zero checksum failures,
including valid GGA/RMC fixes at about 10 Hz. The repository parser accepted the
required sentences. Exact location data is deliberately excluded. This short
capability run does not replace later loss/reconnect and endurance tests; see
`2026-07-24-pi-gps-audio.md`.

## Camera and encoder evidence

The camera reports these sensor modes for both 10-bit packed Bayer and 8-bit
Bayer:

| Mode | Maximum reported rate |
| --- | ---: |
| 640x480 | 200.16 fps |
| 1640x1232 | 81.07 fps |
| 1920x1080 | 47.57 fps |
| 3280x2464 | 21.19 fps |

The 1080p path selects sensor format `1920x1080-SBGGR10_1X10`, Unicam
`1920x1080-pBAA`, and ISP output NV12/BT.709. Unicam is `/dev/video0`; the
hardware H.264 encoder is `/dev/video11`.

The encoder exposes:

- H.264 byte-stream access units.
- baseline, constrained-baseline, main, and high profiles.
- levels through 5.1, including 4.1.
- 25 kbit/s through 25 Mbit/s bitrate control and VBR/CBR mode.
- GOP/I-frame interval, forced keyframes, and repeated sequence headers.
- zero B frames on this device.

A direct 15-second `rpicam-vid` proof at 1920x1080, 30 fps, 8 Mbit/s, High
Profile Level 4.1, GOP 30, and inline headers produced:

- 442 timestamped frames from 0 through 14697.482 ms.
- Mean frame interval 33.327624 ms, or 30.0051 fps.
- 14,736,118 bytes, or approximately 8.021 Mbit/s.
- 15 I frames and 427 P frames.
- temperature 44.5 C before and 46.2 C after.
- `get_throttled=0x0` before and after.
- open file descriptors for `/dev/video11`, camera, media, ISP, and CMA devices.

The raw H.264 file is independently decodable and starts with an IDR. Raw
elementary H.264 has no container duration, so the MP4 tests below are the
authoritative duration/finalization evidence.

## Selected production video path

The first target implementation should use one in-process graph:

```text
IMX219
  -> libcamerasrc 1920x1080 NV12 30/1
  -> optional NV12 textoverlay
  -> v4l2h264enc /dev/video11
       8 Mbit/s CBR, High/4.1, GOP 30, repeated headers
  -> h264parse config-interval=-1
  -> splitmuxsink async-finalize=true
       mp4mux fragment-duration=1000 ms
       approximately 60-second requested IDR splits
```

`dashcamd` remains the only camera owner. `rpicam-vid` is retained as an
independent diagnostic path, not the production owner. Picamera2 is absent and
adds no capability needed by the measured GStreamer graph.

The short GStreamer split test produced five independently decodable H.264 High
Profile Level 4.1 MP4 files at approximately 8 Mbit/s. Every file began with an
IDR. Aggregate continuity is correctly indeterminate because manual
`gst-launch` did not emit the future daemon's monotonic boundary manifest.

The exact selected fragmented/async split graph was then killed without EOS
after about 17 seconds. Three completed files and the active partial file all
remained independently demuxable and decodable. This is capability evidence,
not a claim that arbitrary exFAT power loss is safe.

## Muxer comparison

| Profile | Abrupt process kill result | Decision |
| --- | --- | --- |
| Standard non-fragmented `mp4mux` | 9.24 MB file; no `moov`; unplayable | Reject for the active clip |
| `mp4mux fragment-duration=1000` | 8.97 MB, 8.998 s; independently decodable | Viable |
| Robust MP4 with 1-second periodic `moov` updates | 9.42 MB, 9.332 s; independently decodable | Viable fallback |
| Async `splitmuxsink` plus 1-second fragmented `mp4mux` | completed segments and 2.1-second active crash fragment all decodable | Selected candidate |

The `reserved-prefill=true` robust mode was rejected by the muxer for this codec
configuration. Robust periodic updates without prefill worked. Fragmented MP4
is selected because it matched the specification preference and the interrupted
active-file test.

## Overlay and preview probes

`textoverlay` accepts NV12 directly, avoiding a full-frame RGB conversion. In
otherwise comparable 12-second fakesink runs:

| Graph | Process CPU | RSS | Result |
| --- | ---: | ---: | --- |
| 1080p30 hardware H.264, no overlay | about 56.3% of one core | 23,848 KiB | negotiated and ran |
| same graph with one static telemetry line | about 101% of one core | 32,892 KiB | negotiated and ran |
| 1080p30 recording plus same-owner 640x360 stream | about 59.8% of one core | 23,768 KiB | negotiated and ran |

These short process samples are useful comparisons, not endurance budgets.
Dynamic telemetry updates still need an implementation test.

The same `libcamerasrc` successfully produced 1920x1080 NV12 and 640x360 NV12
simultaneously. Both source pads must request 30 fps. Requesting 15 fps on the
secondary source pad also reduced recording to 15 fps. The supported pattern is
30 fps on both libcamera pads followed by a bounded leaky preview queue and
downstream `videorate` reduction to 15 fps.

One manual dual-stream fakesink graph did not finish its forced EOS after
`SIGINT` and was killed after the deadline. Preview stays disabled until the
production graph proves bounded shutdown and no recording regression.

## Resource, storage, and health observations

- Idle after the UART reboot: 425 MiB RAM total, about 321 MiB available.
- zram swap: 424 MiB configured; 4 KiB in use in the final idle sample.
- Temperature across the short media tests: approximately 42.9 C through
  52.1 C.
- Every recorded throttling/undervoltage value: `0x0`.
- Direct 256 MiB read from the ext4 card partition: 23.2 MB/s.
- The stock image expanded ext4 root across the card: 512 MiB FAT boot plus
  28.8 GiB ext4 root.
- `/srv/dashcam` does not exist, exFAT tooling is absent, and no write benchmark
  was performed.

The present card layout cannot satisfy the product's separate ext4 state and
exFAT `DASHCAM` contract. Do not shrink the mounted root or format this card
under the capability authorization. Milestone 5 requires an expendable,
explicitly approved reflash/provisioning target.

The owner confirmed that the vehicle installation has no power hold-up or
safe-shutdown controller. Abrupt vehicle-power loss must therefore remain an
explicit high-risk deployment limitation; software shutdown cannot protect the
active file or exFAT metadata when input power disappears without warning.

At 2026-07-24 11:24:18 +03:00, the Pi was cleanly powered off with
`systemctl poweroff` before the owner connected new hardware. The pre-shutdown
state was 44.0 C with `get_throttled=0x0`; SSH and ICMP were both offline
afterward.

## Probe and validator corrections discovered on hardware

The target run found and locally tested three compatibility issues:

1. The capability probe now uses `systemctl --version`, not the nonexistent
   `systemd --version` command.
2. `/dev/serial0` uses `readlink -e` so a missing link cannot be reported as a
   resolved mapping.
3. GStreamer availability uses bounded `gst-inspect-1.0 --exists` checks instead
   of overflowing the raw-output limit.
4. The media validator supports FFmpeg 7.1's combined `packets_and_frames`
   output while retaining legacy separate-array support and strict bounds.

Focused validation passed: 35 tests, Ruff, and mypy. The final integrated local
suite passed lock verification, formatting, Ruff, mypy, and 573 tests.

## Evidence index

- Final schema-valid capability report:
  `2026-07-24-pi-capability-post-uart.json`
  (`7AA168F32D1845610CE4E6BC1D6A76DE18C78BB353BBC8C5366EABEE4C9E92C8`)
- Standard clean split validation:
  `2026-07-24-pi-gstreamer-splitmux-validation.json`
- Standard/fragmented/robust crash comparison:
  `2026-07-24-pi-mux-recovery-validation.json` and
  `2026-07-24-pi-standard-mp4-abrupt-validation.json`
- Selected fragmented split validation:
  `2026-07-24-pi-selected-fragmented-splitmux-validation.json`
- Selected active-fragment crash validation:
  `2026-07-24-pi-selected-fragmented-active-crash-validation.json`
- Raw H.264 diagnostic validation:
  `2026-07-24-pi-h264-validation.json`
- Ignored binary/video evidence:
  `artifacts/pi/2026-07-24/`
- Privacy-safe GPS and USB-audio report:
  `2026-07-24-pi-gps-audio.md`
- Ignored PCM/AAC evidence:
  `artifacts/pi/2026-07-24/mic-mono-48k-20260724-01.wav` and
  `artifacts/pi/2026-07-24/mic-aac-128k-20260724-01.m4a`
- Ignored post-reconnect AAC evidence:
  `artifacts/pi/2026-07-24/mic-aac-reconnected-128k-20260724-01.m4a`

## Milestone 4 completion

The owner identified the reference supply as an unspecified-model regulated
5 V / 2.5 A unit. This records available source capacity, not measured Pi
consumption or proof of connector voltage under peak load. Instrumented
power/voltage and endurance measurements remain later acceptance work.

All Milestone 4 tasks and its capability decision exit gate now have evidence.
The absence of a hold-up/shutdown controller remains a declared deployment risk.

## Replacement-board smoke test

After the initial capability work, the owner moved the same configured SD card
and IMX219 to an older Pi Zero 2 W. On 2026-07-24 the replacement board was
identified and tested:

- Host: `dashcam-pi` at `192.168.68.110`.
- Model/revision: Raspberry Pi Zero 2 W Rev 1.0, `902120`.
- Serial: `00000000db28ffe4`.
- Wi-Fi MAC: `2c:cf:67:98:4c:49`.
- SSH host fingerprint matched the SD card's previously recorded ED25519
  fingerprint before the new address was trusted.
- `config.txt` and `cmdline.txt` hashes exactly matched the selected IMX219 and
  PL011 configurations.
- `/dev/serial0` still resolved to `/dev/ttyAMA0`.
- The IMX219 enumerated with all previously observed modes.
- A five-second 1920x1080, 30 fps, 8 Mbit/s, High Profile Level 4.1 hardware-H.264
  stream to `/dev/null` exited successfully.
- Temperature changed from 40.8 C to 41.3 C, `get_throttled=0x0`, and swap
  remained unused.

The replacement assigned Unicam to `/dev/media3`, whereas the first board had
reported `/dev/media2`. The codec media node moved in the opposite direction;
camera `/dev/video0` and encoder `/dev/video11` remained stable in this sample.
Implementation must still discover the media graph and must not hard-code
`/dev/mediaN` numbering.
