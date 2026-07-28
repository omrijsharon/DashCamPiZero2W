# Milestone 5 live Pi validation — 2026-07-26

## Scope and reference state

This report covers the remaining autonomous Milestone 5 checks on the exact
SSH-first Raspberry Pi OS Lite 32-bit Trixie installation. The USB microphone
was intentionally disconnected during the initial checks, then connected and
validated later in the same final-image campaign.

- Pi Zero 2 W, `armhf`, kernel `6.18.34+rpt-rpi-v7`
- final boot ID: `601693e3-fa96-427e-906b-1621463a15cd`
- exact card CID: `fe34325344000000200000031a0192d1`
- recording mount: `/dev/mmcblk0p3`, exFAT `DASHCAM`, UUID `7EED-3EA7`
- final boot-config SHA-256:
  `59efe771dfd2544a2a0eabe190559b70a3b210fc02f79a0f338e7ffb1286eeef`
- final cmdline SHA-256:
  `1646970fa792eccecca03d85ada59297880cce555e71b262d5827e167d04e56a`
- final throttle state: `0x0`
- final root free space after fallback installation/validation:
  3,250,245,632 bytes

The small raw evidence files are retained in the ignored local directory
`artifacts/pi-validation-20260726/`. The validated camera MP4/H.264 payloads
were deliberately not copied into the repository; their hashes and validator
results are retained instead.

## Storage-preflight refusal matrix

The installed production `dashcam.storage.preflight` entry point was exercised
in a new private mount namespace for every case. The worker first detached only
its cloned recording mount, then used either the underlying rootfs directory or
a newly formatted disposable loop image. The network namespace was never
unshared.

All six cases passed with exit 2, `ready=false`,
`probe_attempted=false`, and the exact expected reason:

| Case | State | Exact reasons |
|---|---|---|
| unmounted/rootfs fallback | `FAULTED` | `UNMOUNTED`, `MISSING_MOUNT_IDENTITY`, `READ_ONLY`, `MISSING_SENTINEL`, `INVALID_SPACE` |
| wrong filesystem | `FAULTED` | `WRONG_FILESYSTEM` |
| wrong label | `FAULTED` | `WRONG_LABEL` |
| wrong UUID | `FAULTED` | `WRONG_UUID` |
| wrong sentinel | `FAULTED` | `WRONG_SENTINEL_IDENTITY` |
| read-only exFAT | `READ_ONLY` | `READ_ONLY` |

After every case, the parent proved:

- the exact real mount/sentinel snapshot was unchanged;
- the loop-device inventory returned to its exact baseline;
- NetworkManager and SSH remained active;
- `nmcli` responded and localhost port 22 returned an SSH protocol banner.

The final matrix reported `ready=true`,
`real_recording_device_formatted=false`, and
`real_recording_mount_mutated=false`. Raw evidence SHA-256:
`8f3d03617231aca68427e54a49ba451e565113785d7313dd2178dc506d608813`.

## Bounded `fsck.exfat` matrix

The fsck harness ran in a private mount namespace and operated only on three
64 MiB regular-file images attached to proven loop devices.

- Clean: read-only `fsck.exfat -n` returned 0 and the whole-image hash stayed
  byte-identical.
- Repairable: one byte of the main boot-region checksum was changed while the
  backup boot region remained intact. Read-only fsck returned 4; bounded
  `fsck.exfat -y` restored the main region from the backup and returned 1; the
  final read-only check returned 0.
- Failed: both root-directory-cluster fields were invalidated. Automatic
  `fsck.exfat -p` returned 4, the whole image stayed byte-identical, and its
  `wipefs`/`blkid` identity did not change.
- Exactly one formatter invocation created the disposable seed before the
  cases were cloned. No fsck path invoked a formatter.

The harness ended with `auto_format_count=0_for_all_fsck_cases` and
`completed=true`. Raw evidence SHA-256:
`a4e0a63d896d680dbfc968bd413311f82aa6805f86d92445b51bb31938b30b16`.

## exFAT write/finalization performance

The benchmark revalidated the exact CID, mount device, filesystem, label,
UUID, sentinel, read-write state, and inactive recorder before creating one
unique test directory. It reserved the greater of 2 GiB or 15% of capacity;
15% won at 3,669,963,571 bytes.

- Sustained: 536,870,912 bytes in 30.190242837 seconds, approximately
  17.78 MB/s (16.96 MiB/s).
- Sustained rename plus `sync -f`: 37.914927 ms.
- Eight 16 MiB bursts: write latency 0.875–1.808 seconds; finalize latency
  26.519–34.433 ms.
- The reserve was preserved.
- The trap removed only its explicit files and unique directory. Free space
  returned exactly to the pre-test 24,465,375,232 bytes.

This is storage-path evidence, not recorder/overlay endurance evidence. Raw
evidence SHA-256:
`78b2994988a0ce8745502d1983486606e14da69ae22e4b748629118be77e424a`.

## Camera and encoder

The explicit `imx219` overlay enumerated one IMX219 and `libcamerasrc`
negotiated 1920×1080 NV12 at 30/1. The V4L2 inventory dynamically identified
`bcm2835-codec-encode`, with NV12 input and H.264 output; the observed node was
`/dev/video11`, but production must continue to discover it rather than
hard-code it.

Raspberry Pi's working hardware-H.264 camera path produced a bounded sample:

- H.264 High Profile, Level 4.1
- 1920×1080
- average frame rate approximately 30 fps
- duration 9.699903 seconds
- video bitrate 8,001,069 bit/s
- independent ffmpeg decode passed
- first packet and frame were keyframes
- the first packet contained an H.264 IDR NAL unit
- throttle state stayed `0x0`; temperature stayed 45.1 °C

The installed project media validator reported overall `pass`. Validator JSON
SHA-256:
`30497cd4481fc45959178721d98aa67d2e00b7ee14609598cf636bd814803d1b`.

The initial GStreamer `v4l2h264enc` graph was **not accepted**. It failed
with `Failed to process frame` and kernel
`bcm2835_codec_start_streaming: Failed enabling i/p port, ret -3`, even with a
synthetic source at 640×480 and 1920×1080. A controlled experiment increasing
GPU memory from 64 MiB to 128 MiB did not change the failure. That change was
fully reverted and a second controlled reboot restored the proven config hash
and 64 MiB default. At this point the defect appeared to block production
GStreamer recorder work in Milestone 6; the successful rpicam sample alone
could not be treated as proof that the proposed GStreamer backend worked.

### Milestone 6 follow-up: explicit encoder caps

The conclusion above was narrowed later on the same boot. Bounded reruns using
Raspberry Pi's documented
`extra-controls="controls,repeat_sequence_header=1"` followed by explicit
H.264 level caps passed IMX219 inputs at 640x360 and 1920x1080. Explicit
1920x1080 High/4.1 caps also passed. The earlier implicit level-1 negotiation
was invalid at 1080p.

One-control-at-a-time isolation then found that bitrate 8 Mb/s and GOP 30 pass
individually and together under the encoder's default VBR mode. Every case
setting `video_bitrate_mode=1` reproduced the first-frame `STREAMON` failure
and kernel `ret -3`, irrespective of bitrate-control order. The final bounded
constrained-VBR stream passed at 1920x1080/30, High/4.1, 8 Mb/s target, GOP 30,
repeated headers, no B frames, and one-second keyframes. It measured
8,221,871 bit/s, decoded independently without errors, added no kernel message,
and left the Pi unthrottled.

This selects the constrained-VBR encoder configuration for Milestone 6, but
does not yet accept continuous `dashcamd`, segmented MP4, recovery, ten-clip,
or endurance behavior. See
`docs/test-reports/2026-07-26-gstreamer-explicit-caps.md`.

## GPS/UART

The final image satisfies the hardware mapping:

- `/dev/serial0` resolves to PL011 `/dev/ttyAMA0`;
- the kernel cmdline does not assign a serial console;
- `enable_uart=1` and `dtoverlay=disable-bt` are active;
- the validation reader opened the UART receive-only at 115200 and transmitted
  no bytes;
- no coordinates were written to output or evidence.

The first 30-second indoor/obstructed window received 100,589 bytes, 300 valid
GGA sentences, and 299 valid RMC sentences. A second 45-second window received
154,421 bytes, 449 valid GGA sentences, and 450 valid RMC sentences. After all
autonomous installation work, a final 90-second window received another
301,589 bytes, 900 valid GGA sentences, and 899 valid RMC sentences. The live
transport and parser therefore work, but the receiver reported fix quality 0,
zero satellites, invalid navigation, and no trusted RMC/ZDA anchor in all
three windows.
The GGA time field tracked current UTC, but policy correctly refused to trust
it without a valid fix.

Retry evidence SHA-256:
`01615cf826bcd794ea146ea66f41916f949c5f255dbe4ced1d5fb921be573612`.
Final 90-second evidence SHA-256:
`9532a0719c26091e139762138d7d3ef6a32d2190fb480e58c47b5ef79f82bf12`.

After the owner moved the antenna into the open, a fourth privacy-safe
120-second receive-only window passed the navigation/time gate:

- 712,532 bytes received at 115200;
- 1,199 valid GGA and 1,200 valid RMC sentences;
- 965 autonomous and 187 differential-quality GGA fixes after 47 initial
  no-fix samples;
- 6–7 reported satellites;
- 1,153 active RMC records and trusted UTC-anchor candidates;
- first ten system-minus-GPS observations between 0.040 and 0.070 seconds;
- no coordinates retained or emitted.

Open-sky evidence SHA-256:
`5921ca3b3c1d092a1a67d7c3b51061e45421e0fece4d60859024db733f2ba2e0`.

The main agent independently repeated a 15-second check through the installed
repository parser. All 150 GGA records reported differential fix quality 2
with seven satellites, all 150 RMC records produced
`RMC_STATUS_VALID` trusted anchors, and the first ten system-minus-GPS
observations were 0.032–0.037 seconds. No coordinates were retained.
Independent evidence SHA-256:
`9f5e5ad247d10623c0a1b1baee065db97b1c06a06b1da8d3b17281a0bd853516`.

## USB microphone

The connected microphone was dynamically identified as the expected single
USB capture device:

- USB VID:PID `08bb:2902`, model `USB_PnP_Sound_Device`;
- direct-root physical path
  `platform-3f980000.usb-usb-0:1:1.0`;
- native S16_LE mono at 44.1 or 48 kHz;
- ALSA card 1 was recorded as an observation, never used as the stable
  selector.

A bounded tmpfs-only ALSA capture produced exactly 3.000 seconds and 144,000
frames of 48 kHz mono S16_LE PCM. Independent readback matched the requested
format. The ambient signal was quiet but genuine: 98.53125% nonzero samples,
-62.286 dBFS RMS, -42.888 dBFS peak, and zero clipped samples.
Read-only mixer inspection found capture enabled at its minimum step
(`0%`, reported as `0.00 dB`) with automatic gain control enabled. No mixer
setting was changed. The low ambient level is therefore not treated as a
calibrated in-vehicle loudness result.

The final-image standalone production audio branch also passed:

```text
alsasrc 48 kHz mono S16_LE
  -> bounded leaky queue
  -> audioconvert
  -> audioresample
  -> voaacenc 128000
  -> aacparse
  -> mp4mux
```

The bounded M4A contained AAC-LC, 48 kHz mono at 127,997 bit/s and decoded
independently to 47,104 PCM frames. Its decoded signal was non-silent and
unclipped. All raw/encoded/decoded tmpfs payloads were deleted after their
hashes and metrics were retained. No capture process remained; `dashcamd` and
the fallback unit stayed inactive; throttle state remained `0x0`.

Sanitized microphone evidence SHA-256:
`c4bba0053b989ae8aeb1749a168ec8278f37bff1434cb8b07700aeb6ff067ad4`.

This proves final-image microphone identity, capture capability, non-silent
signal, and standalone AAC encoding. It does not claim calibrated input gain,
integrated camera/audio mux synchronization, or hot-unplug recovery; those
remain later tasks.

## Home Wi-Fi and installed fallback service

`wlan0` (verified MAC `2c:cf:67:98:4c:49`) was connected in infrastructure
mode at `192.168.68.107/24` with a default route and local-subnet route.
NetworkManager and SSH were active. The connection name was redacted from
saved evidence; internet reachability was not required.

Home-Wi-Fi evidence SHA-256:
`538ddd79c75c1074c678dc9a970c793855c7b375bf55f5de51a2c85cf0ad12c3`.

The SSH-development installer was extended with a reviewed
`dashcam-network-fallback.service` that runs
`/opt/dashcam/current/venv/bin/python -m dashcam.network_fallback`. The
installer hash-closes and semantically validates the unit, orders it after
NetworkManager and cloud-init, enables it for later boots, and never starts or
restarts it during installation.

The full local suite passed with 1,064 tests and 10 host-capability skips. The
transferred closed bundle had archive SHA-256
`3c009de5d61c84275b33896d8a0c163fe9ac5be2f1262b67608c7e2e874018eb`
and manifest SHA-256
`e355ff0d095142b4680e8a46d9ac9e5661d09a409540014dc4e21136e12d83a9`.
The first authoritative dry run passed with zero missing or solver packages:

- plan SHA-256:
  `2e35f4ecd29bd60d7b34a3523c3ecff6782d0d18a3c2a651a5fa4f72f89823e1`;
- apply SHA-256:
  `0b44a332e1a1c27d049cd10bba55a0b25fbeeeaac22e6b0cafcc51eff40ab60f`;
- idempotency plan SHA-256:
  `d14ef4f20ac852c1a4abd7435ad7c6b07c4a63a33405b8aa7440da5edb871a7b`;
- idempotency apply SHA-256:
  `13450beeedda722e4e45a64b10f7e223ac6bf7a0c709b85b0361475f2bf491ca`.

The initial pre-plan correctly refused because the conservative one-GiB
installation budget left less than two GiB projected free. Cleaning only
re-downloadable APT cache data recovered 89,509,888 bytes; the regenerated
plan then projected 2,195,238,912 bytes free. No APT refresh occurred between
that plan and apply. The second apply installed no packages, started no
services, and left root free space unchanged at 3,250,249,728 bytes.

Installed release `0.1.0.dev0-4c0df4d9372fd9ea` passed its import smoke test.
The fallback unit is root-owned mode `0644`, SHA-256
`990f21227cc2b9789565128c61e7301230fed6188ff6981e9a8f5c5f6f46d0e8`,
enabled, inactive, and has never started
(`ExecMainStartTimestampMonotonic=0`). Its private state directory is
root-owned mode `0755`.

Manual execution of the newly installed client path completed in 1.151
seconds. The home-Wi-Fi connection UUID and `192.168.68.107/24` address were
unchanged, the fallback unit remained inactive, NetworkManager and SSH
remained active, and none of the NetworkManager profile, private credential,
or boot credential handoff files existed afterward.

AP activation was intentionally not attempted over the sole current
home-Wi-Fi SSH route. The only remaining owner-assisted network check is the
deliberate connection-loss/AP/recovery test.

## Live harness corrections

Target execution found and corrected several validation-harness assumptions:

- util-linux 2.41 has no `losetup --autoclear`; the harness now uses
  `--nooverlap`, explicit detach, and parent-side exact inventory recovery;
- a private overmount made `findmnt --mountpoint` return multiple rows, so each
  worker now detaches only its private cloned mount before modeling a case;
- exFAT rejects ownership changes, so disposable sentinel creation relies on
  mount-assigned ownership and uses bounded `sync -f`;
- `blockdev` is `/usr/sbin/blockdev` on this image;
- the original fsck volume label exceeded exFAT's accepted length;
- exfatprogs 1.2.9 ignores the dirty flag for this purpose, so the repair case
  now uses a deterministic primary/backup boot-region checksum recovery.

Each correction was locally regression-tested, hash-verified after transfer,
and rerun on the Pi before accepting evidence.

## Result

Passed:

- storage-preflight negative matrix;
- bounded clean/repairable/unrepairable fsck behavior with no auto-format;
- bounded exFAT write/finalization benchmark and cleanup;
- IMX219/NV12 capability and Raspberry Pi hardware-H.264 sample;
- receive-only GPS UART/module/parser communication plus a valid open-sky
  navigation fix and trusted UTC anchor;
- USB microphone stable identity, PCM capture, and standalone AAC-LC
  encode/decode;
- home-Wi-Fi association/local route plus SSH/NetworkManager survival;
- reviewed fallback-unit installation, idempotency, and healthy-client
  behavior without AP activation.

Still open:

- continuous GStreamer recorder integration, segmented MP4, recovery,
  ten-clip, and endurance behavior;
- owner-assisted activation/recovery test of bounded AP fallback.
