# SSH-first Stage A/B exact-card progress — 2026-07-25

## Scope and authorization

This report covers only the explicitly authorized, expendable SD card:

- CID: `fe34325344000000200000031a0192d1`
- Size: `31,457,280,000` bytes (`61,440,000` 512-byte sectors)
- Pi boot medium: `/dev/mmcblk0`
- Development trigger: `dashcam.bootstrap=ssh-dev-v1`

The USB microphone was intentionally disconnected and is irrelevant to this
storage-provisioning gate.

## Stage A passed

The live Stage A dry-run returned `ready=true`. Stage A then made its one
authorized full-table write, verified raw LBA0, committed the result durably,
synced, and initiated its one controlled reboot. The boot ID changed from
`cf1614e7-35cb-42e1-9223-f2473dd80978` to
`a8b7c6c7-bc09-4d8a-866b-7f554780984e`.

Post-reboot readback:

- Committed MBR SHA-256:
  `04eeff87140367c3046fcab407ff87e695cd3cfcb70f897049e612dd6ad2dc0a`
- p1 start/count: `16,384` / `1,048,576`
- p2 start/count: `1,064,960` / `12,582,912`
- p3 start/count: `13,647,872` / `47,790,080`
- Journal-bound p3 4 MiB prefix SHA-256 remained:
  `8c01bea511d15baa18fdbecb8caf88af33f16811a4c7fb8da68a4ea26a22a058`
- `wipefs --json /dev/mmcblk0p3` reported no signatures.

The source MBR and `sfdisk` dump exist on both ext4 and FAT. The two copies
match:

- Source MBR SHA-256:
  `2487b1f9af5151759ad1ec762d077424736d38d01a68b72b8d6d4e1634545c3a`
- Source table dump SHA-256:
  `aff41dafc3ada952af6443a2ecbe61fd2aadac54c4b0a0b943f289b26eae3a4d`
- Backup manifest SHA-256:
  `4daeb3510aaf66d7787e9ab248aa085498b93520ecc10be21677df861eb51977`

## Stage B dry-run passed, execution stopped before format

The first post-reboot Stage B dry-run returned `ready=true`. The real Stage B
invocation ran `resize2fs /dev/mmcblk0p2`, then stopped before format with a
latched `target_layout_mismatch`:

`resize2fs returned but exact ext4 target size was not observed`

The stop was safe:

- p2 block device size is exactly `6,442,450,944` bytes.
- The ext4 superblock reports block count `1,572,864` and block size `4,096`,
  whose product is exactly `6,442,450,944` bytes.
- p3 still has no filesystem and no `wipefs` signature.
- The journal-bound p3 prefix hash is unchanged.
- No `/srv/dashcam` exFAT mount or completion marker was created.

## First root cause and exact recovery

The provisioner used `lsblk FSSIZE` as if it were the ext4 superblock's total
geometry. On util-linux 2.41, the live Pi reports `6,304,432,128` bytes there,
which corresponds to filesystem data blocks after ext4 metadata rather than
the total ext4 block count. The filesystem itself did grow to the exact target.

The observer was corrected to use bounded `dumpe2fs` block count multiplied by
block size. The exact regression and refusal tests passed. The corrected
provisioner SHA-256 at this boundary was
`85b67435dd03a792ccdb342eb9c4a2857412079b807837bb616224e71cf9f87b`.

The original refused journal was preserved locally and on the Pi with SHA-256
`fd067e46b2b889d4bf2f0a95bc5a4afa365779ce8136500a5e790c6e388f2c80`.
A one-off helper bound to the exact journal, helper/provisioner/contract
hashes, boot/card/table/ext4/p3 identities, and absence of storage
configuration passed a write-free dry run. It durably archived the refusal,
wrote a prepared/completed audit, and restored only `TABLE_COMMITTED`. The
replacement journal SHA-256 was
`d420ac0234a87c748e80bfd9d20b95508048a98d3583f56e6fd807243f983ff3`.
The next normal Stage B dry-run observed ext4 already exact and did not plan a
second `resize2fs`.

## exFAT reconciliation correction and exact recovery

The resumed Stage B recorded format intent and ran its one permitted
`mkfs.exfat`. It then stopped before mount/configuration because `wipefs`
reported both:

- `exfat` at offset `0x3`; and
- `dos` at offset `0x1fe`.

Read-only evidence proved this was the normal exFAT boot sector: raw bytes
contained `EXFAT` at offset `0x3` and `55 aa` at offset `0x1fe`.
`fsck.exfat -n` reported the new volume clean. `blkid` reported exact type
`exfat`, label `DASHCAM`, UUID `7EED-3EA7`, and PARTUUID `4f2c9ea0-03`.
The volume was unmounted and no sentinel or completion marker existed.

The planner was corrected to require exactly one `exfat` and one `dos`
signature only after durable format intent and in the formatted phase.
Pre-format blankness still requires zero signatures; missing, duplicate,
additional, or foreign signature shapes still refuse.

The second refused journal was preserved locally and on the Pi with SHA-256
`50d46589d6b86bdcdde4126e781b53bd3886f7f1eef0a8c529f52a72a2f563dd`.
A separate exact-event, state-only helper passed its write-free dry run,
archived/audited that refusal, and restored only `FORMAT_INTENT`. The
replacement journal SHA-256 was
`c87600b8cb9cf5b31f225effadedc7b9b6b5fb495a2ade4578048309fc3837de`.
The next Stage B dry-run planned only UUID capture and no `mkfs.exfat`.

## Stage B completion

The final normal Stage B invocation completed without output or error.
Post-completion evidence:

- Journal phase: `configured`; data UUID: `7EED-3EA7`
- Journal SHA-256:
  `4915add53855ff35200fa7d0c1ab79b7550642be34a5126ceb626d5a1890452d`
- Completion SHA-256:
  `e86701dc2b1bfdf21fe35397289db185e9bb0d333d3a5aee9b75363c54e9b627`
- Sentinel SHA-256:
  `fa0b22c59ec4d7b5c789b1c9600beb49eb93c4bbe141101e6bd27723d62f6ed6`
- Storage environment SHA-256:
  `5c588e4f294d754844fd9467d5104ae596972095fde1a3a8597f9781ece8cc1a`
- fstab SHA-256:
  `a24c88b5d025a16320c888a3a53a42befa2a9937b0ad942f99a78a08be7eb5bf`
- MBR SHA-256 remained:
  `04eeff87140367c3046fcab407ff87e695cd3cfcb70f897049e612dd6ad2dc0a`
- Root free space: `4,165,500,928` bytes
- exFAT capacity/free: `24,466,423,808` / `24,465,375,232` bytes
- Mount options included `rw,nosuid,nodev,noexec,noatime`, UID `999`, GID
  `984`, `fmask=0137`, and `dmask=0027`.
- The `dashcam` account could write both `/srv/dashcam` and
  `/srv/dashcam/clips`.
- `fsck.exfat -n` reported clean after sentinel/directories were created.

A completed dry-run returned `verified completion is a no-op`. A subsequent
real Stage B invocation left the journal, completion, sentinel, environment,
fstab, and MBR hashes byte-for-byte unchanged.
