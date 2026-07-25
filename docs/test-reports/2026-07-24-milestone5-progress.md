# Milestone 5 storage-provisioning progress

## Status and authorization

- Date: 2026-07-24
- Target host: `dashcam-pi`
- Owner authorization: complete erase, reflash, repartition, and format of the
  exact card listed below
- Still required before destructive execution: shut down the Pi, move the card
  offline, re-resolve the physical target, and recheck identity evidence
- No partition-table, filesystem, or recording-volume mutation has occurred

The exactly authorized card is:

- Linux path during this boot: `/dev/mmcblk0`
- CID: `fe34325344000000200000031a0192d1`
- device serial: `0x0000031a`
- size: 31,457,280,000 bytes / 61,440,000 512-byte sectors
- source partition-table fingerprint:
  `e80659000879b7afe6b6efac1ce091e6c518c83dbfa759345a0fb6bff90af8eb`

The device path is not an authorization identity and must be resolved and
rechecked before every later destructive phase.

## Source layout and boot identity

The selected Raspberry Pi OS image uses native DOS/MBR, not GPT:

```text
MBR disk ID: 0x624667df
p1: start 16384, size 1048576 sectors, type 0x0c
p2: start 1064960, size 60375040 sectors, type 0x83
```

Observed filesystem and boot identities:

- boot FAT UUID: `89F4-4546`
- boot PARTUUID: `624667df-01`
- root ext4 UUID: `e9ef4083-101b-46b4-b87d-de84fe1169f8`
- root PARTUUID: `624667df-02`
- live kernel root argument: `root=PARTUUID=624667df-02`
- live root device: `/dev/mmcblk0p2`
- `rpi-resize.service`: disabled

The root partition already occupies the remaining card. The specification
forbids shrinking this mounted filesystem. The supported path is therefore a
customized/rebuilt image or an offline fresh-image workflow, not an in-place
split.

## Backups

A read-only source backup was captured before any implementation or mutation:

- ignored artifact directory:
  `artifacts/pi/2026-07-24/storage-preflight/dashcam-m5-source-backup-20260724-01/`
- manifest: `SHA256SUMS`
- manifest SHA-256:
  `408B2301DC9C0F3623122CC6D94C1E277A50D2C9C6715F755EFA142B03E5CC57`
- 14 manifest entries independently verified after transfer

The bundle includes the raw first sector, `sfdisk` dump, `lsblk`, `blkid`,
mount information, card identity/size, boot configuration, kernel command
line, `/etc/fstab`, root resolution, and resize-unit state.

Repository evidence:

- `2026-07-24-pi-storage-source-observation.json`
- `2026-07-24-pi-storage-source-identity.json`

## Verified base image

The exact official source archive was downloaded from Raspberry Pi's image
archive into the ignored artifact area:

- file: `2026-06-18-raspios-trixie-armhf-lite.img.xz`
- compressed size: 549,086,704 bytes
- official and locally verified SHA-256:
  `EA4E84C501D6DD4F4B1D04EB84DF133A03F90A05EE2E8AB849185C17C2B0707B`
- uncompressed size: 2,675,965,952 bytes / 5,226,496 sectors
- archive integrity: XZ CRC64 decompression completed

Read-only inspection of that image found:

```text
MBR disk ID: 0x4f2c9ea0
p1: 16384..1064959, 1048576 sectors, FAT32
p2: 1064960..5226495, 4161536 sectors, ext4
```

The base image uses the same FAT/ext4 filesystem UUIDs as the current card but
has PARTUUIDs derived from `4f2c9ea0`. Its boot command line contains
`root=PARTUUID=4f2c9ea0-02 ... resize`. The initramfs `resize_early` hook expands
partition 2 to the end of the device, and the later `set_partuuid` hook
randomizes the MBR disk ID, updates `/etc/fstab` and `cmdline.txt`, then removes
the `resize` token. This explains why the current flashed card has disk ID
`0x624667df`.

The custom image must replace that unbounded resize trigger with a bounded
early-boot provisioning trigger. Policy must preserve the actual observed/
customized source identity and its boot references; it must not hard-code either
the official archive's ID or one card's randomized ID.

The local image-builder foundation now pins the archive, raw geometry, target
layout examples, and an inert first-boot contract. It was run against the actual
downloaded archive and decompressed image: compressed size/SHA-256, raw size,
MBR ID, p1/p2 geometry/types/flags, FAT UUID, and ext4 UUID all matched. The
dry run produced a deterministic plan and did not create its proposed output
image. Its runtime remains deliberately non-executable until the bounded
transaction and loopback tests are integrated.

A gated two-stage first-boot transaction model is now integrated for initramfs
and post-root reconciliation. It binds the exact source size, root PARTUUID,
CID/size, authentic MBR fingerprint, p1/p2 identities, target geometry, backup,
signature scan, generated exFAT UUID, mount identity, sentinel, and ext4 marker.
Ext4 check/grow and p3 signature/format are separate observable phases; retries
and reboot count are bounded. Every command path is absolute and allow-listed,
and each persistent mutation requires a fresh identity recheck. The deploy
entrypoints and CLI remain disabled by an intentionally absent exact-image gate.

## Dry-run refusal

The existing read-only verifier and plan-only provisioner both correctly
refused the running card:

```text
system_disk
root_disk
unexpected_mount
root_already_too_large
```

The corrected MBR-aware contract recognizes the table type and still refuses
the live card for the four independent safety reasons above. This refusal is
evidence that the current tool will not shrink or mutate the mounted root; it is
not a provisioned-layout result.

The integrated MBR layout/planner suite passes 53 focused tests plus Ruff and
strict mypy. Its dry-run plan preserves the observed MBR identity, backs up and
validates the table before mutation, computes partition bounds from actual
capacity, formats only a verified-new partition 3, and carries the restricted
exFAT mount contract
`noatime,nosuid,nodev,noexec,uid=...,gid=...,umask=0007`.

A bounded read-only Linux observation collector is also implemented. Main-agent
review tightened shared stdout/stderr bounds, absolute executable identity,
device-node partition-number derivation, and MBR-derived PARTUUID correlation.
Its focused suite passes 9 tests plus Ruff and strict mypy. A live read-only run
through SSH on the exact Pi produced the same CID, size, MBR ID, geometry,
filesystem identities, mount state, and fingerprint as the saved observation.
It performed no target writes.

After final file-image integration and main-agent corrections, the full local
repository suite passes 752 tests with one Windows-only POSIX-shell syntax test
skipped; the same hook passed `/bin/sh -n` in WSL. Ruff formatting/lint and
strict mypy pass. This is local logic and regular-file evidence, not the
required custom-image boot or expendable-card result.

The regular-file DOS/MBR loopback gate subsequently passed with real `sfdisk`
for the exact 31,457,280,000-byte reference capacity and a 64,000,000,000-byte
case. It preserved p1 and the MBR ID, produced the exact 6 GiB p2 and aligned p3
bounds, restored a validated backup, refused a recognized exFAT signature in
the proposed p3 region, passed an idempotent target recheck, and contained all
eight injected table-transaction stops. No root privilege, loop device, mount,
or block device was used. Evidence:
`docs/test-reports/2026-07-24-storage-loopback.json`.

The exact initramfs closure is now inspected and machine-recorded in
`docs/test-reports/2026-07-24-initramfs-closure.json`. The pinned boot
`initramfs` contains a modules-only early CPIO followed at byte 3,901,440 by one
zstd main CPIO. The stock main archive has BusyBox, `parted`, `blkid`,
`blockdev`, `e2fsck`, and `partprobe`, but not Python, `resize2fs`, `dumpe2fs`,
`sfdisk`, `wipefs`, or `mkfs.exfat`. The exact ARMHF `resize2fs`, `dumpe2fs`,
and `sfdisk` binaries plus `libfdisk.so.1` were extracted read-only from the
pinned root filesystem; every other required shared object is already present
in the stock main archive.

The strict newc parser/serializer was exercised against the real early and main
archives. Two independent real zstd customization runs preserved the early
archive byte-for-byte and produced the same 13,452,681-byte initramfs,
SHA-256
`b9d288874682de0d57ba48cd8abf6b6dd23ef8bb9d41394df81c508e6964cf77`.
The injected POSIX hook is bound to the exact card/image/geometry, durable
backup/journal phases, and a single reboot cap. It remains fail-closed because
`/etc/dashcam/firstboot-runtime-v1.enabled` is deliberately absent.

The native-WSL regular-file executor then completed against the exact pinned
archive. It created and independently re-read
`artifacts/images/dashcam-release-disabled-v1.img`, size 2,675,965,952 bytes,
SHA-256
`3fd152c0309a8ed24a81f5d534dc592c22cc912445792b620a1088e9f921cc58`.
The MBR ID, p1/p2 geometry/types, FAT UUID, and ext4 UUID remained exact. FAT
readback contained one `dashcam.bounded_provision=v1` token, no stock `resize`
token, the expected customized initramfs hash, and no enable gate. The executor
used only new/temporary regular files, read-only `debugfs`, and mtools; no
device, loop, mount, sudo, root-filesystem write, or Pi access occurred.
Evidence: `docs/test-reports/2026-07-24-file-image-build.json`. Block-device
flashing remains a separate unconditional refusal.

## Production correction required

The pre-Pi `layout-v1.toml` deliberately used GPT as a fixture candidate. It has
now been corrected to the exact selected image's native MBR boot contract.
Production retains MBR; no GPT conversion is planned.

Before destructive testing, the local contract must preserve and validate the
MBR disk ID, partition numbers/starts/types/boot flag, PARTUUID references,
filesystem UUIDs, source-image identity, and exact target geometry. A guarded
offline/custom-image workflow must disable whole-card expansion before first
normal boot, back up and recheck identity before mutation, create only the
verified-new partition 3, and remain idempotent.

For this card and the declared 6 GiB root target, the evidence-specific computed
recording partition is:

- start sector: 13,647,872
- end sector: 61,437,951
- size: 24,468,520,960 bytes / approximately 24.469 GB decimal / 22.788 GiB

No sector values may be blindly reused for another card.

## Exact-card destructive authorization

On 2026-07-24 the owner explicitly stated:

```text
CID fe34325344000000200000031a0192d1 is expendable and may be completely erased, reflashed, repartitioned, and formatted.
```

This authorizes the 31,457,280,000-byte exact-card trial and its CID-bound
runtime gate. It does not authorize live-root mutation: the Pi must be shut
down and the card moved offline before the first physical write. The target
must be re-resolved and all available identity evidence rechecked immediately
before each destructive phase. Later 64 GB and destructive fault-test targets
still require their own explicit identification and authorization.

## Authorized regular-file image

The CID-bound enabled image was built and independently re-read:

- path:
  `artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v1.img`
- size: 2,675,965,952 bytes
- SHA-256:
  `7225b9285e48cecab9fd9a7765cb35e6cb4d4769a59b0606b1784abc3b870904`
- FAT cmdline: one `dashcam.bounded_provision=v1`, no stock `resize`
- initramfs gate/hook/contracts: present with exact modes and hashes
- root payload: exact gate, authorization marker, contracts, post-root runtime,
  service, and `local-fs.target.wants` symlink
- full local validation: 775 passed, 1 Windows POSIX-shell skip; Ruff and strict
  mypy passed

Two unsafe intermediates were rejected before hardware use: one exposed the
cross-agent service-target mismatch and was deleted, and the executor removed a
partial output when the required `local-fs.target.wants` directory was absent.
Detailed evidence:
`docs/test-reports/2026-07-24-authorized-exact-card-image.json`.

Immediately before the physical-work handoff, the running Pi again reported
CID `fe34325344000000200000031a0192d1`, 61,440,000 sectors, root
`/dev/mmcblk0p2`, and Pi serial `00000000db28ffe4`. A clean shutdown was issued
and loss of SSH was confirmed. The card has not yet been reported in the
Windows reader.

After insertion, Windows resolved the card as Disk 1: USB removable,
non-system/non-boot, 31,457,280,000 bytes with 512-byte sectors. Its MBR ID
`0x624667df` and exact p1/p2 starts and lengths matched the final live-Pi
observation. Windows `Get-Disk` is the capacity authority here; the legacy
`Win32_DiskDrive` geometry projection reported a smaller synthetic value and
was not used for target identity.

Raspberry Pi Imager v2.0.10 then displayed the expected pre-write summary:
Raspberry Pi Zero 2 W, the CID-bound authorized image, and the Generic
MassStorageClass USB target. The initially selected local image exposed no
Customisation step because Imager 2.x intentionally has no initialization
metadata for an image selected through **Use custom**. No write was started.

`deploy/image/dashcam-authorized-local.rpi-imager-manifest` now supplies the
official `cloudinit-rpi` initialization contract inherited from the exact
2026-06-18 Raspberry Pi OS Lite base image while binding the local authorized
image by its 2,675,965,952-byte size and SHA-256
`7225b9285e48cecab9fd9a7765cb35e6cb4d4769a59b0606b1784abc3b870904`.
The manifest's file URI resolves to the verified local image and its only
hardware profile is the Pi Zero 2 W (plus the standard no-filtering profile).
Imager v2.0.10 loaded the manifest and displayed its name in the window title.
The fresh final summary again showed the Pi Zero 2 W, the authorized exact-card
v1 image, and the Generic MassStorageClass target. It now also listed hostname,
localisation, user account, Wi-Fi, and SSH as configured. No write was started.
The next gate is the destructive Write action.

## First exact-card boot result

The owner completed the Imager write, inserted the card into the replacement
Pi Zero 2 W, and powered it on. Public-key SSH succeeded at
`192.168.68.110`. The live Pi serial `00000000db28ffe4`, Wi-Fi MAC
`2c:cf:67:98:4c:49`, card CID
`fe34325344000000200000031a0192d1`, 61,440,000-sector capacity, source MBR
ID/geometry, and p1/p2 filesystem UUIDs matched the authorized contracts.

The transaction failed safe before any partition or filesystem mutation. The
system was degraded only because `dashcam-firstboot-storage.service` exited
125 with:

```text
dashcam post-root refused: required regular file is absent or unsafe: /boot/firmware/dashcam-provision/initramfs-v1.state
```

The trigger remains present, p2 is still the exact 4,161,536-sector source
partition, p3 does not exist, and the durable early-initramfs state directory
was never created. Read-only boot inspection established the cause: the
32-bit Zero 2 W booted `kernel7.img`, and `auto_initramfs=1` selected the stock
`initramfs7` (SHA-256
`3f5288ed963028accc5103f13f5a03a9d6d26ef3f58ad38a31736d3802d452b1`).
The v1 builder had instead customized the unused generic `initramfs` (SHA-256
`ee7f95c2d70071ce5223ef78fc9df599c2152afd241840ea769bccf369848153`).

Do not retry the failed service or force the generic initramfs. Milestone 5
remains unchecked pending a corrected, independently verified armv7
`initramfs7` artifact and repeat exact-card boot.

## Corrected armv7 v2 image

The corrected builder now closes the exact Pi Zero 2 W armv7l target:
`kernel7.img`, `initramfs7`, and exactly one globally applicable
`auto_initramfs=1`. It refuses conditional-only assignments, duplicates,
explicit kernel/initramfs overrides, includes, `arm_64bit`, `os_prefix`, and
target/closure drift. The schema-v2 plan binds the 7,791-byte closure manifest
at SHA-256
`8a4a109aed561d559e4f2a0703370778e105449be144f724ca49c65447c9bcfe`.

The selected `initramfs7` ELF closure covers four roots and 19 exact stock
symlink/versioned entries. An independent derivation found no unresolved
SONAMEs and matched every manifest mode, link target, and regular-file hash.
Two real customizer runs were byte-identical.

The new authorized regular image is:

- path:
  `artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v2.img`
- size: 2,675,965,952 bytes
- SHA-256:
  `ff053cc595c8b7323a9779b28c338ed9c57de13ca5d384ecff69d797159cc802`
- selected `initramfs7`: 13,450,240 bytes, SHA-256
  `c1e3cba130781f22fa45e0d01b33f22fc37088ec1f767288bb77dddbcb95da5e`
- generic `initramfs`: unchanged from stock, SHA-256
  `3505ce515a40f3c3ae39b28d1ff16cbd17db46710ee3732c8a1631f83673b70c`
- full local validation: 808 passed, one Windows POSIX-shell skip; Ruff and
  strict mypy passed
- independent FAT/ext4 readback: passed, including target config/cmdline,
  clean root UUID, exact gate/marker/contracts/runtime/service hashes and
  modes, and the `local-fs.target.wants` symlink

Detailed evidence:
`docs/test-reports/2026-07-24-authorized-exact-card-image-v2.json`.
The updated local Imager manifest now names and hashes v2. The next gate is a
clean Pi shutdown, offline card re-resolution, v2 flash, and repeat first- and
second-boot evidence collection.

Immediately before the v2 reflash handoff, the live Pi again matched serial
`00000000db28ffe4`, card CID
`fe34325344000000200000031a0192d1`, 61,440,000 sectors, and
`/dev/mmcblk0p2` root. Only the source 512 MiB FAT32 p1 and 2 GiB ext4 p2
existed; p3 remained absent. A clean shutdown was issued and loss of SSH was
confirmed.

After the owner reported moving that card to the Windows reader, it resolved
as Disk 1, `Generic MassStorageClass`, USB serial `000000002960`: online,
healthy, writable, non-system/non-boot, 31,457,280,000 bytes, 512-byte logical
sectors, and 61,440,000 sectors total. Its MBR ID was `0x4f2c9ea0`; p1 was
type `0x0c` at sector 16,384 for 1,048,576 sectors and p2 was type `0x83` at
sector 1,064,960 for 4,161,536 sectors. This exactly matches the final
live-Pi capacity and unmutated v1 source layout. Disk 2 is the reader's empty
second LUN and is not a target.

The next gate is to load the updated v2 manifest, reapply the erased
cloud-init customisation, inspect the final Imager summary, and obtain
action-time confirmation before the destructive write.

The final Imager v2.0.10 summary was inspected. It shows Raspberry Pi Zero 2 W,
`DashCam Pi Zero 2 W - authorized exact-card v2`, and
`Generic MassStorageClass USB Device`. It lists hostname, localisation, user
account, Wi-Fi, and SSH as configured. No write has been started. The sole
remaining pre-flash gate is the owner's action-time confirmation to erase the
resolved 31,457,280,000-byte Disk 1 and write the verified v2 image.

## Corrected v2 first-boot trial

The owner reported that Imager completed writing and verification, then moved
the card to the reference Pi and powered it on on 2026-07-25. From 00:06:00
through 00:12:18 Asia/Jerusalem, `dashcam-pi.local` did not resolve, SSH at the
old `192.168.68.110` address remained closed, and the expected Wi-Fi MAC
`2c:cf:67:98:4c:49` was not present in the Windows neighbor table. A different
Raspberry Pi at `192.168.68.113` was explicitly rejected because its
`2c:cf:67:16:28:70` MAC does not match the authorized reference Pi.

This is recorded as not yet network-reachable, not as a provisioning failure.
The initramfs transaction permits one automatic reboot; its bounded partition,
filesystem, durability, and reboot commands do not establish a global
SSH-availability deadline. No manual service retry, reboot, or power
interruption has been attempted. The next safe gate is either identity-checked
SSH evidence or an owner-observed boot/LED symptom that supports a deliberate
recovery decision.

The owner then reported that the first v2 flash omitted the SSH public key and
reflashed the same v2 image with the key configured. This reset the transaction
and observation window. From 00:19:46 through 00:25:48, the expected Wi-Fi MAC
still did not appear and SSH at the old address remained closed; an active
subnet SSH scan also found no host with the expected MAC. Authentication was
therefore never attempted. The missing key explains why a reachable SSH server
could reject key authentication, but it does not explain the absence of the
Pi's Wi-Fi association.

The owner reports that the Zero 2 W's single green LED is fixed solid. No
repeating firmware warning pattern or visible storage-activity dropout was
reported. On this model the LED combines power and active-low storage activity,
so this is evidence of power with no currently visible SD access, not by itself
a specific boot-error code.

Observation continued through 00:36:09, more than sixteen minutes after the
second attempt's first check, without the expected MAC or SSH. This exceeds the
bounded early transaction plus the unit's five-minute post-root window enough
to require preserved-state diagnosis rather than further blind waiting. The
exact v2 image has no active Zero 2 W `dwc2` overlay, gadget module load, USB
network profile, or USB serial console. Connecting its OTG port to the laptop
would therefore not expose a management shell on the current boot.

## v2 offline read-only failure capture

After the Pi was powered down, the card was inserted in the Windows reader and
examined without card writes. It resolved as Disk 1, `Generic MassStorageClass`
USB serial `000000002960`, with the authorized CID
`fe34325344000000200000031a0192d1`, 31,457,280,000 bytes / 61,440,000 sectors,
and MBR disk ID `0x4f2c9ea0`. The partition table had reached the target
geometry: FAT32 p1 begins at sector 16,384 for 1,048,576 sectors; ext4 p2 begins
at sector 1,064,960 for 12,582,912 sectors; and unformatted type-`0x07` p3
begins at sector 13,647,872 for 47,790,080 sectors.

The durable FAT journal is `phase=table_committed`, `reboot_count=0`, and names
the exact CID and valid backup-manifest hash
`f1e6174b69aa61172162ce403434734780596a63df04bc22d74ff51d161a77cf`.
The ext4 filesystem is clean and remains at its original 520,192 4 KiB blocks
(2,130,706,432 bytes), UUID `e9ef4083-101b-46b4-b87d-de84fe1169f8`; it was not
grown to p2. A read-only copy of precisely that filesystem extent is retained at
`artifacts/pi/2026-07-25-v2-firstboot-failure/root-source-extent-readonly.img`
(SHA-256 `ec82536b1906f25817ef84e8f6a7b3d8665852018b62f9a06233a11a8ac556b6`).
It exactly matches the v2 image's root extent, proving no root filesystem write
occurred.

The exact v2 initramfs `e2fsck` is the closed ARMHF e2fsprogs 1.47.2 binary
(SHA-256 `b321caa91bb834fd90498cffa862c303387b023d81e87fea2d2dc11460029633`),
with a complete verified ELF closure. A host e2fsck 1.46.5 rejection of
`FEATURE_C12` is an old-tool incompatibility with ext4 `orphan_file`, not card
corruption. Docker Desktop e2fsck 1.47.0 completed a read-only `-fn` check with
exit code 0.

The bounded conclusion is that v2 committed the new partition table and then
stopped before successful filesystem growth. The exact failing command is not
recoverable because v2 did not persist refusal/failure diagnostics. Its forced
120-second e2fsck stage is the leading hypothesis, not a proven root cause. The
card remains offline and read-only pending review of a v3 recovery/diagnostic
design; do not retry v2 or mutate the card. Full structured evidence is in
`docs/test-reports/2026-07-25-authorized-exact-card-image-v2-failure.json`.

## Verified v3 recovery/diagnostic image

V3 is an offline, independently re-read regular-file repair of the v2
diagnosability and failure-boundary gap. It does not claim that the v2 e2fsck
timeout was the cause: the v2 card proved target-table commitment and no root
filesystem write, but did not retain the precise failing command.

The updated hook (SHA-256
`289a03bdfa364d04679f13a913162b4c51ba17c13eae494f8b9b4589ff7d7b9f`) writes a
separate atomic FAT progress record of at most 512 bytes at
`dashcam-provision/initramfs-v1.progress` before e2fsck and resize, and upon
completion or refusal. The record has the bounded stages `before_e2fsck`,
`before_resize2fs`, `complete`, and `refusal`. E2fsck now uses
`timeout -k 10 120 ... e2fsck -p` and accepts only status 0 or 1; the exact
BusyBox v1.37.0 supports that `-k` form. Resize has explicit status handling.
The post-grow check compares exact values: 1,572,864 blocks of 4,096 bytes and
6,442,450,944 device bytes, rather than relying on shell multiplication.

The independently verified v3 output is:

- path:
  `artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v3.img`
- size: 2,675,965,952 bytes
- SHA-256:
  `05f08ca82c7578a007c30653daf461832e882fe15db9d6d9ca79b2c6279ad28a`
- build: exit 0 in 597.3 seconds
- selected `initramfs7`: 13,449,304 bytes, SHA-256
  `c3ed7c04f05eadbb2296fd1fc743d2cdea77b33aed72512989be6079161a319d`
- target closure manifest: 7,791 bytes, SHA-256
  `3b392ebf879528df5ad2f46c87b96bdf11a60238f968788fed0f739b7ddffdbe`

Independent CPIO readback matched the hook hash; cmdline still has the bounded
trigger and exactly one active `auto_initramfs=1`. Full validation reports 809
passed and one Windows-native-POSIX-shell skip; Ruff, mypy for 69 files, and a
separate WSL `sh -n` check passed. The Imager manifest has also been updated
and its v3 size/hash match this output.

V1 and V2 are retired and must not be flashed. The authorized physical card is
currently offline in Windows Disk 1 and must remain untouched until the main
agent initiates a separate identity-checked v3 Imager workflow. Milestone 5
remains unchecked. Full evidence is in
`docs/test-reports/2026-07-25-authorized-exact-card-image-v3.json`.

## v3 offline read-only failure capture

The owner completed the identity-checked v3 flash and powered the reference Pi.
A bounded read-only network observation from 11:45:09 through 11:55:39 +03
found no association for the expected Wi-Fi MAC `2c:cf:67:98:4c:49`;
`dashcam-pi.local` did not resolve, and the old `192.168.68.110` address had no
TCP port 22 listener. `192.168.68.113` was explicitly rejected: its MAC was
`2c:cf:67:16:28:70`, not the authorized Pi's MAC. The owner then powered off
the Pi and inserted the card in the Windows reader.

Read-only inspection resolved the authorized card as Windows Disk 1,
`Generic MassStorageClass`, USB serial `000000002960`, 31,457,280,000 bytes /
61,440,000 sectors. Its MBR has the target layout: FAT32 p1 at sector 16,384
for 1,048,576 sectors, ext4 p2 at sector 1,064,960 for 12,582,912 sectors, and
unformatted type-`0x07` p3 at sector 13,647,872 for 47,790,080 sectors.

The FAT journal records `phase=table_committed`, `reboot_count=0`, the exact
authorized CID and sector count, and valid backup-manifest SHA-256
`f1e6174b69aa61172162ce403434734780596a63df04bc22d74ff51d161a77cf`. V3's new
FAT progress record is exact: `phase=refusal`,
`status=refused_identity_changed_after_the_target_table_commit`, with the
authorized CID and sector count. A raw read-only ext4 inspection found magic
`ef53`, a clean filesystem, 4,096-byte blocks, and 520,192 blocks
(2,130,706,432 bytes): it was not grown and no root filesystem write occurred.

This establishes that V3's diagnostics succeeded: it refused at the
post-table-reread aggregate identity check before e2fsck. The prior forced
e2fsck-timeout hypothesis is therefore disproven as the cause of this V3
stoppage. V3 does not identify which aggregate field caused the refusal; a
transient `/dev`, sysfs, or `blkid` identity race is the leading hypothesis,
not a proven field-level cause. The card remains read-only after insertion.
V3 is retired pending a reviewed V4 field-level diagnostic/fix; do not retry
or mutate it. Milestone 5 remains unchecked. Full structured evidence is in
`docs/test-reports/2026-07-25-authorized-exact-card-image-v3-failure.json`.

## Verified V4 identity-settle image (offline only)

V4 repairs the V3 post-table identity-reread failure boundary without treating
the unproven race hypothesis as fact. Its only retry loop is the immediate
post-`blockdev --rereadpt` identity settle: it allows at most ten one-second
attempts. It captures the `rereadpt` result, uses `partprobe` only as a fallback
when reread fails, and preserves the granular failing identity field, attempt,
and command statuses in the durable refusal diagnostic. The identity check
immediately before ext4 work remains strict and one-shot; no partition write,
e2fsck, or resize retry was introduced.

The independently re-read regular-file artifact is:

- path:
  `artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v4.img`
- size: 2,675,965,952 bytes
- SHA-256:
  `6c243a01405210727aae5a63c1d28af9d386b746f26839ea917e15fe1308dd25`
- closure manifest: 7,791 bytes, SHA-256
  `ab74df854adabaa4611ea1683f06b73c42cbef51ce7a0c0ceda6044394d30799`
- selected `initramfs7`: 13,482,464 bytes, SHA-256
  `0740131c8be609b57ed4da0732f76483127a6941a9a7d27717c9173f1943eb30`
- embedded `resize_early` hook SHA-256:
  `0eb9d53e6c0c6b5e16f7214803f7a61d5cdc50f6f0cc3aa34562055c3b59eeee`

Independent FAT and CPIO readback confirmed the expected root PARTUUID and
bounded trigger in `cmdline.txt`, `camera_auto_detect=1`, exactly one active
`auto_initramfs=1`, the `dwc2` host overlay, and the selected embedded hook.
Focused identity tests report 13 passed and one host-shell skip; the WSL POSIX
behavioral checks prove a transient failure succeeds at attempt 3 and a
persistent failure stops at attempt 10 with its field and attempt retained.
Focused provisioning/image tests report 107 passed and one skip; the full suite
reports 812 passed and one skip; mypy passed 69 files and Ruff passed.

V4 has not been flashed to the physical card or booted on a Pi. Consequently,
it is not hardware evidence and does not alter the uncompleted Milestone 5.
The next authorized gate is to re-resolve the exact expendable card offline,
inspect the V4 Imager summary and customisation, obtain action-time confirmation
for the destructive write, and then collect bounded first-boot evidence. Full
structured local evidence is in
`docs/test-reports/2026-07-25-authorized-exact-card-image-v4.json`.
