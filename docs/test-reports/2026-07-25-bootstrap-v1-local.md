# DashCam Bootstrap v1 local implementation report

Date: 2026-07-25  
Scope: repository and local/WSL logic only  
Hardware/card mutation: none

## Outcome

The retired initramfs image path has been replaced in the specification and
implementation by a normal post-root Bootstrap v1 design. The local storage
transaction, fail-closed recorder preflight, and NetworkManager fallback policy
are implemented and locally tested. No SD-card read, write, format, flash, or Pi
operation was performed during this work.

The release image itself is **not built or flash-ready**. The repository now
has an initial committed baseline, but the executable image builder still
correctly refuses while its Linux builder identities are unresolved.

## Pinned source contract

- Image: Raspberry Pi OS Lite 32-bit Trixie, 2026-06-18
- Imager customization format: `cloudinit-rpi`
- Compressed bytes: `549086704`
- Compressed SHA-256:
  `ea4e84c501d6dd4f4b1d04eb84df133a03f90a05ee2e8ab849185c17c2b0707b`
- Extracted bytes: `2675965952`
- Extracted SHA-256:
  `235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2`
- Source p1: start `16384`, size `1048576` sectors
- Source p2: start `1064960`, size `4161536` sectors
- Build-time p2/ext4: exactly 4 GiB (`8388608` sectors)
- Runtime p2/ext4 target: exactly 6 GiB
- Future p3 prefix authored as zero: 4 MiB

The local source archive matches the pinned compressed size/hash. Its streamed
extracted size/hash and MBR geometry were independently checked during source
review.

## Implemented local contracts

- Exact one-token `resize` to `dashcam.bootstrap=v1` command-line transform.
- Preservation/readback contract for Imager cloud-init seed and initramfs
  files.
- Exact-card Stage A transaction with durable boundaries, one full-table
  `sfdisk --no-reread --force`, raw MBR readback, sync, and controlled reboot.
- Stage B different-boot validation, exact online ext4 resize, one-time exFAT
  format intent, UUID mount, sentinel/directories, closed storage identity
  handoff, and completion-marker-last behavior.
- Fail-closed recorder-volume preflight at `/srv/dashcam`.
- Bounded client-first Wi-Fi selection and stable per-device fallback AP.
- Idempotent rootfs payload installers and service enablement.
- Regular-file-only image plan/executor/verifier scaffolding with pinned source
  hashes, 4 GiB offline layout, future-p3 zero-prefix readback, compressed
  extraction proof, and manifest generation only through the executor path.

## Integration corrections made during review

- Replaced the invalid `id -g dashcam-storage` user lookup with a strict
  `/etc/group` lookup. The exFAT mount and storage identity now use the actual
  `dashcam-storage` GID.
- Refused a `COMPLETE` journal when the exact completion marker is absent.
- Enabled storage preflight only behind
  `/var/lib/dashcam/provisioning/layout-v1.complete.json`; kept the recorder
  disabled.
- Bound image-builder command input, output, and runtime.
- Changed writable image customization to one combined libguestfs appliance
  for root and boot, avoiding concurrent writable appliances on one raw image.
- Added immediate read-only 4 GiB ext4 verification after offline growth.
- Added an MBR byte comparison proving offline growth changes nothing outside
  partition entry 2.
- Removed the standalone manifest-writing bypass.
- Replaced a production-sized 6.99 GB unit-test fixture with compact injected
  geometry. Three failed disposable pytest files were truncated to recover
  approximately 12 GB on the Windows system drive.

## Validation

Commands:

```text
uv run pytest -q
uv run ruff check .
uv run mypy --strict
bash -n for every checked-in .sh file through Ubuntu-22.04 WSL
```

Results:

- Pytest: `936 passed, 4 skipped`
- Ruff: passed
- Strict mypy: passed for 119 source files
- Shell syntax: passed

The four skips are declared platform-specific POSIX/symlink/fsync checks. The
equivalent Linux behavior remains part of the real builder/Pi gate.

The storage fault matrix covers six Stage A and eight Stage B restart
snapshots, destructive-command non-repetition, foreign/torn state latching,
format-intent reconciliation, and completion-write power cuts. These are model
and fake-runtime results, not physical power-loss evidence.

## Release-image blockers

The following remain deliberately unchecked:

1. Replace the placeholder builder container/tool identities and repository
   locations in `deploy/bootstrap/image/build-requirements.json` with a
   reviewed, executable Linux contract. No validated immutable Raspberry Pi
   package snapshot has yet been selected.
2. Produce the application wheel and dependency bundle from one clean recorded
   commit and bind their complete installed identities to that commit and
   `uv.lock`.
3. Replace the bare armhf `chroot` execution with a reviewed private chroot
   environment (or official image-build helper) that supplies bounded
   `/proc`, `/sys`, `/dev`, `/run`, DNS, qemu/binfmt validation, and guaranteed
   cleanup.
4. Deepen independent readback to verify exact application/payload/unit
   inventories and final package versions, including cloud-init identities.
5. Perform the real 4 GiB image build and fresh-process readback on a Linux
   filesystem with libguestfs/qemu available.
6. Retain and verify the `.img.xz`, verification report, and Imager manifest.

Until those gates pass, do not flash Bootstrap v1.
