# Milestone 9 overlay candidate failure and native-NV12 capability result

Date: 2026-08-03

Target: exact Raspberry Pi Zero 2 W, serial `00000000db28ffe4`

Boot ID: `91dc363d-3e76-47d3-9c10-56f897c99e9d`

Recording volume: `/dev/mmcblk0p3`, exFAT `DASHCAM`, UUID `7EED-3EA7`

## Outcome

Milestone 9 remains open. The first installed production candidate,
GStreamer `textoverlay`, negotiated the required 1920x1080 NV12 and hardware
H.264 High/4.1 graph but delivered only about 10.4 frames/s. It therefore
failed the mandatory 1080p30 gate and was stopped cleanly. A stock
`gdkpixbufoverlay` fixed-region candidate improved delivery to about 18.3
frames/s but also failed.

A subsequent isolated Python `GstBaseTransform` capability probe wrote a
pre-rendered opaque 1152x64 luma strip into native NV12 buffers before
`v4l2h264enc` and delivered 30.006 frames/s, matching a same-session
no-overlay baseline. This is evidence for the next implementation direction,
not Milestone 9 acceptance: the probe used synthetic fixed content, no audio
or GPS integration, no live text changes, and no clip rollover.

The failed production release remains installed at
`/opt/dashcam/releases/0.1.0.dev0-e727ddccd94659ff`, but
`dashcamd.service` is deliberately disabled and inactive so an unexpected
reboot cannot start the rejected recorder. Storage verification and network
fallback remain enabled. Do not re-enable the recorder until a corrected
hash-closed release passes its bounded exact-Pi gate.

## Deployment transaction

Source commit `64d7a693b3e1ea64c4989812ae1b153f8cad5eeb` produced release
`0.1.0.dev0-e727ddccd94659ff`.

| Artifact | SHA-256 |
|---|---|
| Bundle manifest | `b6367517fa8cd1715b45acbcb89a6b0e3fc3d7c9f12e467c0e0e8f9f93501147` |
| `SHA256SUMS` | `ef541edf18a0d8d1477025011064df570d5cce5ed90912c2f7999fcaf678fb10` |
| Application wheel | `3ba1f83a3dc6ee774bf8ec30d0460269f4b4c1fb4517b74164c7517c4695776a` |
| Pinned tzdata wheel | `dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931` |
| Authoritative plan | `caa2e93b63f2ec031484774bd86950e261c51fa28c6a2bfa9c43471bc338b337` |
| Apply | `7496f3739bf5f47d8a8bc394fda8a97578824edbd2678c36e4a08e1703397cde` |
| Idempotent plan | `25eb34498c86454b82b787d462b93408e13d2ef90834c52368359d1f90a85ffe` |
| Idempotent apply | `f4e2d5636c46cfbea7c4c3808eb26989999bd6b52c6a2e145a2109d499748dfb` |

The plan installed only
`gstreamer1.0-x=1.26.2-1+rpt3+deb13u1`, started no service, and preserved
the managed configuration and unit hashes. The second plan/apply required
zero package changes. The idempotent apply left root free space byte-for-byte
unchanged at 2,757,083,136 bytes; its preceding plan observed
2,757,087,232 bytes, and the 4 KiB evidence-file write between those
observations is not claimed as installer growth.

The first safe plan refusals were preserved:

- A bundle allowlist refusal after evidence was accidentally redirected into
  the closed bundle: `49f528dcc2fbd1e40155d9bd71fcb1d8462f06928bfae9f288fd0e7ea68bac2a`.
- A root-headroom refusal before owner-authorized cleanup:
  `68fd327ff25f22656ad15cd5658231e3130bb76298c17e038661a9243d6632b1`.

The owner authorized archiving obsolete root data to the protected exFAT
area, deleting only the verified sources, resetting the expendable recording
catalog/media, and reducing the ext4 reserved-block percentage to 1%.
The old catalog is recoverable at
`/var/lib/dashcam/catalog-backups/pre-m9-purge-20260803-v1/catalog.sqlite3`
(3,293,184 bytes, SHA-256
`2a77ea429002f0f701db81576a14babfac488e60f7921a045dd4ec3b260fe887`).
The protected exFAT archives are:

- obsolete home data:
  `cb264a9b608b08142f41678ef0fcdf740dd5973bf236d756d9257fd06651144a`;
- two noncurrent releases:
  `b7b4a2cd36e353bbe6fe5c2257e9301dc09a323cbe84a8132f956fb4f218bbf2`.

Ext4 reserved blocks changed from 67,951 to exactly 15,728. The authoritative
plan then passed with only 3,144,744 bytes above its required pre-apply
headroom; the safety threshold was not weakened.

## Rejected production `textoverlay`

The production daemon ran from 19:15:13 to 19:16:23 local time. It reported:

- 1920x1080, 30/1 negotiated NV12;
- `v4l2h264enc` at `/dev/video11`, hardware encoded, High/4.1;
- storage `READY`, audio `MATCHED`, overlay `ACTIVE`;
- zero pipeline and systemd restarts;
- `throttled=0x0`;
- a clean service stop with status 0.

Before stop, the bounded status snapshot already showed 419 encoded frames
and 775 input-PTS-gap drops. The finalized media proved the nominal 30/1 caps
did not represent delivered frame rate:

| Sequence | Frames | Duration | Delivered fps | Video bitrate | MP4 SHA-256 |
|---|---:|---:|---:|---:|---|
| 0 | 625 | 57.556 s | 10.425 | 2.779 Mbit/s | `551f7b0f8fa28cdc3d90b5b8dd80ab7d8a0bbcd7e4090ac7134661aeafaa30fc` |
| 1 | 38 | 6.066 s | 10.552 | 2.869 Mbit/s | `3798f28d6eaa7b494666cc2f6f084ab06abbdc55a57d772449f192d9f8696c5b` |

Systemd recorded 129.816 CPU-seconds for the roughly 70-second service
lifecycle. Both MP4/JSON pairs finalized and `pending` was empty, so this is a
performance refusal rather than a finalization failure.

## Rejected stock fixed-region compositor

An isolated, service-inactive `gdkpixbufoverlay` probe used a pre-rendered
1100x100 RGBA image directly on the NV12 graph before the same hardware
encoder. The exact ignored probe source is retained locally as
`artifacts/pi-m9-gdkpixbuf-probe-v1.sh`, SHA-256
`05f47e8af9dcc15c4539017bfa44c83ef03343ce670cebc37df43934bdacbbb6`.
The later checked 1536x64 candidate harness is a separate reproducer and is
not represented as the source of this result. The executed probe produced a
cleanly decodable 1920x1080 High/4.1 file but only:

- 622 frames over 34.060333 seconds: 18.262 frames/s;
- 36.903 user plus 1.585 system CPU-seconds over 35.375 seconds;
- zero throttle flags.

The media SHA-256 is
`49d30ed084ae849f211325ef9c239fe2b822d6a91226f9782c0e6a16be99dc52`.
This candidate also fails and must not be promoted.

## Native-NV12 capability comparison

A direct pad-probe attempt was correctly rejected as an implementation path:
the camera buffers were not writable and every `gst_buffer_fill` returned
zero. Its apparent 30 fps contained no overlay pixels and is not performance
evidence.

The next probe registered an in-process Python `GstBaseTransform`. GStreamer
provided writable buffers to its in-place transform, which copied only a
pre-rendered opaque luma row set into a fixed region. The transform:

- saw and wrote all 1,047 frames;
- wrote 77,193,216 bytes with zero short writes or failures;
- bounded the slowest transform call at 14,592,020 ns;
- received EOS and produced a fully decodable hardware-H.264 High/4.1 file;
- stayed at `throttled=0x0`.

| Arm | Frames | Media duration | Delivered fps | CPU seconds / wall |
|---|---:|---:|---:|---:|
| No overlay | 1,033 | 34.427000 s | 30.0055 | 21.905 / 35.335 |
| Native NV12 transform | 1,047 | 34.893667 s | 30.0057 | 33.250 / 35.389 |

The transform added 11.345 CPU-seconds, about 32.1 percentage points of one
core during this short run, while preserving delivered frame rate. Its MP4
decoded with empty stderr; the on-Pi decoded overlay crop SHA-256 was
`3f34ecbcf37248aa439f1d6401fd59a3828bca3f61f4086a9b1e150890780526`.
No coordinate-bearing frame was copied to Windows.

## Required next gate

Implement the native-NV12 transform inside the production recorder with a
bounded deterministic bitmap font and cached two-line payload. Then build and
install a new hash-closed release and repeat, in order:

1. initial `TIME UNSYNCED` rendering before PLAYING;
2. live 2 Hz shared-snapshot changes, stale/lost hiding, and GPS recovery;
3. audio/GPS integrated production recording across multiple 60-second
   boundaries;
4. decoded first-frame/crop proof, sidecar snapshot consistency, hardware
   decode, IDR starts, zero drop/restart increase, and A/V skew;
5. matched resource sampling long enough to evaluate CPU/RSS/temperature and
   transient finalization margin, including enabled/disabled allocation-copy
   behavior and bounded first-buffer video-meta/memory-layout evidence.

If the integrated transform does not retain 1080p30, stop again. Do not lower
resolution, frame rate, bitrate, or codec to manufacture a pass.
