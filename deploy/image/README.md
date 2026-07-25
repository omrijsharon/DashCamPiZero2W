# Release image builder and file-only executor boundary

This directory pins the exact selected 32-bit Raspberry Pi OS Lite source and
contains an auditable customization payload. Normal file-image mode keeps the
runtime disabled by omitting the exact-image validation gate. A separate,
unambiguous exact-card trial mode is available only with a closed authorization
record bound to the reviewed CID and byte capacity. Neither mode writes a block
device.

## Pinned source

`source-manifest-v1.json` is a closed manifest for:

- `2026-06-18-raspios-trixie-armhf-lite.img.xz`;
- compressed size `549086704` bytes and SHA-256
  `ea4e84c501d6dd4f4b1d04eb84df133a03f90a05ee2e8ab849185c17c2b0707b`;
- raw size `2675965952` bytes / `5226496` 512-byte sectors;
- DOS/MBR ID `0x4f2c9ea0`;
- the verified FAT32 p1 and ext4 p2 starts, sizes, types, flags, and filesystem
  UUIDs, with empty p3/p4 entries; and
- the exact 6 GiB root target, 1 MiB alignment/reserve, and reviewed 32/64 GB
  capacity examples.

The archive URL is:

```text
https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2026-06-19/2026-06-18-raspios-trixie-armhf-lite.img.xz
```

## Dry-run planning

After downloading the exact archive, choose an absolute path for a new output
file. The output must not exist:

```text
uv run python scripts/build_release_image.py \
  --source C:\absolute\path\2026-06-18-raspios-trixie-armhf-lite.img.xz \
  --output C:\absolute\path\dashcam-release-v2.img
```

The planner reads and hashes the source, validates output-path safety, hashes
the payload, and binds the absolute path, size, and SHA-256 of
`initramfs-closure-v1.json` plus the exact `pi-zero-2-w-armv7l` target into
plan schema v2. It calculates bounded example geometries and prints
deterministic JSON containing argv arrays. It never creates the output or runs
an action.
It refuses a source size/hash mismatch, a relative/existing/symlink/device
output, output equal to input, and unsafe payload entries.

No shell command strings and no SD/block-device executor are included.

The unambiguous execution entry point is `--execute-file-image`. It accepts
only the exact pinned `.img.xz`, a new absolute regular `.img` output, and the
explicit `--target-profile pi-zero-2-w-armv7l`. Omitting or changing the target
profile is refused before output creation. The generic `--execute` option does
not exist. `--flash-device TARGET` is a separate, unconditional
`block_device_execution_disabled` refusal.

Before creating output, the executor re-hashes the source and every payload
file and binds them to the dry-run plan. Immediately before exclusive output
creation, it also re-resolves and re-hashes the closure manifest and refuses a
different path, changed bytes, or changed target profile. The exact bytes read
by this final check are the bytes parsed and used by that execution. It then
emits exact dependency facts.
On this Windows host it is probe-only; execution must run wholly inside Linux
or WSL so paths and subprocesses share one namespace. Use:

```text
uv run python scripts/build_release_image.py \
  --source C:\absolute\path\2026-06-18-raspios-trixie-armhf-lite.img.xz \
  --output C:\absolute\path\dashcam-release.img \
  --probe-file-executor
```

The admitted decompression primitive uses `O_CREAT|O_EXCL`, an exact `xz`
argv with no shell, a 15-minute timeout, raw MBR/filesystem identity
verification, and removes the partial output after every failure. The cmdline
transform is also implemented and tested: it requires exactly one standalone
`resize`, rejects an existing bounded trigger, replaces only that token, and
preserves all other tokens and their order, including Imager tokens.

The disabled-runtime file executor is now admitted on native Linux/WSL when
exact regular executable paths for `xz`, `mcopy`, `mtype`, `mdir`, `debugfs`,
and `zstd` are available. Optional `--*-path` arguments support user-extracted
tools without a global install and are accepted only with an explicit
file-image execution mode.

The executor exclusively creates the output, verifies the raw image, copies p2
to a temporary regular file, and uses read-only `debugfs` dumps to obtain the
exact ARMHF `resize2fs`, `dumpe2fs`, `sfdisk`, and `libfdisk.so.1` bytes. It
does not mount, loop, elevate, or write the root filesystem. It extracts the
boot `config.txt`, `initramfs7`, and `cmdline.txt` through mtools. The closed
target contract is Pi Zero 2 W, `armv7l`, `kernel7.img`, and `initramfs7`.
Execution requires exactly one active `auto_initramfs=1` and refuses explicit
kernel/initramfs overrides, target-affecting conditional assignments, includes,
and 64-bit or OS-prefix overrides. It preserves the selected initramfs's modules-only
early CPIO byte-for-byte, deterministically rebuilds the zstd main CPIO,
replaces the stock resize hook, injects the exact tools/contracts, and replaces
only the standalone `resize` token. It reopens the selected `initramfs7` and
command line and re-verifies the raw partition/filesystem identities. It never
selects or overwrites the generic `initramfs`. Any failure removes only the
exact output inode created by that run.

## Authorized exact-card trial image

The owner authorization for the reviewed card is represented by a small,
closed JSON file. All four fields are required, extra fields are refused, and
the CID, byte capacity, and statement must match exactly:

```json
{
  "schema_version": 1,
  "cid": "fe34325344000000200000031a0192d1",
  "size_bytes": 31457280000,
  "statement": "CID fe34325344000000200000031a0192d1 is expendable and may be completely erased, reflashed, repartitioned, and formatted."
}
```

Create the enabled regular-file trial image only with both explicit arguments:

```text
uv run python scripts/build_release_image.py \
  --source /absolute/path/2026-06-18-raspios-trixie-armhf-lite.img.xz \
  --output /absolute/path/dashcam-exact-card-trial-v2.img \
  --execute-authorized-exact-card-image \
  --authorization-file /absolute/path/exact-card-authorization.json \
  --target-profile pi-zero-2-w-armv7l
```

For the authorized CID in this repository, reserve the new output name
`artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v2.img`.
The executor requires it to be an absolute, nonexistent path; it will never
overwrite the retired v1 artifact.

This mode still creates only a new regular `.img`. Before output creation it
re-hashes the source, payload plan, authorization, and exact layout. It adds
the closed `firstboot-runtime-v1.enabled` gate to the main initramfs. It copies
p2 to a temporary regular ext4 file, then uses exact `debugfs -w` argv calls to
install and mode-check:

- the same runtime gate and exact-card authorization marker;
- `layout-v1.toml`, the first-boot contract, and three initramfs contracts;
- the checked-in post-root script and service; and
- the exact `local-fs.target.wants` service symlink.

The executor reopens and hashes every installed root file, verifies modes,
service-link target, ext4 UUID, boot cmdline, and initramfs, then bounded-stream
copies only the exact p2 range back into its newly created output. It
re-extracts p2 from that output and repeats the root verification. A failure at
any point removes only that run's output inode. It never uses a shell, mount,
loop device, sudo, or device path. `--flash-device` remains unconditionally
refused.

`initramfs-closure-v1.json` now records the target-aware stock `initramfs7`
closure: 14,257,002 bytes, SHA-256
`3f5288ed963028accc5103f13f5a03a9d6d26ef3f58ad38a31736d3802d452b1`,
with 3,924,992-byte main offset and 328/467 early/main entries. The
deterministic v2 build/readback passed; see
`docs/test-reports/2026-07-24-authorized-exact-card-image-v2.json`.

The same manifest closes the dynamic-library graph rooted at the exact
`resize2fs`, `dumpe2fs`, `sfdisk`, and injected `libfdisk` artifact bytes. Its
19 required stock entries include every selected SONAME symlink and versioned
regular target plus ARMHF libc and loader. Customization verifies destination,
mode, and SHA-256 for every entry before rebuilding and again after reopening
the rebuilt main archive. A missing, retargeted, mode-changed, or byte-changed
library is refused. Two temporary real-byte customizer runs against the exact
stock `initramfs7` and exact four artifact bytes were identical: 13,450,240
bytes, SHA-256
`c1e3cba130781f22fa45e0d01b33f22fc37088ec1f767288bb77dddbcb95da5e`.
This is initramfs customization evidence, not a completed v2 image or Pi boot
acceptance result.

## Customization contract and hardware boundary

The stock command line contains the exact token `resize`, which would invoke
the image's unbounded `resize_early` path. The planned transform requires
exactly that token, removes only it, adds the distinct
`dashcam.bounded_provision=v1` trigger, and preserves every other token in
order. That includes Raspberry Pi Imager `firstrun`/`systemd.run` tokens.

`payload/firstboot-contract-v1.json` closes the required runtime behavior:
actual observed MBR/PARTUUID reconciliation, no shrink, exact geometry and
capacity bounds, p3 absence/signature checks, a durable validated
backup-before-table-write sequence, idempotency marker/sentinel reconciliation,
and bounded failure/reboot behavior. It also requires rebuilding and inspecting
the initramfs.

The two `.inert` shell files remain review templates. The initramfs runtime
candidate is now a fail-closed POSIX hook bound to the exact image, card CID,
capacity, PARTUUIDs, filesystem UUIDs, source/target tables, durable backup,
journal, one-reboot cap, and bounded ext4 growth. The post-root entrypoint is
implemented with a closed power-loss journal, exFAT/account/mount/sentinel
reconciliation, and exact `local-fs.target` ordering; its real Pi behavior
remains hardware-unvalidated. Both require
`/etc/dashcam/firstboot-runtime-v1.enabled`. Disabled mode deliberately omits
it. Authorized mode injects the exact closed bytes only after validating the
CID/size-bound authorization record, and also installs
`/etc/dashcam/expendable-card-v1.authorized` with the reviewed CID and sector
count.

The regular-file build proves the offline image transformation only. Physical
flash targeting, first-boot partition growth, exFAT formatting, recovery, and
performance still require the exact-card hardware trial and saved evidence. A
disabled or enabled regular image alone is not evidence for a Milestone 5
PI/DESTRUCTIVE acceptance task.

The earlier reviewed enabled output is retired because it customized the
generic `initramfs`, while the Pi Zero 2 W firmware loaded stock `initramfs7`.
It must not be reflashed:
`artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v1.img`,
size 2,675,965,952 bytes, SHA-256
`7225b9285e48cecab9fd9a7765cb35e6cb4d4769a59b0606b1784abc3b870904`.

The corrected flash candidate is
`artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v2.img`,
size 2,675,965,952 bytes, SHA-256
`ff053cc595c8b7323a9779b28c338ed9c57de13ca5d384ecff69d797159cc802`.
Its offline readback remains historical evidence, not target-boot evidence.

V1 and V2 are retired and must not be flashed again. The next candidate is the
offline-verified v3 recovery/diagnostic image:
`artifacts/images/dashcam-release-authorized-fe34325344000000200000031a0192d1-v3.img`,
2,675,965,952 bytes, SHA-256
`05f08ca82c7578a007c30653daf461832e882fe15db9d6d9ca79b2c6279ad28a`.
It modifies only the bounded-initramfs transaction design: a separate atomic
FAT progress record (maximum 512 bytes) now preserves `before_e2fsck`,
`before_resize2fs`, `complete`, or `refusal`; e2fsck runs as
`timeout -k 10 120 ... e2fsck -p` and accepts only 0/1; resize has explicit
status handling; and post-growth validation compares exact block/device values
rather than shell multiplication. The exact BusyBox timeout is v1.37.0 and
supports `-k`.

This recovery design does not assert a proven v2 cause: the v2 target table was
committed and its root remained unwritten, while the exact failed command was
not recorded. V3's regular-file build and independent readback passed; its
target-card boot remains untested. Keep the authorized card offline and
untouched until a separate identity-checked v3 Imager workflow is deliberately
started. See
`docs/test-reports/2026-07-25-authorized-exact-card-image-v3.json`.
