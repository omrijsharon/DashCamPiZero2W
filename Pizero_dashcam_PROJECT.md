# Raspberry Pi Zero 2 W GPS Dashcam

## Project directive

Build a reliable, self-contained dashcam for a **Raspberry Pi Zero 2 W**. The device must boot and begin recording without user interaction, record continuous 1080p30 H.264 video with USB-microphone audio into one-minute clips, obtain UTC date/time and navigation data from a UART GPS receiver, burn selected telemetry into the video, expose a local smartphone web interface through its own Wi-Fi access point, and continuously reclaim storage by deleting the oldest unprotected clips.

Reliability of recording is the highest priority. GPS, microphone, Wi-Fi, web UI, and preview failures must not stop the video recorder.

This document is both the product specification and the implementation contract for the coding agent.

---

## 1. Core design decisions

1. **Only one component may own the camera.** Do not run two independent camera capture processes.
2. Use one continuous camera pipeline. Rotate the output container/file at clip boundaries without restarting the camera sensor.
3. Use **hardware H.264 encoding**. Do not silently fall back to software H.264 at 1080p30.
4. Segment only at H.264 IDR/keyframe boundaries so every finalized clip is independently playable from its first frame.
5. Use a **monotonic clock** for media timing and clip continuity. Wall-clock corrections must not alter video presentation timestamps.
6. Treat GPS time as UTC. Convert it to local display time with an **IANA timezone identifier**, for example `Asia/Jerusalem`; never hard-code a UTC offset or Israel daylight-saving dates.
7. Start recording even before GPS time is valid. Use provisional names and reconcile timestamps after a valid GPS time anchor is received.
8. Keep canonical metadata and filenames in UTC. Local time is for overlays and UI display.
9. Store telemetry in a sidecar file as well as burning a human-readable subset into the video.
10. The storage loop prevents a full filesystem; it does **not** prevent SD-card wear or physical failure. Use a high-endurance card and minimize unrelated writes.
11. Use a dedicated **exFAT recording partition** on the primary microSD card so completed videos and sidecars are directly readable on Windows. Keep the Linux operating system on ext4 and the Pi boot files on FAT32.
12. Keep pending and finalized media on the same exFAT filesystem so finalization can use a same-filesystem atomic namespace rename. Flush the file and containing directory/filesystem as supported and validated on the target image; rename atomicity does not by itself guarantee power-loss durability. Keep application state, configuration, and the durable clip index on ext4.
13. Never silently write recordings into the ext4 root filesystem when the exFAT partition is missing, damaged, read-only, or not mounted. Enter an explicit storage fault instead.
14. The system microSD card must never be hot-removed. Provide a controlled “prepare card for removal” action that finalizes the active clip, flushes data, unmounts the recording partition, and powers down the Pi.
15. exFAT provides Windows interoperability, not journaling or immunity to power-loss corruption. Treat a power hold-up/safe-shutdown circuit as strongly recommended for vehicle deployment.
16. Treat an MP4 file and its JSON sidecar as one **recoverable logical clip**, not as a filesystem-atomic pair. Persist intent, make each individual file operation atomic where possible, and reconcile interrupted pair operations after reboot.
17. Do not commit to a camera source element, encoder device, muxer profile, preview transport, or 32/64-bit OS architecture until it is probed and measured on the exact target image.

---

## 2. Target hardware

Required:

- Raspberry Pi Zero 2 W.
- Raspberry Pi-compatible CSI camera supported by the installed libcamera/rpicam stack.
- UART GPS receiver that outputs NMEA sentences.
- USB audio input dongle or USB microphone.
- High-endurance microSD card.
- Stable regulated power supply suitable for the Pi, camera, GPS, and USB microphone.

Recommended:

- 32 GB or larger high-endurance microSD card.
- A power-loss-safe shutdown circuit, UPS HAT, supercapacitor solution, or ignition controller for vehicle use.
- GPS receiver with PPS output for a later precision-timing upgrade. PPS is not required for version 1.

Hardware details that must remain configurable:

- Camera model and sensor mode.
- GPS UART device path, expected default `/dev/serial0`.
- GPS UART baud rate. The measured reference FlyFishRC M10 Mini release
  configuration defaults to `115200`; support at least `4800`, `9600`,
  `38400`, `57600`, and `115200` for other receivers. Do not assume every GPS
  ships at the reference rate.
- GPS UART mapping. On a Pi Zero 2 W, `/dev/serial0` normally maps to the clock-sensitive mini UART while the PL011 is assigned to Bluetooth. Phase 0B must either validate the mini UART with a fixed core-clock configuration or deliberately remap the PL011, then record the Bluetooth/power consequences. All GPIO UART signals are 3.3 V only.
- USB audio device, selected by stable ALSA identity or udev rule rather than a volatile card index such as `hw:1,0`.
- USB physical topology and power budget. The microphone uses the Zero 2 W's single USB OTG data port and may require a suitable OTG adapter or powered hub; do not back-power the Pi through an unsafe hub.
- Optional GPIO event/protect button.

---

## 3. Operating-system target

- Raspberry Pi OS Lite supported by Raspberry Pi Zero 2 W.
- Headless operation.
- The current `libcamera`/`rpicam` camera stack installed and camera auto-detection or device-tree configuration verified. Do not depend on the retired legacy-camera enable switch or legacy `raspivid` stack.
- UART enabled and the Linux serial console disabled on the selected GPS UART.
- NetworkManager or an equivalent OS-managed Wi-Fi access-point configuration.
- `systemd` for service management and automatic restart.
- `tzdata` installed and kept as a declared system dependency.
- The in-kernel exFAT driver and `exfatprogs` installed as declared dependencies.
- DashCam Bootstrap v1: a compressed custom Raspberry Pi OS Lite 32-bit image
  with the application, its environment, dependencies, and first-boot payload
  preinstalled. The retained release artifact is `.img.xz`, accompanied by an
  Imager manifest and hashes; a raw `.img` is only a temporary build product.
- No custom initramfs is permitted for DashCam Bootstrap v1. Its first-boot
  storage transaction runs as ordinary post-root systemd services.

The installation documentation must record the exact tested OS image and release date, 32/64-bit architecture, kernel, camera stack, GStreamer/FFmpeg, Python, exFAT driver, and `exfatprogs` versions. Do not assume plugin names or hardware encoder device nodes without probing the target image. Because the Pi Zero 2 W has only 512 MB RAM, the OS architecture is a measured release decision, not a preference.

### 3.1 Primary microSD partition layout and Windows interoperability

The release image must use one physical microSD card with three partitions:

| Partition | Label | Filesystem | Purpose | Default sizing policy |
|---|---|---|---|---|
| 1 | `bootfs` or image-provided boot label | FAT32 | Pi firmware, kernel, and boot configuration | Keep the image-provided size, normally a few hundred MiB |
| 2 | `rootfs` | ext4 | Raspberry Pi OS, application, configuration, logs, and durable state | Target 6 GiB on a nominal 32 GB card, configurable after a capacity check |
| 3 | `DASHCAM` | exFAT | Pending/finalized videos, JSON sidecars, protected clips, and recovery artifacts | Use all remaining space |

Sizing notes:

- A nominal “32 GB” card contains about 29.8 GiB before partitioning. With an approximately 0.5 GiB boot partition and a 6 GiB root partition, the recording partition will usually be close to **25 GB decimal**, or approximately **23 GiB**. Exact capacity varies by card and image.
- A 6 GiB root partition is allowed only if the fully installed image still has at least 2 GiB of root free space. Otherwise enlarge rootfs and accept lower recording retention.
- Do not hard-code an exact final sector count or assume that all cards sold at the same nominal capacity contain the same number of sectors.
- Cards smaller than the declared minimum supported capacity must fail provisioning cleanly rather than producing undersized or overlapping partitions.

#### Image and first-boot provisioning

Stock Raspberry Pi OS commonly expands `rootfs` to fill the card on first boot.
DashCam Bootstrap v1 prevents that by removing exactly the standalone stock
`resize` token from the build-time boot `cmdline.txt` and adding
`dashcam.bootstrap=v1`.

The pinned 2026-06-18 Trixie armhf Lite source uses Raspberry Pi Imager's
`cloudinit-rpi` customization format. The image manifest must retain that exact
format. Imager may add `ds=nocloud` and seed files while flashing; the build
transformation must preserve all unrelated cmdline tokens and seed files.
Storage and network policy services must be ordered after `cloud-final.service`,
and the storage runtime must independently require terminal successful
cloud-init completion before any mutation. An incomplete or failed cloud-init
run is a non-destructive defer/refusal, not permission to repartition.
Independent image readback must also prove cloud-init's module lists contain no
`growpart` or `resizefs`; Bootstrap Stage A is the only runtime partition
grower.
Legacy `systemd.run`/`firstrun` detection remains a defensive compatibility
check but is not the primary path for this pinned source.

The official source root filesystem has insufficient free space for the full
offline payload. The image builder must therefore verify the official
compressed and extracted hashes, then grow partition 2 and ext4 **offline** to
an exact 4 GiB build-time source size before installing packages. The retained
image must also contain and independently verify an all-zero 4 MiB extent at
the future partition-3 start. Stage A later grows partition 2 from that checked
4 GiB source geometry to the 6 GiB runtime target.

Bootstrap v1 is a normal post-root, two-boot transaction. It must not use a
custom initramfs, a kernel partition-table reread, or `partprobe`:

1. **Stage A (first eligible normal boot):** derive the actual mounted root and
   backing disk; apply the exact CID/layout authorization gate for the
   destructive trial; back up and hash the MBR to both ext4 and FAT; record
   durable intent; then use one `sfdisk --no-reread` write to extend partition
   2 to 6 GiB and create partition 3 in the remaining aligned space. Read the
   raw MBR back, durably record the committed table, sync, and perform exactly
   one controlled reboot.
2. **Stage B (a later boot with a different boot ID):** revalidate the exact
   target, grow the mounted ext4 filesystem online with `resize2fs` (never
   `e2fsck` on the mounted root), and independently re-read the ext4 size before
   advancing durable state. Prove partition 3 was newly created by the exact
   authorized Stage A transaction, has no `blkid`/`wipefs` signature, and has
   the image-authored all-zero 4 MiB prefix before recording format intent.
   Format it once with `mkfs.exfat -n DASHCAM`, and capture its UUID. Mount by
   UUID, create the sentinel and directory tree, write the mount/environment
   configuration, and write the completion marker last.
3. All later boots are no-ops when that completion marker and the verified
   mount agree. Reconciliation is idempotent across a power cut. A foreign,
   torn, or contradictory state is a latched refusal: never auto-restore,
   auto-format, or retry a destructive action.

The installer must not attempt to shrink an already expanded, mounted ext4
root partition. If stock first boot already consumed the card, the supported
recovery is to rebuild/reflash using Bootstrap v1 or repartition offline with
an explicitly documented expert procedure.

Provisioning safety requirements:

- Never run `mkfs` merely because “partition 3 exists.” Verify partition identity, expected bounds, absence of a recognized existing filesystem, and provisioning state.
- Never overwrite an existing `DASHCAM` volume during upgrade. Reformatting is permitted only through an explicit factory-reset workflow with a destructive confirmation.
- Back up the partition table before changing it and retain enough diagnostic information to recover a failed first boot.
- Test the provisioning workflow repeatedly on expendable cards of every supported capacity class.
- The exact 31,457,280,000-byte CID trial authorization applies only to its
  named Pi test card. General-release destructive authorization policy remains
  unresolved and must be explicit before release testing.

#### Linux mount behavior

- Mount the recording volume at `/srv/dashcam` using its filesystem UUID.
- Use tested exFAT mount options that assign ownership to the dashcam service account and restrict access appropriately; exFAT does not provide native Unix ownership or mode bits.
- Use `noatime`; do not use a global synchronous-write mount mode that would unnecessarily reduce throughput and card life.
- The operating system may continue booting if the data volume fails to mount, but `dashcamd` must verify with `findmnt` or an equivalent API that `/srv/dashcam` is a distinct mounted exFAT filesystem with the expected UUID/sentinel before recording.
- A plain directory at `/srv/dashcam` is not sufficient. The recorder must refuse to write there when the mount is absent, preventing accidental exhaustion of rootfs.
- Run a bounded `fsck.exfat` check before mounting when the volume is marked dirty or after an unclean shutdown. Never auto-format a failed volume.
- If repair fails, preserve the volume read-only when possible, report `STORAGE_FAULT`, and require explicit recovery.

#### Windows access behavior

The supported offline workflow targets Windows 10 and Windows 11 with a card reader that exposes multiple partitions:

1. Stop the Pi through the web UI’s **Prepare SD card for removal** action, or otherwise ensure the Pi has fully shut down.
2. Remove the card only after shutdown is complete.
3. Insert it into the Windows computer.
4. Open the volume labeled `DASHCAM` and copy files from `clips/` or `protected/` to the computer before analysis or editing.

Windows may also show the FAT32 boot volume. It will not natively read the ext4 `rootfs` partition and may offer to initialize or format an unknown partition. Documentation and `README-WINDOWS.txt` must clearly say:

- Do **not** initialize, format, or modify the unknown Linux partition.
- Do **not** use the boot partition for recordings.
- Open only the volume labeled `DASHCAM`.

All exported names must be Windows-safe:

- Use ASCII filenames without `:`, `*`, `?`, quotes, angle brackets, pipes, trailing spaces, or trailing periods.
- Do not rely on case-sensitive filename distinctions, symlinks, Unix permissions, extended attributes, or hard links.
- Ignore Windows-created metadata such as `System Volume Information` and `$RECYCLE.BIN`; the retention manager must never treat unknown files as dashcam clips.
- Keep each `.mp4` and its `.json` sidecar adjacent and identically based so they can be copied as a pair.

The project must validate card readability and playback on at least one Windows 10 system and one Windows 11 system using representative card readers. Very old Windows versions and readers that expose only the first removable-media partition are outside the default support target and must be documented if encountered.

#### Safe-removal action

The smartphone UI must provide an authenticated, confirmed action named **Prepare SD card for removal**. It must:

1. Reject new recording/control operations.
2. Mark the device `PREPARING_REMOVAL`.
3. Request an immediate keyframe when supported and finalize the active clip with a bounded timeout.
4. Flush metadata, the clip index, and filesystem buffers.
5. Stop recorder and web-dependent writers.
6. Unmount `/srv/dashcam` cleanly.
7. Trigger an orderly system shutdown through a narrow privileged helper.
8. Before connectivity disappears, tell the user that shutdown is in progress and that the card is removable only after the documented physical power/ACT-LED shutdown cue. Do not claim the card is already safe while the Pi is still serving the page. If a persistent “safe” LED is required after Linux halts, the selected power controller must provide it.

This action is not a substitute for a vehicle power-hold-up circuit. Sudden power loss can still occur, and exFAT has a larger corruption surface than a journaled Linux filesystem.

---

## 4. Default recording profile

| Setting | Default |
|---|---:|
| Resolution | 1920 × 1080 |
| Frame rate | 30 fps |
| Video codec | H.264, hardware encoded |
| Target video bitrate | 8,000,000 bit/s |
| Rate control | Hardware-supported constrained VBR or CBR-like mode |
| H.264 keyframe interval | 30 frames / 1 second |
| Repeat SPS/PPS headers | Enabled at keyframes when supported |
| Clip duration | 60 seconds |
| Container | MP4; fragmented/robust MP4 preferred |
| Audio codec | AAC-LC |
| Audio bitrate | 128,000 bit/s |
| Audio sample rate | 48,000 Hz |
| Audio channels | Mono by default |
| Preview | 640 × 360, 10–15 fps, enabled only while a client is connected |
| GPS sample rate | Receiver-provided rate; retain up to 10 Hz |
| Speed display | km/h by default; configurable |

Expected retention at the default profile:

- Video plus audio payload is approximately `8.128 Mbit/s` before container/filesystem overhead.
- 25 GB of usable space on the exFAT recording partition is approximately **6.8 hours before reserve space and overhead**.
- With the default 15%/20% free-space watermarks, only about 80%–85% of the partition is occupied by managed clips. Before practical overhead, the retained window therefore cycles around **5.5 to 5.8 hours**, not 6.8 hours; expect roughly **5.2 to 5.6 hours** in practice on the example partition. The UI must calculate an estimate from measured rolling bitrate, actual eligible capacity, protected/unknown-file usage, and the stop watermark rather than displaying a hard-coded value.

The exact MP4 muxer settings are a Phase 0B hardware gate. The selected profile must pass independent playback/seek tests on the supported Windows systems and defined power-loss tests. Standard MP4 may be selected only if its active-file loss behavior is accepted and documented; fragmented or robust MP4 may be selected only if its browser/player compatibility is proven. The release must select one tested default instead of leaving “MP4 or fMP4” as a runtime guess.

---

## 5. Functional requirements

### 5.1 Boot and autonomous operation

The system must:

1. Start the recording service automatically at boot.
2. Begin recording without GPS lock, Wi-Fi clients, internet, or a working microphone.
3. Reach active recording state before starting nonessential services when possible.
4. Expose current service state through both the web API and `systemd` status, using `sd_notify`/`STATUS=` (or an equivalent mechanism) so `systemctl status` shows more than process liveness.
5. Restart the media pipeline automatically after a recoverable camera or encoder failure.
6. Use bounded restart backoff to avoid a tight crash loop.
7. Finalize the active clip on a clean shutdown.

Bootstrap services run before the storage check and before any dashcam write,
but independently of networking. Provisioning failure must not block
NetworkManager, SSH, or AP fallback. `dashcamd` may report `STORAGE_FAULT`,
but camera/recording writes remain blocked until the recording mount is
verified.

Recording states:

- `STARTING`
- `RECORDING`
- `DEGRADED`
- `STOPPING`
- `FAULTED`

`DEGRADED` means recording continues but one or more optional subsystems are unavailable.

Recorder state, time state, storage state, and device-operation state are separate fields. For example, a failed recording mount is recorder state `FAULTED` with reason `STORAGE_FAULT`; `PREPARING_REMOVAL` is a device-operation state while the recorder transitions through `STOPPING`. Do not overload one enum with all subsystem states.

### 5.2 Continuous capture and segmentation

The recorder must:

- Keep the camera running continuously across clip boundaries.
- Keep the H.264 encoder running continuously where supported.
- Split the muxed output at approximately 60-second boundaries on closed-GOP IDR/keyframes. Request an IDR at the boundary when the chosen encoder/muxer supports it.
- Never create overlap or intentional gaps between adjacent clips.
- Finalize the previous clip asynchronously where the selected media framework supports it.
- Ensure every completed clip starts with the decoder configuration and an IDR/keyframe.
- Write the active file with a temporary suffix and rename it only after successful mux finalization and file flush. Use a collision-resistant finalized name and fail rather than overwriting an existing clip.

Example lifecycle:

```text
.../pending/boot-<boot_id>-000123.partial.mp4
    -> successful mux finalization
.../clips/20260723T182700.000Z_ba1b2c3d4_s000123.mp4
```

The first and final clips of a boot session may be shorter than 60 seconds. Normal middle clips should be 59–61 seconds unless a documented framework limitation requires a wider bound.

The UTC portion is for humans; the short boot ID and sequence make names unique if clocks are corrected, repeat, or collide. The UUID `clip_id`, not the filename, is the stable API identity.

### 5.3 Audio

- Capture from a configured USB audio device.
- Record mono AAC-LC at 48 kHz and 128 kbit/s by default.
- Timestamp audio from the same pipeline clock used for video.
- Use resampling/timestamp correction where needed to prevent USB-audio clock drift.
- Maintain audio/video synchronization across clip boundaries.
- If the microphone is absent or disconnected, continue recording video-only clips and set `audio_available=false` in status and metadata.
- Attempt microphone recovery without restarting the camera when practical. It is acceptable to restore audio at the next clip boundary.
- Never substitute a different USB audio device merely because its ALSA card index matches.

### 5.4 GPS over UART

The GPS subsystem must:

- Read NMEA from the configured UART.
- Support at least `$GPRMC`/`$GNRMC`, `$GPZDA`/`$GNZDA`, `$GPGGA`/`$GNGGA`, and common multi-constellation talker prefixes.
- Verify NMEA checksums before accepting a sentence.
- Parse UTC date/time, latitude, longitude, ground speed, course, altitude, fix quality, satellite count, and HDOP when available.
- Retain the receiver's native sample cadence up to 10 Hz.
- Mark data stale rather than repeating it indefinitely.
- Distinguish these states:
  - UART unavailable.
  - UART receiving but no valid NMEA.
  - Time valid, position invalid.
  - 2D/3D navigation fix valid.
  - Data stale/lost.
- Reopen the UART after disconnect or read failure without stopping recording.

A navigation fix is not always required to obtain time. Apply sentence-specific trust rules: for example, RMC has a validity status while ZDA has no equivalent navigation-valid flag. A valid checksum alone proves transmission integrity, not that the receiver's date is plausible. Require a complete date and time source, enforce configured plausibility/continuity limits, and report which sentence established each accepted anchor.

### 5.5 Time synchronization and timezone handling

#### Canonical time

- Store all canonical timestamps as timezone-aware UTC.
- Use ISO 8601 UTC formatting in metadata.
- Use UTC in filenames to avoid ambiguous or duplicate local timestamps during daylight-saving transitions.

#### Local time

- Store an IANA timezone name such as `Asia/Jerusalem`.
- Use the system IANA timezone database through Python `zoneinfo` or an equivalent maintained library.
- Derive UTC offset, timezone abbreviation, and daylight-saving state from the UTC timestamp and selected zone.
- Do not maintain custom DST transition tables.

#### No-RTC startup behavior

The Pi may boot with an incorrect wall clock. Therefore:

1. Start media capture immediately using a generated `boot_id`, a monotonic timestamp, and a clip sequence number.
2. Mark time state as `UNSYNCED` until a validated GPS UTC anchor exists.
3. Record `monotonic_ns` for every clip start/end and GPS sample.
4. When GPS UTC becomes valid, create an anchor pair:
   - `anchor_monotonic_ns`
   - `anchor_utc`
5. Derive UTC for earlier and later samples from this anchor.
6. Rename finalized provisional clips when their corrected UTC start time becomes known.
7. Do not base media PTS/DTS on a wall clock that may jump.
8. The service may set the Linux system clock once a valid GPS time is available, but the media pipeline must remain stable if the system clock steps.
9. Reject implausible GPS dates with explicit diagnostics rather than setting the system clock blindly.
10. Assign exactly one component to discipline the system clock. Prefer `chrony` with a GPS source or a narrowly scoped privileged time helper; do not grant the recorder broad root privileges or let multiple time services fight.
11. Track anchor source, age, uncertainty, and disagreement. Do not repeatedly rename a clip as noisy anchors arrive; accept/reject anchors through a documented policy and make filename reconciliation idempotent.

Time status uses independent fields because GPS freshness and Linux wall-clock discipline can coexist:

- GPS time source: `UNSYNCED`, `GPS_TIME_VALID`, or `GPS_TIME_STALE`.
- System clock: `UNSET`, `SYNCING`, `SYNCHRONIZED`, or `ERROR`.
- Canonical timestamp quality: `MONOTONIC_ONLY`, `GPS_ANCHORED`, or `SYSTEM_DERIVED`.

### 5.6 Burned-in video overlay

The recorded image must contain a configurable overlay with:

- Local date and time, including a numeric UTC offset by default; a timezone abbreviation may be shown in addition.
- `REC` indicator.
- Ground speed.
- Latitude and longitude.
- GPS fix/lost state.
- Optional altitude, course, satellite count, and HDOP.
- Optional device name or vehicle label.

Default layout:

```text
2026-07-23 21:27:04 +03:00  REC
31.76832, 35.21371   54 km/h   ALT 782 m   SAT 11
```

Overlay rules:

- Never display stale coordinates or speed as if current.
- After `gps_stale_after_s`, display `GPS LOST` and either hide or explicitly mark the last position as stale.
- Before GPS time is valid, display `TIME UNSYNCED` rather than a plausible but wrong date.
- The overlay must be rendered before the recording encoder so it is physically burned into the stored video.
- Avoid full-frame CPU format conversion for text rendering.
- Prefer a fixed overlay region and a pre-rendered bitmap updated only when displayed values change, then blend only that region into camera buffers.
- Benchmark the overlay on the target Pi. If the required overlay cannot sustain 1080p30, do not silently lower frame rate or switch to software encoding. Report the failed performance gate and retain synchronized sidecar metadata.

### 5.7 Per-clip metadata

Create one sidecar JSON file for every finalized video clip.

Example names:

```text
20260723T182700.000Z_ba1b2c3d4_s000123.mp4
20260723T182700.000Z_ba1b2c3d4_s000123.json
```

Minimum schema:

```json
{
  "schema_version": 1,
  "clip_id": "uuid",
  "boot_id": "uuid",
  "sequence": 123,
  "video_file": "20260723T182700.000Z_ba1b2c3d4_s000123.mp4",
  "start_utc": "2026-07-23T18:27:00.000Z",
  "end_utc": "2026-07-23T18:28:00.000Z",
  "start_monotonic_ns": 123456789000000,
  "end_monotonic_ns": 123516789000000,
  "gps_time_state": "GPS_TIME_VALID",
  "system_clock_state": "SYNCHRONIZED",
  "timestamp_quality": "GPS_ANCHORED",
  "timezone": "Asia/Jerusalem",
  "start_local": "2026-07-23T21:27:00.000+03:00",
  "video": {
    "codec": "h264",
    "width": 1920,
    "height": 1080,
    "fps_nominal": 30,
    "target_bitrate_bps": 8000000,
    "measured_bitrate_bps": 7923000,
    "frames_written": 1800,
    "dropped_frames": 0
  },
  "audio": {
    "available": true,
    "codec": "aac",
    "sample_rate_hz": 48000,
    "channels": 1,
    "target_bitrate_bps": 128000
  },
  "gps": {
    "available": true,
    "first_fix_utc": "2026-07-23T18:27:00.200Z",
    "samples": [
      {
        "monotonic_ns": 123456989000000,
        "utc": "2026-07-23T18:27:00.200Z",
        "lat_deg": 31.76832,
        "lon_deg": 35.21371,
        "speed_mps": 15.0,
        "course_deg": 91.2,
        "altitude_m": 782.0,
        "fix_quality": 1,
        "satellites": 11,
        "hdop": 0.9
      }
    ]
  },
  "protected": false,
  "protection_reason": null,
  "software_version": "git-describe-or-build-id",
  "warnings": []
}
```

Requirements:

- Include units in field names where ambiguity is possible.
- Preserve enough information to regenerate an overlay offline.
- Write JSON atomically through a temporary file plus rename.
- Version the schema from the first release.
- Do not make embedded MP4 telemetry tracks a version-1 requirement.
- Define `start_utc`, `end_utc`, `start_local`, and per-sample `utc` as nullable while time is `UNSYNCED`. Preserve monotonic fields in every case, then update/reconcile the sidecar atomically after a trusted anchor is accepted.
- Record time-anchor provenance/uncertainty and whether a UTC value is GPS-anchored or only system-clock-derived.
- Bound the number and size of samples and warnings per clip; malformed or excessive GPS input must not create unbounded memory or JSON output.

### 5.8 Ring-file retention manager

This is a circular set of finalized files, not an in-memory ring buffer.

The retention manager must:

1. Monitor both free bytes and free percentage on the recording filesystem.
2. Start deletion at a low-water threshold.
3. Delete oldest eligible clips until a high-water threshold is restored.
4. Delete the video and sidecar as one crash-recoverable logical operation. No available exFAT primitive can atomically unlink two files.
5. Persist a `DELETING` intent before unlinking either member, make retries idempotent, and reconcile any interrupted half-deletion on boot without treating an orphan as an ordinary eligible clip.
6. Never delete:
   - The active `.partial` clip.
   - A clip still being finalized.
   - A protected/event clip.
   - A clip currently being downloaded.
   - Files outside the configured recording root.
   - Unknown/non-dashcam files, including Windows-created metadata directories.
7. Use an exclusive lock or transactional state transition so the web server and retention manager cannot race.
8. Represent downloads with bounded leases owned by `dashcamd`; expire abandoned leases after a configured timeout so a crashed web client cannot pin storage forever.
9. Flush directory/filesystem metadata using the behavior supported by the selected kernel/exFAT stack and verify the result during power-loss testing.
10. Continue recording while deletion runs.
11. Emit a critical fault if no deletable files remain and free space is below the emergency threshold.
12. Never delete protected clips merely to continue recording unless an explicit, separately configured emergency policy allows it. Default policy: stop safely before destroying protected evidence.

Default thresholds:

```toml
[storage]
recording_root = "/srv/dashcam"
required_filesystem = "exfat"
required_volume_label = "DASHCAM"
require_distinct_mount = true
low_watermark_percent = 15
high_watermark_percent = 20
minimum_free_gib = 2.0
emergency_free_mib = 256
```

Define the thresholds without ambiguity:

- Start deletion when `free_bytes < max(low_watermark_percent × capacity, minimum_free_gib)`.
- Stop deletion only when `free_bytes >= max(high_watermark_percent × capacity, minimum_free_gib)`.
- Enter emergency behavior when `free_bytes < emergency_free_mib` or a write fails with an equivalent no-space condition.

Validate configuration so the stop threshold is strictly greater than the start threshold and both are below total usable capacity.

### 5.9 Event/protect behavior

Provide a manual event action through the web UI and optional GPIO button.

Default protection window:

- Previous 2 completed clips.
- Current clip.
- Next 1 completed clip.

When an event is triggered:

- Acquire the same catalog/retention coordination used for deletion, then mark all still-existing applicable clips protected in durable state before acknowledging the event.
- Include event timestamp and source (`web`, `gpio`, or `api`).
- Show confirmation in the UI.
- Ensure retention cannot delete those clips.
- Report any requested previous clip that had already expired; do not claim it was protected.

Automatic crash detection using an accelerometer is out of scope for version 1.

### 5.10 Wi-Fi access point

The Pi must expose a local Wi-Fi access point without requiring an upstream network.

Defaults:

```text
SSID: Dashcam-<short-device-id>
Security: WPA2/WPA3 as supported by the installed stack
Address: 192.168.50.1/24
DHCP range: 192.168.50.20–192.168.50.100
```

Requirements:

- On every boot, try configured home Wi-Fi first for at most 60 seconds.
  Success means association plus a usable local route; internet access is not
  required.
- If that bounded attempt fails, activate the NetworkManager AP at
  `192.168.50.1/24` with a per-device `Dashcam-<short-device-id>` SSID and a
  unique WPA secret. Keep AP mode stable until reboot or an explicit retry;
  do not oscillate between client and AP modes.
- AP startup failure must not stop recording.
- WPA passphrase must be set during installation or generated uniquely per device.
- Do not ship a universal default password.
- The web service must bind only to the AP/local interfaces by default.
- The device must be usable with no internet connection.
- Optionally advertise `dashcam.local` with mDNS (for example, Avahi). `.local` is mDNS, not ordinary unicast DNS; direct IP access must still work.

### 5.11 Smartphone web UI

Minimum pages/features:

#### Live view

- Low-latency live preview.
- Recording state and elapsed clip time.
- GPS state, current speed, coordinates, satellite count, and fix age.
- Local/UTC time and synchronization state.
- Microphone state.
- Free storage and estimated retention time.
- Temperature, undervoltage indication when available, and service health.
- Manual event/protect button.

#### Clips

- List finalized clips newest first.
- Show time, duration, size, GPS availability, and protected state.
- Filter protected clips.
- Download video and JSON sidecar.
- Protect/unprotect with confirmation.
- Delete only through an explicit user action and never while a clip is active or downloading.

#### Settings

- IANA timezone selection; default `Asia/Jerusalem`.
- Resolution and frame rate from a validated list.
- Video bitrate.
- Clip duration.
- Overlay fields and units.
- GPS UART device and baud.
- Audio device.
- Preview resolution/frame rate.
- AP SSID and password.
- Storage thresholds.
- Read-only display of recording-volume label, filesystem, UUID suffix, mount state, and last filesystem-check result.

A separate, authenticated **Prepare SD card for removal** control must be available from the status/settings area. It is an operational action, not an ordinary editable setting.

Settings behavior:

- Validate all input on both client and server.
- Store configuration atomically.
- Label settings that require pipeline restart or full reboot.
- Apply non-disruptive settings immediately where practical.
- A settings failure must leave the previous valid configuration intact.
- Secrets are write-only: configuration reads and status responses must return only a redacted/set indicator, never the AP passphrase or session secrets.

### 5.12 Preview implementation constraints

- Preview must never take ownership of the camera.
- Preview must derive from the existing camera pipeline or a camera-configured secondary/low-resolution stream.
- Start expensive preview processing only when at least one client is connected.
- Use a leaky/bounded queue so a slow phone cannot backpressure the recording branch.
- Dropping preview frames is acceptable; dropping recording frames because of preview is not.
- Version-1 target on the local AP: median glass-to-glass latency below 500 ms and 95th percentile below 1 second, measured on a supported phone.
- Limit concurrent preview clients, default 1 and maximum 2.
- Audio in live preview is optional for version 1.

Candidate transports to compare during target-device benchmarking:

1. WebRTC, reusing the recording H.264 stream only if its profile, overlay, bitrate, and packetization are browser-compatible, or using a separately encoded low-resolution stream only if hardware/resource tests pass.
2. Low-resolution MJPEG over HTTP, accounting for its CPU and Wi-Fi bandwidth cost.
3. Low-latency fragmented MP4/MSE, accounting for browser compatibility and startup latency.

Do not use multi-second HLS segments as the only preview mode if the latency target cannot be met.
Do not select a transport from desktop convenience alone; record the Pi CPU/memory cost, recording-frame impact, Wi-Fi throughput, browser compatibility, and measured glass-to-glass latency.

---

## 6. Required architecture

### 6.1 Process boundaries

Use these logical components:

#### `dashcamd` — critical recorder daemon

Responsibilities:

- Sole camera owner.
- Media pipeline and segment lifecycle.
- USB audio capture.
- GPS UART reader and parser.
- Time anchor and UTC/local conversion.
- Overlay state.
- Metadata creation.
- Ring-file retention.
- Durable event protection.
- Health and metrics.
- Local control API over a Unix-domain socket.

Run with only the OS permissions needed for camera, video encoder, serial, audio, and recording storage.
Optional workers inside `dashcamd` must have explicit supervised failure boundaries, bounded queues, and restart policies. A GPS/audio/retention/metrics exception must not terminate the camera pipeline loop. Separate helper processes are allowed where they materially improve isolation, but they may not open the camera.

#### `dashcam-web` — noncritical UI service

Responsibilities:

- HTTP UI and JSON API.
- Preview delivery/signaling.
- Settings validation and update requests.
- Clip listing and controlled download.
- Event/protect commands.

Run unprivileged. Communicate with `dashcamd` through a Unix-domain socket. A web-service crash must not disturb recording.
The web process must not turn a user-supplied clip ID into a filesystem path. Catalog mutation, retention eligibility, and download-lease authority remain with `dashcamd`; downloads use an approved immutable path/open handle for the duration of a bounded lease.

#### OS-managed services

- Wi-Fi AP and DHCP/DNS.
- Bootstrap v1 Stage A/Stage B storage services, ordered normally after root
  is available and before storage verification/dashcam writes, never behind
  network availability.
- Timezone database.
- `systemd` supervision.
- Optional log persistence policy.

Multiple processes are acceptable, but **only `dashcamd` may open the camera**.

### 6.2 Media graph

Preferred conceptual graph:

```text
CSI camera / libcamera
        |
        +--> overlay --> hardware H.264 encoder --> parser --> split muxer --> MP4 segments
        |                                                ^
        |                                                |
        |                               USB mic --> AAC encoder
        |
        +--> bounded/leaky preview branch --> low-latency web preview
```

Implementation preference:

- First evaluate GStreamer with the installed libcamera source, a probed hardware H.264 encoder, `h264parse`, and `splitmuxsink` or an equivalent non-blocking segment muxer.
- Use a queue on every `tee` branch.
- The preview queue must be leaky and bounded.
- Use asynchronous muxer finalization where available.
- If the target OS media plugins cannot produce a stable pipeline, use the current Raspberry Pi `rpicam`/libav or Picamera2 APIs, but preserve all architecture and acceptance requirements.
- Do not choose an implementation solely because it works on a development laptop; validate it on the Pi Zero 2 W.

### 6.3 Timing model

Maintain distinct clocks:

- **Pipeline/monotonic clock:** media timestamps, clip boundaries, timeout logic.
- **UTC clock:** canonical real-world timestamps derived from GPS anchor or synchronized system time.
- **Local display clock:** UTC converted through selected IANA timezone.

Never compute elapsed media time from local wall time.

### 6.4 Clip state machine

```text
CREATING
  -> WRITING
  -> FINALIZING
  -> FINALIZED
  -> DELETING
  -> DELETED
```

Protection and download lease are orthogonal attributes, not lifecycle states. A finalized clip is retention-eligible only when it is unprotected, has no active lease, is not part of another mutation, and both pair members are reconciled.

Error states:

```text
CORRUPT
QUARANTINED
MISSING_SIDECAR
MISSING_VIDEO
```

Persist enough state to recover safely after an unclean reboot. SQLite in WAL mode or an atomic append-only journal on ext4 is acceptable. The database and exFAT directory cannot participate in one atomic transaction, so every cross-filesystem operation must use durable intent plus idempotent reconciliation. Do not rely solely on in-memory state.

---

## 7. Crash and power-loss behavior

- Design and test so that, under the defined abrupt-power test matrix on healthy reference cards, previously finalized clips remain playable and only the currently open clip is normally lost or repaired.
- Do not present that test target as a guarantee under arbitrary exFAT corruption, controller failure, unsafe card behavior, or loss of power during earlier metadata writeback.
- Prefer fragmented MP4 or another configuration that minimizes dependence on a final `moov` write.
- On boot, scan the `pending` directory and reconcile any recorded cross-filesystem operation intent:
  - Attempt safe repair/remux only with a bounded timeout.
  - Move unrecoverable files to `quarantine`.
  - Never block new recording for an extended repair operation.
- Store configuration and each individual metadata file with atomic replace semantics plus the supported flush sequence; pair-level consistency still comes from reconciliation.
- Keep system logs bounded; consider volatile journaling plus a small persistent fault log.
- ext4 rootfs and exFAT recording data have different failure characteristics. Keep the durable clip index/state on ext4, but make recovery able to rebuild/reconcile it from the exFAT directory tree.
- Before mounting a dirty exFAT volume, invoke the supported `fsck.exfat` utility with bounded behavior. Do not implement custom raw filesystem repair and never auto-format on failure.
- exFAT is not journaled. The design target is that previously finalized clips survive abrupt power removal, but this cannot be treated as a mathematical guarantee under arbitrary media/controller failure. Power-loss testing and a power-hold-up circuit remain mandatory reliability measures.
- If the recording volume cannot be mounted read-write after recovery, do not fall back to rootfs; enter `STORAGE_FAULT` and expose recovery instructions.

---

## 8. Configuration format

Use a versioned TOML file, for example `/etc/dashcam/config.toml`.

```toml
schema_version = 1
device_name = "Dashcam"

[video]
width = 1920
height = 1080
fps = 30
codec = "h264"
hardware_encoder_required = true
bitrate_bps = 8000000
keyframe_interval_frames = 30
clip_duration_s = 60
container = "mp4"

[audio]
enabled = true
device_match = "usb-VID_PID-or-stable-name"
sample_rate_hz = 48000
channels = 1
codec = "aac"
bitrate_bps = 128000

[gps]
device = "/dev/serial0"
baud = 115200
stale_after_s = 2.0
max_sample_hz = 10

[time]
timezone = "Asia/Jerusalem"
filename_timezone = "UTC"
discipline_system_clock = true
system_clock_owner = "chrony"

[overlay]
enabled = true
show_local_datetime = true
show_utc_offset = true
show_rec = true
show_speed = true
speed_unit = "kmh"
show_coordinates = true
coordinate_decimals = 5
show_altitude = true
show_satellites = true
show_hdop = false

[preview]
enabled = true
width = 640
height = 360
fps = 15
max_clients = 1
latency_target_ms = 500

[storage]
recording_root = "/srv/dashcam"
required_filesystem = "exfat"
required_volume_label = "DASHCAM"
require_distinct_mount = true
low_watermark_percent = 15
high_watermark_percent = 20
minimum_free_gib = 2.0
emergency_free_mib = 256
protect_previous_clips = 2
protect_next_clips = 1

[network]
ap_enabled = true
ssid_prefix = "Dashcam"
address = "192.168.50.1/24"

[service]
watchdog_s = 20
restart_backoff_min_s = 1
restart_backoff_max_s = 60
```

Secrets such as the AP passphrase must not be stored in a world-readable file or returned through the API.

---

## 9. Filesystem and partition layout

The ext4 root filesystem contains the application and durable control state:

```text
/etc/dashcam/
  config.toml
  secrets.env                         # root-readable only
  storage-volume.env                  # generated UUID/identity, root-readable

/var/lib/dashcam/                     # ext4 rootfs
  state/                              # database/journal, boot IDs, migrations
  thumbnails/                         # optional bounded/rebuildable cache
  provisioning/                       # idempotency marker and partition backup
  fault-log/                          # small bounded persistent fault history

/run/dashcam/
  dashcamd.sock
  preview.sock
  status.json                         # optional atomic snapshot
```

The exFAT volume labeled `DASHCAM` is mounted at `/srv/dashcam` and contains all user-removable evidence files:

```text
/srv/dashcam/                         # dedicated exFAT mount, never rootfs fallback
  README-WINDOWS.txt                  # safe Windows access instructions
  .dashcam-volume                     # generated volume identity/sentinel
  pending/                            # active/unfinalized clips
  clips/                              # normal finalized .mp4 + .json pairs
  protected/                          # protected/event .mp4 + .json pairs
  quarantine/                         # incomplete/unrecoverable artifacts
  lost+found-dashcam/                 # optional app-owned recovery staging
```

Requirements:

- `pending`, `clips`, `protected`, and `quarantine` must be on the same exFAT filesystem so moves/renames do not become cross-filesystem copies.
- The clip index on ext4 must store relative paths and stable clip IDs, not fragile device names.
- Reconcile the ext4 index and exFAT files after every unclean boot. Never delete an unindexed file automatically during reconciliation.
- Moving a protected/unprotected MP4 and JSON pair between directories is also a recoverable two-file operation, not an atomic pair rename. Record intent and reconcile interruption without exposing either orphan to retention.
- Do not depend on POSIX permissions, symlinks, hard links, sparse files, extended attributes, or case-sensitive names on the exFAT volume.
- Do not allow user-controlled filenames or path traversal. All clip IDs must map to application-owned paths below the verified mount.
- Unknown files/directories on the exFAT volume must be ignored and preserved unless an explicit user delete operation targets them through a separate maintenance workflow.

---

## 10. Local API contract

The public web API may proxy a smaller privileged Unix-socket API. Use versioned endpoints.

Minimum endpoints:

```text
GET  /api/v1/status
GET  /api/v1/config
PUT  /api/v1/config
GET  /api/v1/clips
GET  /api/v1/clips/{clip_id}
GET  /api/v1/clips/{clip_id}/video
GET  /api/v1/clips/{clip_id}/metadata
POST /api/v1/clips/{clip_id}/protect
POST /api/v1/clips/{clip_id}/unprotect
DELETE /api/v1/clips/{clip_id}
POST /api/v1/event
POST /api/v1/recorder/restart
POST /api/v1/system/prepare-sd-removal
GET  /api/v1/health
```

`/status` minimum response fields:

- Recorder state and reason.
- Current clip ID, sequence, and elapsed monotonic time.
- Effective video/audio settings.
- Measured frame rate and bitrate.
- Dropped-frame count.
- GPS/time state and age.
- Current navigation values when valid.
- Audio availability.
- Storage free bytes/percentage and retention estimate.
- Recording-volume mount state, filesystem type, read/write state, label, UUID suffix, and last filesystem-check result.
- Preview client count.
- CPU temperature and throttling/undervoltage flags when exposed by the platform.
- Software version and boot ID.

Use structured error responses with stable error codes.

---

## 11. Observability

Required metrics/counters:

- Recording uptime.
- Clips finalized, failed, repaired, quarantined, and deleted.
- Video frames captured, encoded, written, and dropped.
- Audio discontinuities and estimated A/V skew.
- GPS sentences received, checksum failures, valid fixes, time anchors, and reconnects.
- Preview clients, preview frame drops, and queue overruns.
- Filesystem free space and deletion cycles.
- Camera/encoder pipeline restarts.
- Service crashes and watchdog resets.
- CPU temperature, throttling, and undervoltage indicators when available.

Logging requirements:

- Structured logs.
- No per-frame log messages.
- Rate-limit repeated hardware errors.
- Never log AP passwords or other secrets.
- Include `boot_id`, `clip_id`, and component name where relevant.

---

## 12. Security requirements

- Unique Wi-Fi passphrase per device or user-provided passphrase during installation.
- Web UI accessible only from local/AP interfaces by default.
- No internet-facing cloud service in version 1.
- Use CSRF protection for state-changing browser requests.
- Use authenticated sessions if the AP may be shared with untrusted users.
- Require re-authentication and explicit confirmation for the prepare-removal/shutdown action.
- Implement shutdown through a narrowly scoped helper or service; never grant the web process general `sudo` or shell access.
- Sanitize all filenames and request parameters.
- Run the web process unprivileged.
- Restrict Unix-socket permissions.
- Use least-privilege groups for video, render, dialout, audio, and storage access.
- Do not expose arbitrary shell commands through the API.
- Configuration changes must be allow-listed and validated.

---

## 13. Performance and resource constraints

The implementation must be designed for Pi Zero 2 W limits.

Hard constraints:

- No software H.264 encoding at 1080p30 in the production profile.
- No full-resolution second encoder merely for phone preview unless target testing proves it sustainable.
- No unbounded frame, telemetry, HTTP, or logging queues.
- No preview backpressure on recording.
- No continuous full-frame RGB conversion just to draw a small overlay.
- No memory growth proportional to recording duration.
- No dependence on SD-card swap for steady-state recording. Declare and test the swap/zram policy, and treat OOM or sustained swap activity as a failed resource gate.

Target resource budgets while recording with no preview client:

- Stable memory use after warm-up.
- Enough free memory to tolerate clip finalization and one web client.
- CPU load must leave margin for audio, GPS, storage management, and transient mux finalization.
- Zero sustained thermal throttling in the intended enclosure and ambient conditions.

Target with one preview client:

- Recording frame rate and dropped-frame counter statistically unchanged from no-preview operation.
- No clip boundary gaps caused by preview.
- Preview may drop frames under load.

---

## 14. Acceptance tests

All tests must produce machine-readable results and preserve diagnostic logs.

### A. Basic media

1. Record 10 consecutive clips at default settings.
2. Verify every finalized file with `ffprobe` or equivalent.
3. Verify each file contains H.264 video and, when the mic is present, AAC audio.
4. Verify each clip is independently seekable and begins with a decodable keyframe.
5. Verify nominal duration is 59–61 seconds for middle clips.
6. Normalize each file's decoded timestamps onto the captured monotonic timeline, then verify no boundary gap larger than one video-frame period and no duplicated interval larger than one frame. Do not compare raw per-file PTS values as though separate MP4 files necessarily share one zero point.
7. Verify average measured bitrate is within the documented encoder tolerance of the 8 Mbit/s target.

### B. Twelve-hour endurance

Run for at least 12 continuous hours with:

- GPS connected.
- USB microphone connected.
- AP enabled.
- Periodic preview sessions.
- Storage retention cycling at least twice.

Pass criteria:

- No recorder crash.
- No missing clip sequence except explicitly reported/quarantined failures.
- No recording gap greater than one frame period at ordinary clip boundaries.
- All finalized clips probe successfully.
- A/V skew remains below 100 ms within each clip and does not grow systematically.
- Memory use does not trend upward without bound.
- No sustained thermal throttling.

### C. GPS and time

1. Boot with no GPS; verify recording starts and overlay says `TIME UNSYNCED`.
2. Connect GPS later; verify a valid UTC anchor is accepted.
3. Verify provisional clip timestamps are reconciled from monotonic time.
4. Verify local time for `Asia/Jerusalem` is produced by timezone data, not a fixed offset.
5. Unit-test dates before, during, and after both daylight-saving transitions.
6. Simulate UTC midnight and local-date rollover.
7. Remove GPS during recording; verify `GPS LOST` appears after timeout and recording continues.
8. Reconnect GPS; verify recovery without restarting the camera.
9. Feed bad-checksum and malformed NMEA; verify rejection without service failure.
10. Feed an implausible date; verify it is rejected and logged.

### D. Audio failure

1. Boot without microphone; verify video-only recording.
2. Unplug microphone while recording; verify video continues.
3. Reconnect microphone; verify audio returns at or before the next supported restart boundary.
4. Verify metadata accurately reports audio availability per clip.

### E. Wi-Fi and preview

1. Boot with AP configuration failure; verify recording continues.
2. Connect/disconnect a phone repeatedly.
3. Keep a slow preview client connected; verify its queue drops preview frames instead of blocking recording.
4. Measure preview latency and verify median below 500 ms and 95th percentile below 1 second on the declared reference phone.
5. Restart `dashcam-web`; verify no camera or recorder restart.

### F. Storage ring

1. Use a small test filesystem to trigger retention quickly.
2. Verify deletion starts at the low-water threshold and stops at/above the high-water threshold.
3. Verify oldest eligible clips are deleted first.
4. Verify active, finalizing, protected, and downloading clips are never deleted.
5. Verify video and sidecar deletion is a crash-recoverable logical operation: inject interruption after each step, reboot/reconcile, and confirm that no orphan is silently treated as an ordinary eligible clip.
6. Fill storage with protected clips; verify a clear critical fault and safe behavior.
7. Verify retention work does not create recording gaps.

### G. Event protection

1. Trigger an event halfway through a clip.
2. Verify previous 2, current, and next 1 clips become protected.
3. Verify retention skips them.
4. Verify event source and timestamp are stored.
5. Verify protect state survives reboot.

### H. Power loss

Perform repeated random power removals during recording.

Pass criteria:

- On every run in the declared test matrix, clips finalized before the active clip remain playable.
- On every run in that matrix, at most the active clip is lost or quarantined.
- Boot recovery does not block new recording for more than the configured recovery budget.
- State database/journal remains usable.

These are release test criteria on the declared reference hardware/cards, not a guarantee for arbitrary flash-controller or filesystem failure.

### I. Hardware fault/recovery

- Camera disconnect/failure where physically testable.
- Encoder initialization failure.
- SD write error or read-only remount.
- UART disconnect/noise.
- CPU thermal stress.
- Undervoltage indication.

Each fault must produce a visible state, bounded retry behavior, and no misleading `RECORDING` status when data is not being durably written.

### J. Partition provisioning and Windows interoperability

1. Flash the supported compressed Bootstrap v1 image through its checked Imager manifest to fresh nominal 32 GB, 64 GB, and any other declared card sizes.
2. Verify the build changed only the standalone stock `resize` cmdline token, preserved Imager first-run tokens, and defers when Imager first-run is active.
3. Verify ordinary post-root Stage A performs one `sfdisk --no-reread` commit and one controlled reboot, then Stage B creates FAT32 boot, fixed-size ext4 root, and remainder exFAT `DASHCAM` partitions without overlap.
4. Verify the fully installed 6 GiB root target retains at least 2 GiB free; otherwise the image must select a larger root target.
5. Verify partition creation is idempotent and a second boot does not recreate or format the recording volume.
6. Verify the exFAT filesystem is mounted at `/srv/dashcam` by UUID and the recorder confirms the expected filesystem, UUID/sentinel, and distinct mount.
7. Simulate a missing/failed mount and verify the recorder does not write into the underlying rootfs directory.
8. Record and finalize at least 10 clips, use **Prepare SD card for removal**, remove the powered-down card, and insert it into Windows 10 and Windows 11.
9. Verify the `DASHCAM` volume appears, `.mp4` files play in a standard Windows player, `.json` files open, and protected clips are easy to locate.
10. Verify documentation prevents accidental formatting of the unknown ext4 partition and clearly distinguishes the boot volume from `DASHCAM`.
11. Allow Windows to create `System Volume Information` and other metadata, reinsert the card, and verify those entries are ignored and preserved by retention.
12. Reinsert the card in the Pi and verify recording resumes without reformatting or manual repair after a clean removal.
13. Repeat with abrupt power loss; verify dirty-volume detection, bounded `fsck.exfat`, explicit failure behavior, and no rootfs fallback.
14. Verify a deliberately corrupted/unrepairable exFAT volume is never auto-formatted.

---

## 15. Implementation phases

### Phase 0A — Local repository and test foundation

This work happens in the development repository before SSH access to the Pi is available:

- Establish packaging, lint/type/test configuration, versioned configuration/schema models, state machines, and test fixtures.
- Implement hardware boundaries behind interfaces and use recorded fixtures/fakes for local tests; never claim a fake result as hardware validation.
- Author the capability probe, deployment checks, partition-layout dry run, media validator, and test-report format without executing destructive Pi operations.
- Record undecided target-dependent choices explicitly rather than baking in laptop-derived plugin names or devices.

### Phase 0B — Pi hardware and capability gate

When the owner authorizes Pi access, run the probe and produce a report that identifies:

- Camera and supported modes.
- Installed libcamera/rpicam/Picamera2/GStreamer/FFmpeg versions.
- Available H.264 hardware encoders and tested caps.
- Audio devices and stable identifiers.
- UART device and GPS baud.
- Boot-device identity, current partition table, stock auto-resize state, filesystem types, and available aligned free space.
- exFAT driver/`exfatprogs` versions, recording-volume identity, free space, and sustained write speed.
- CPU temperature/throttling state.

Probe the Pi Zero 2 W UART mapping/stability, USB topology, 32/64-bit image memory cost, camera buffer formats, overlay paths, muxer profiles, and preview candidates. Deliver and validate the Bootstrap v1 normal-post-root partition provisioner on expendable cards before relying on `/srv/dashcam`. Do not begin target-dependent integration or a large application before validating both the storage layout and a minimal 1080p30 hardware-encoded recording on the actual Pi.

### Phase 1 — Reliable video-only segmenter

- Continuous camera/encoder.
- 60-second independently playable clips.
- Atomic finalization.
- Basic systemd service.
- Frame/drop metrics.
- Two-hour endurance test.

### Phase 2 — Audio muxing

- USB audio selection.
- AAC muxed into each segment.
- A/V sync tests.
- Microphone failure behavior.

### Phase 3 — GPS, time model, and metadata

- UART NMEA parser.
- Monotonic-to-UTC anchor.
- Timezone conversion.
- Sidecar JSON.
- Provisional filename reconciliation.

### Phase 4 — Overlay

- Optimized fixed-region overlay.
- Stale-data behavior.
- 1080p30 performance gate.

### Phase 5 — Storage retention and event protection

- Durable clip index/state.
- Watermark deletion.
- Protected clips.
- Power-loss recovery.

### Phase 6 — Access point and web UI

- AP setup.
- Status/config API.
- Clip browser/download.
- Event button.
- Low-latency preview with bounded queues.

### Phase 7 — Hardening

- Twelve-hour endurance test.
- Random power-loss testing, including exFAT dirty-volume recovery.
- Fault injection.
- Finalize the Bootstrap v1 compressed image, Imager manifest, hashes, and tested normal-post-root first-boot process; initial partition-provisioning validation belongs to Phase 0B.
- Safe-removal/shutdown workflow.
- Windows 10/11 card-readability validation.
- Documentation and recovery procedures.

Do not combine all phases into an untestable first implementation.

---

## 16. Repository layout

```text
Pizero_dashcam_PROJECT.md
plan.md
AGENTS.md
README.md
LICENSE
pyproject.toml

config/
  default.toml

src/dashcam/
  __init__.py
  main.py
  config.py
  recorder/
    pipeline.py
    segmenter.py
    overlay.py
    audio.py
  gps/
    uart.py
    nmea.py
    clock.py
  storage/
    index.py
    retention.py
    recovery.py
  metadata/
    schema.py
    writer.py
  control/
    unix_api.py
  health/
    metrics.py
    platform.py

src/dashcam_web/
  app.py
  api.py
  preview.py
  auth.py
  static/
  templates/

systemd/
  dashcamd.service
  dashcam-web.service
  srv-dashcam.mount.template
  dashcam-storage-check.service
  dashcam-bootstrap-stage-a.service
  dashcam-bootstrap-stage-b.service
  dashcam-prepare-removal.service

network/
  create-ap.sh
  NetworkManager/

deploy/
  install.sh
  uninstall.sh
  image/
  storage/
    layout.toml
    firstboot-provision.sh
    verify-layout.sh
    README-WINDOWS.txt
  udev/

tests/
  unit/
  integration/
  hardware/
  fixtures/nmea/

scripts/
  capability_probe.sh
  media_validate.py
  endurance_test.py
  power_loss_recovery_check.py
  partition_layout_check.sh
  windows_interop_check.ps1

docs/
  hardware.md
  installation.md
  configuration.md
  troubleshooting.md
  architecture.md
  test-report-template.md
```

The exact language split may change, but critical media work should remain in native media frameworks rather than per-frame Python loops.

---

## 17. Coding and quality rules

- Python 3 with type hints for control-plane code, unless a measured requirement justifies C/C++ or Rust.
- Use native GStreamer/libcamera/FFmpeg components for media movement and encoding.
- Format, lint, and type-check in CI.
- Unit-test configuration, NMEA parsing, timezone conversion, retention selection, filename generation, and state transitions.
- Hardware integration tests must be runnable separately and clearly labeled.
- Every background task must have bounded queues, cancellation, and shutdown behavior.
- No silent exception swallowing.
- No shell-command construction with unsanitized values.
- No dependency on internet access during normal operation.
- Never write recordings to an unverified mountpoint or fall back from `/srv/dashcam` to rootfs.
- Treat partitioning/formatting code as destructive infrastructure: require explicit invariants, idempotency, dry-run diagnostics, and tests on expendable media.
- Pin or record dependency versions used in the release image.
- Include database/config schema migration paths from version 1 onward.
- Keep `plan.md` synchronized with verified progress. Check a task only after its stated validation/evidence exists; check a milestone only after all of its tasks and exit criteria pass.
- A local/mock test may validate logic but may never be used to check a Pi hardware, performance, power-loss, provisioning, or Windows-interoperability task.

---

## 18. Explicit non-goals for version 1

- Cloud upload or remote internet access.
- Computer-vision inference.
- License-plate recognition.
- Automatic crash detection without an added accelerometer.
- Multi-camera recording.
- 4K recording.
- Flight-control-grade analog/composite FPV video output.
- Embedded GPS telemetry track inside MP4.
- Editing or transcoding old clips on the Pi.
- Guaranteeing that a microSD card will never wear out.

A composite/FPV output can be explored later, but it requires separate latency measurement and must not be assumed suitable as a primary piloting feed.

---

## 19. Definition of done

The project is complete only when all of the following are true:

- A fresh supported Raspberry Pi OS installation can be converted into the dashcam using documented, repeatable installation steps.
- The recorder starts automatically and records without GPS, microphone, Wi-Fi client, or internet.
- Default 1080p30 H.264 is hardware encoded at an 8 Mbit/s target.
- USB microphone audio is muxed and synchronized when available.
- One-minute clips rotate without restarting the camera and without ordinary boundary gaps.
- GPS UTC/date, coordinates, speed, altitude, and fix status are parsed from UART NMEA.
- `Asia/Jerusalem` and other IANA zones correctly handle daylight saving through installed timezone data.
- Overlay and sidecar metadata agree.
- Storage retention deletes only the oldest eligible clips and never deletes active/protected clips.
- Manual event protection works and survives reboot.
- The Pi provides its own secured access point and smartphone web UI.
- Preview operation cannot backpressure the recorder.
- The twelve-hour endurance test passes.
- Random power-loss tests meet the documented exFAT recovery criteria and never trigger automatic formatting or rootfs fallback.
- A freshly flashed Bootstrap v1 card safely and idempotently provisions the FAT32/ext4/exFAT layout through its normal post-root two-boot transaction.
- After controlled shutdown, the `DASHCAM` partition is directly readable on validated Windows 10/11 systems and its MP4/JSON pairs can be copied and opened.
- Hardware/software versions and measured performance are documented.

---

## 20. Agent instructions

When implementing this project:

1. Begin with Phase 0A locally. Do not access the Pi or start implementation until the owner authorizes it. After authorization and SSH availability, complete Phase 0B and commit the measured capability report before target-dependent integration.
2. Challenge any assumption that is not confirmed on the target Pi image.
3. Build the smallest testable vertical slice for each phase.
4. Do not hide a failed requirement by changing resolution, frame rate, bitrate, overlay, or codec defaults.
5. Do not add a second camera-opening process for preview.
6. Do not block recording while finalizing clips, serving downloads, deleting old files, parsing GPS, or rendering the web UI.
7. Preserve evidence: a recoverable failure should degrade optional features before it stops or deletes recordings.
8. Include exact commands for installation, service enablement, logs, diagnostics, media validation, and recovery.
9. Report measured Pi Zero 2 W CPU, memory, temperature, dropped frames, preview latency, A/V skew, and storage throughput in the final test report.
10. Do not let Raspberry Pi OS auto-expand rootfs across the full card. At build time remove only the standalone stock `resize` token, preserve Imager first-run tokens, and verify Bootstrap v1 defers until first-run completion.
11. Never identify the destructive partition target from a hard-coded device path alone, and never format an existing recognized data volume during an upgrade.
12. Fail closed when `/srv/dashcam` is not the verified exFAT data mount; do not record into its underlying rootfs directory.
13. Treat this specification as the acceptance contract. Document any deviation explicitly with its reason, risk, and proposed remedy.
14. Update `plan.md` as work is verified: check completed tasks and milestones, leave blocked/unverified work unchecked, and attach the evidence path or command result where the plan requests it.

---

## 21. Primary technical references

- [Raspberry Pi camera software documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [Raspberry Pi Zero 2 W product specification](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/)
- [Raspberry Pi UART configuration](https://www.raspberrypi.com/documentation/configuration/computers/raspberry-pi.html#configure-uarts)
- [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
- [GStreamer `splitmuxsink`](https://gstreamer.freedesktop.org/documentation/multifile/splitmuxsink.html)
- [GStreamer `mp4mux`](https://gstreamer.freedesktop.org/documentation/isomp4/mp4mux.html)
- [Python IANA timezone support](https://docs.python.org/3/library/zoneinfo.html)
- [exFAT userspace tools (`mkfs.exfat`, `fsck.exfat`)](https://github.com/exfatprogs/exfatprogs)
