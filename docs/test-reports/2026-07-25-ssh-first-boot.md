# SSH-first stock-image first-boot evidence

## Scope

- Date: 2026-07-25
- Operation: read-only first SSH discovery after the owner-authorized official
  Raspberry Pi OS Lite 32-bit Trixie flash and pre-first-boot removal of the
  standalone stock `resize` token.
- Excluded: package installation, repository transfer, partition-table writes,
  filesystem growth, formatting, and microphone validation.
- The USB microphone was deliberately disconnected and was not probed.

## Network and SSH identity

- Address: `192.168.68.107`
- Wi-Fi MAC observed by the Windows host: `2c:cf:67:98:4c:49`
- Hostname: `dashcam-pi`
- SSH user: `dashcamadmin`
- SSH host ED25519 fingerprint:
  `SHA256:iNlz0NDhUbn+GfH5Nbb5v9nImSX+zFujVDSqvcHSMOg`
- Imager's configured authorized-key fingerprint matched the laptop
  `id_rsa.pub` fingerprint:
  `SHA256:7ZjZXxSKt6gcW7QON9ai2QYGpM3Fky+f0v+P5KK7vzk`
- Public-key authentication and passwordless `sudo -n` succeeded.

The first login was accepted only after resolving the Imager-configured
username from its local settings. The fresh host key was checked after the
login against the exact board serial and SD CID and was then pinned in the
ignored local file `artifacts/pi-ssh-known-hosts`.

## Exact target identity

- Model: Raspberry Pi Zero 2 W Rev 1.0
- Board serial: `00000000db28ffe4`
- Boot ID: `15cba739-7875-4689-8548-8bd9947f42a2`
- SD CID: `fe34325344000000200000031a0192d1`
- SD capacity: 31,457,280,000 bytes
- Logical sector size: 512 bytes

## OS and first-boot state

- OS: Raspbian GNU/Linux 13.4 (`trixie`)
- Architecture: `armv7l`
- Kernel: `6.18.34+rpt-rpi-v7`
- `ssh.service`, `NetworkManager.service`, and `cloud-final.service`: active
- `cloud-init status --long`: `done`, with no fatal errors

Cloud-init reported one recoverable warning because the image could not find
`cc_netplan_nm_patch`. Networking, SSH, the declared user, and the authorized
key were nevertheless operational. Treat the warning as retained evidence;
do not infer that unrelated first-boot modules succeeded without inspection.

The effective kernel command line contained no standalone `resize` token.

## Stock storage geometry

| Device | Start sector | Sector count | Observed role |
| --- | ---: | ---: | --- |
| `/dev/mmcblk0` | 0 | 61,440,000 | exact authorized card |
| `/dev/mmcblk0p1` | 16,384 | 1,048,576 | FAT boot filesystem |
| `/dev/mmcblk0p2` | 1,064,960 | 4,161,536 | mounted ext4 root |

Root was `/dev/mmcblk0p2`, mounted read-write as ext4. Its measured filesystem
geometry was 520,192 blocks at 4,096 bytes per block. It had 93,421,568 bytes
available after first boot (96% used), with 55,757 free inodes.

This free-space measurement is lower than the earlier pristine-image estimate.
Do not install packages or clone the full repository before controlled
provisioning. Transfer only the minimal reviewed payload needed for Stage A
and Stage B.

## Existing provisioning tools

The stock image already contains the required packages and root-only tools:

- `fdisk=2.41-5`, including `/usr/sbin/sfdisk`, `/usr/sbin/fdisk`, and
  `/usr/sbin/blockdev`;
- `e2fsprogs=1.47.2-3`, including `/usr/sbin/resize2fs`,
  `/usr/sbin/e2fsck`, and `/usr/sbin/tune2fs`;
- `exfatprogs=1.2.9-1+deb13u1`, including `/usr/sbin/mkfs.exfat` and
  `/usr/sbin/fsck.exfat`;
- Python 3.13.5, `rsync`, `curl`, `partx`, and `wipefs`.

`git` is not installed. The initial deployment can therefore use `rsync`
without spending scarce root space on package installation.

## Result

The read-only SSH and stock-layout gates pass for adapting the local
provisioner. No destructive storage operation is authorized by this evidence
alone: the revised stock-p2-to-6-GiB contract, focused local tests, transferred
minimal payload, and immediately preceding exact-card dry run remain required.

## Minimal payload transfer

After local review and validation, the 113,593-byte SSH-development payload
was copied over pinned SSH into tmpfs at
`/tmp/dashcam-ssh-dev-payload-v1-20260725`. No rootfs installation or payload
execution occurred during transfer. The remote directory contained exactly
the six allowlisted regular files; all five manifest entries passed
`sha256sum -c`. The remote `SHA256SUMS` hash was
`1ee510e58ceba23ef780cce46fe9c0ad71651d2ad96fc1a034ca149721e2b0a8`.

## Minimal payload installation

The manifest-gated minimal installer ran twice to prove idempotency. Before
installation there were no local `dashcam` identities, DashCam target paths,
DashCam units, or provisioning journal. After both runs:

- installed `bootstrap.py` SHA-256:
  `e0c813e8a39d6e4ffff3f91f549cc059fec3b0ae176ac7ac393d62e70e504d27`;
- installed `arm-cmdline.py` SHA-256:
  `c5ff81d70f8385a402f4cdaf7f1b1a9fbc3ed1509929022d5beebcc537aa2785`;
- installed contract SHA-256:
  `7d8239d93cca2c665f9d92ea3f9e6aec20a67a70237d473e804617427ae6d867`;
- local user `dashcam` has UID 999, primary local group `dashcam` GID 985,
  home `/var/lib/dashcam`, shell `/usr/sbin/nologin`, and local supplementary
  `dashcam-storage` GID 984 membership;
- `/srv/dashcam` is root:`dashcam-storage`, mode `0550`, and is not writable
  by the `dashcam` user while no exFAT mount is present;
- no provisioning journal or DashCam unit exists;
- the card still has exactly two partitions;
- cmdline SHA-256 remained
  `a57c536af29922659a75115002770c69d5a82dbc151ac580a2629c2769e7aa1f`;
- raw MBR SHA-256 remained
  `2487b1f9af5151759ad1ec762d077424736d38d01a68b72b8d6d4e1634545c3a`;
- root still had 93,790,208 bytes available after installation.

No trigger, service, partition, filesystem, package, or reboot action occurred.
