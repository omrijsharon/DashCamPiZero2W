# Partition provisioning design

The generic device provisioner remains intentionally **plan-only**. It never
opens or mutates a block device. A separate executor can customize only a new
regular image file. Its authorized mode is closed to the reviewed
31,457,280,000-byte card and requires the exact CID/size/statement record before
it injects runtime gates. It still refuses every block-device target.

## Inputs

The exact selected Raspberry Pi OS image uses DOS/MBR. The production contract
retains that native table and preserves its observed disk ID, from which Linux
derives the partition PARTUUIDs. The policy includes:

- image-provided partition 1 preserved as FAT32;
- partition 2 retaining its original start and growing to a configurable 6 GiB
  ext4 target;
- partition 3 using the aligned remainder as exFAT labeled `DASHCAM`;
- a nominal-32-GB minimum capacity check, an 8 GiB minimum recording area, and
  1 MiB reserved trailing alignment space;
- an ext4 completion-marker path and exFAT volume-sentinel name.

The JSON observation format is represented by fixtures in
`tests/fixtures/provisioning/`. It binds the canonical resolved path, hardware
serial/CID, exact byte size, MBR disk ID, sector geometry, partition
types/flags/PARTUUIDs, filesystem identities, mount state, and a deterministic
fingerprint of that evidence. The verifier never treats a mutable `/dev` path as
sufficient identity.

## Read-only verification

From the repository root:

```text
uv run python scripts/verify_layout.py \
  --observation tests/fixtures/provisioning/source-ready.json
```

The verifier recognizes only:

1. a supported image source containing the expected boot and root partitions
   with no partition 3; or
2. an exactly matching completed layout whose ext4 marker and exFAT sentinel bind
   the layout version, device serial, and exFAT UUID.

It refuses unresolved paths, live system/root disks, undersized media, unexpected
or overlapping partitions, conflicting data signatures, unexpected mounts,
wrong filesystems, a root partition that would require shrinking, insufficient
aligned space, and inconsistent idempotency identities.

On Linux, create the observation with the read-only collector:

```text
uv run python scripts/collect_storage_observation.py \
  /dev/<verified-device> \
  --output /absolute/new/device-observation.json
```

The collector resolves the whole disk, correlates `lsblk`, `sfdisk`, `blkid`,
`findmnt`, MMC CID, and `wipefs` evidence, then validates the result against the
closed observation schema. Its subprocesses are allow-listed, shell-free,
time-bounded, and share a bounded output budget. It never mounts, writes,
partitions, formats, or opens a block device itself. Run it from an already
trusted installation with sufficient read permission for the target block
device; on the exact Raspberry Pi OS image, the `/usr/sbin` probes require root.
The collector never invokes `sudo` or prompts for credentials itself.

## Dry-run plan authoring

```text
uv run python scripts/plan_provision.py \
  --observation tests/fixtures/provisioning/source-ready.json \
  --expected-identity tests/fixtures/provisioning/source-identity.json
```

The output is deterministic JSON. Command `argv` arrays and bounded `sfdisk`
input are review artifacts, never shell strings. They show the intended
ordering: save and validate an identity-bound DOS/MBR dump before the first
mutation; rewrite the complete table while preserving its disk ID and p1/p2
identity; grow partition 2 without moving its start; create partition 3; check
and grow ext4; format only the verified-new partition 3; capture its UUID;
configure the restricted UUID mount; create directories and the volume
sentinel; then durably write the ext4 completion marker.

Some argv entries reference `dashcam-provision-internal`,
`${CAPTURED_DASHCAM_UUID}`, `${DASHCAM_UID}`, and
`${DASHCAM_STORAGE_GID}`. These are explicit interface placeholders for a later
reviewed executor, not executable functionality in this repository version.
Before a mount unit can be installed, the executor must resolve and validate
those values, use
`noatime,nosuid,nodev,noexec,uid=...,gid=...,umask=0007`, and verify the unit.
Unresolved placeholders are hard refusals.

For DOS/MBR, the executor must preserve the observed disk ID and partition
numbers, starts, types, and boot flags. It must also verify that the derived
PARTUUIDs remain consistent with boot `cmdline.txt` and `/etc/fstab`, and that
the FAT/ext4 filesystem UUIDs are unchanged.

For the generic device planner, passing `--non-dry-run` only exercises the
refusal gate. Without the complete
identity-bound phrase it returns `confirmation_required`; even with the exact
phrase it returns `execution_disabled`. No executor exists behind either path.

The distinct `scripts/build_release_image.py
--execute-authorized-exact-card-image` path writes only a new regular `.img`.
It installs the exact initramfs/post-root transaction and gates after validating
the closed authorization file. Its verified output and readback are recorded in
`docs/test-reports/2026-07-24-authorized-exact-card-image.json`.

## Exact-card hardware gate

The selected official image was inspected and its stock behavior is now known:
the `resize` kernel token invokes an initramfs hook that grows root to the end of
the card, after which another hook randomizes the MBR disk ID and updates boot
references. The authorized custom image replaces that trigger with the bounded,
CID-bound initramfs transaction before first normal boot. Its behavior remains
hardware-unproven until the exact-card flash and first-boot evidence pass.

The identity and layout observations must be repeated immediately before every
destructive phase, the backup must be validated, and all tests must use
explicitly authorized expendable cards. The already-expanded running card is
always refused; the supported recovery is to reflash the custom image, never to
shrink its mounted root.
