# DashCam Bootstrap v1 image recipe

This directory is the build contract for the compressed Bootstrap v1 image.
It supersedes the retired `deploy/image` initramfs mechanism. Nothing here
flashes an SD card, accepts a block-device output, modifies an initramfs, calls
`partprobe`, or asks a running kernel to reread a partition table.

## Source and retained artifacts

`source.json` pins the official 2026-06-18 Raspberry Pi OS Lite 32-bit
(`armhf`, Debian Trixie) `.img.xz` by URL, byte size, and SHA-256. The exact
archive is the build input for this generation. A reviewed Linux builder
decompresses it only into a new regular `.img`, verifies the official
pre-customization extracted size (2,675,965,952 bytes) and SHA-256
(`235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2`),
then performs the required offline pre-customization expansion described by
`build-requirements.json`. It extends only temporary regular-image p2 from the
official 4,161,536 sectors to 8,388,608 sectors (4 GiB), grows its ext4
filesystem offline, and verifies both partition and filesystem size before
installing any declared package or app payload. It extends the sparse regular
image through the future p3 start for the 6 GiB runtime root plus 4 MiB, making
the exact temporary/extracted size 6,991,904,768 bytes, and reads back every
byte of that future-p3 prefix as zero. It then applies the custom stage from
this directory to an independently exposed FAT/rootfs tree. The regular raw
image and work directory are temporary.

The p2 expansion is a mandatory build-executor step, not a first-boot action.
The measured pinned source has only about 298 MiB unreserved ext4 free, so the
declared media packages and app cannot be installed into it unchanged. The
executor must operate without a host block-device target, loop-device
partition-table reread, `partprobe`, or initramfs modification. Stage A still
grows p2 from the checked 4 GiB build layout to 6 GiB on the authorized card;
its source-layout authorization must therefore bind the 8,388,608-sector p2.

Retain only:

- `dashcam-bootstrap-v1.img.xz`;
- `dashcam-bootstrap-v1.rpi-imager-manifest`;
- the manifest/artifact hashes and saved independent readback report.

Cleanup is permitted only after the compressed artifact was reopened, hashed,
decompressed to a verifier-owned stream/file, and matched to the verified raw
size and SHA-256. Cleanup uses an explicit creation ledger and may unlink only
regular files beneath the tool-created work directory. It never recursively
discovers or deletes paths.

## Builder flow

The `pi-gen-stage` directory follows the official pi-gen custom-stage layout:
`00-packages` is the stage package list and `01-run.sh` consumes the staged
assets. `prepare-stage.sh` is run by the Linux build wrapper before image
customization. It copies, without following symlinks:

1. the clean repository source at one recorded full Git commit;
2. `uv.lock` and an offline production wheelhouse resolved with that lock;
3. `deploy/bootstrap/storage` and `deploy/bootstrap/network`;
4. deterministic build metadata and the original FAT `cmdline.txt`.

The Linux wrapper must consume and enforce `build-requirements.json` before
package installation. The checked shell stage does not substitute for that
executor gate and must refuse release if independent readback does not prove
the 4 GiB pre-customization root layout.

The stage remains suitable for official pi-gen after its ordinary Lite stage.
Bootstrap v1's authoritative release path is the stricter equivalent that
starts from the pinned official image: a Linux builder uses libguestfs/FUSE and
an armhf qemu-user/binfmt environment to expose the two filesystems from the
regular raw image, sets `ROOTFS_DIR`/`BOOTFS_DIR`, and runs the same stage. No
host block-device target or kernel partition-table reread is involved. This
equivalent is necessary because rebuilding a nominally similar Lite image from
moving apt repositories would no longer use the exact pinned `.img.xz` source.
The saved build report must pin the builder container digest, libguestfs/qemu
versions, and Debian repository snapshot used for package installation.
`build-requirements.json` intentionally contains unresolved identities today,
so the executable builder refuses a large build. Replace them only with
reviewed exact container/tool-version-output hashes and immutable Debian and
Raspberry Pi snapshot URLs.

The stage:

- installs the declared packages from `00-packages`;
- installs the app into `/opt/dashcam` and its locked production environment
  into `/opt/dashcam/venv`;
- installs the storage and network payloads from their owner directories;
- installs/enables their post-root systemd units;
- installs the recorder and storage-preflight units and default configuration;
  enables the fail-closed storage preflight behind the exact Stage B completion
  marker, but deliberately leaves the recorder disabled until its production
  entry point/runtime gate is complete;
- replaces exactly one standalone `resize` token in FAT `cmdline.txt` with
  `dashcam.bootstrap=v1`, preserving every other token and whitespace;
- records source archive, app commit, lock hash, exact dpkg versions, and stage
  inventory under `/opt/dashcam/build-metadata`;
- preserves the pinned image's `cloudinit-rpi` customization mechanism,
  requires `cloud-final.service`, and orders both Bootstrap storage and network
  selection after it so Imager-created identities and home-Wi-Fi profiles
  already exist. The payload must not clean, mask, disable, or replace
  cloud-init state or configuration. The stage hashes every non-`cmdline.txt`
  FAT file before and after its sole command-line edit and refuses any seed or
  boot-file drift. It also requires the measured source identities
  `cloud-init=25.2-1~bpo13+1+rpt20` (`all`) and
  `raspberrypi-sys-mods=1:20260612` (`armhf`), Raspberry Pi cloud-init config
  and Python support files, and FAT `meta-data`, `network-config`, and
  `user-data`; these source packages are preserved, not added to the moving
  package-install set.

The checked-in shell stage is orchestration, not a release assertion. Package
names, versions, unit ordering, camera plugins, and root free space remain
unaccepted until the independent readback and exact-Pi validation are saved.

## Required independent readback

Run inspection from a fresh verifier process that did not mount or author the
image. It must:

- reread FAT `cmdline.txt`, prove exactly one Bootstrap token and no standalone
  `resize`, and compare all pre-existing command-line tokens;
- prove the source and customized root filesystems retain the Raspberry Pi
  `cloudinit-rpi` package/support files, cloud-init units (including
  `cloud-final.service`), and FAT seed-file inventory/hashes, with the
  Bootstrap services ordered after cloud-final. Package readback must match
  `cloud-init=25.2-1~bpo13+1+rpt20` (`all`) and
  `raspberrypi-sys-mods=1:20260612` (`armhf`), without relying on `.pyc`
  hashes;
- hash all source and output `initramfs`/`initrd` files and prove none changed;
- reread ext4 app, venv, storage/network payloads, enabled services, package
  inventory, lock/source metadata, and absence of DashCam initramfs hooks or
  legacy triggers;
- prove cloud-init's `growpart` and `resizefs` modules remain absent while the
  `raspberry_pi` module and NoCloud `file:///boot/firmware` Imager seed support
  remain present;
- calculate free space projected after Stage B grows ext4 to 6 GiB and require
  at least 2 GiB free;
- independently verify the official compressed archive and pre-customization
  extracted raw size/hash before applying any stage;
- verify the offline build expansion changed only the authorized p2 geometry,
  made p2 and ext4 exactly 4 GiB before package installation, and matches
  `build-requirements.json`;
- verify the compressed customized artifact and its extracted raw size/hash
  recorded in the Imager manifest.

`src/dashcam/provisioning/bootstrap_image.py` contains the closed list of
readback requirement IDs. A manifest is not releasable if any item is absent,
false, mocked, or derived only from the builder that wrote the image.

No image build or SD-card flash is authorized merely by this recipe.
