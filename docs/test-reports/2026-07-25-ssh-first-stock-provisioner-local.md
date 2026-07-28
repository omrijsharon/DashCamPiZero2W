# SSH-first stock provisioner local evidence

## Scope

- Date: 2026-07-25
- Scope: local implementation and model/fake-runtime validation only.
- No Pi filesystem, partition table, or service was mutated by these tests.
- The USB microphone is disconnected and is outside this storage gate.

## Implemented contract

- Added a distinct SSH-development trigger:
  `dashcam.bootstrap=ssh-dev-v1`.
- Preserved the deferred release-image `dashcam.bootstrap=v1` contract and its
  image-authored zero-prefix requirement.
- Bound the SSH-development contract to CID
  `fe34325344000000200000031a0192d1`, 31,457,280,000 bytes, 512-byte sectors,
  p1 start/count 16,384/1,048,576, and stock p2 start/count
  1,064,960/4,161,536.
- The target p2 is exactly 12,582,912 sectors (6 GiB). Target p3 starts at
  sector 13,647,872 and contains 47,790,080 sectors after the aligned trailing
  reserve.
- SSH-development journal schema 2 stream-hashes exactly 4 MiB at the future
  p3 start in bounded 64 KiB reads before Stage A. Stage B requires the
  identical hash plus no recognized filesystem or `wipefs` signature before
  format intent. Hash drift, absence, or malformed evidence fails closed.
- The exact known official-image cloud-init terminal warning is accepted only
  by schema 2 as `done_known_degraded`. Release schema 1 still requires clean
  `done`; any warning/error/shape drift remains non-ready.
- Added a true live `--dry-run`. It loads the closed contract and journal,
  collects full read-only evidence, invokes only the pure planner, emits
  bounded deterministic JSON, and cannot invoke execution, writes, sync, or
  reboot. `ready=false` returns exit 3 for deferred/refused/latched states;
  collection/runtime errors return exit 4.

## Exact-Pi read-only observation that informed the contract

Before any partition mutation, the future p3 4 MiB prefix on the freshly
stock-flashed authorized card hashed to:

`8c01bea511d15baa18fdbecb8caf88af33f16811a4c7fb8da68a4ea26a22a058`

It was not all zero and a bounded `blkid` offset probe found no recognizable
filesystem. This is expected because Raspberry Pi Imager writes the compressed
source image but does not sanitize all later unallocated sectors. The schema-2
hash-stability proof avoids both an unnecessary raw zeroing write and an unsafe
signature-only assumption.

## Minimal payload

The reviewed local payload was built outside the repository at
`C:\Users\tamipinhasi\AppData\Local\Temp\dashcam-ssh-dev-payload-v1-20260725`.
It contains six files totaling 113,593 bytes:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `README.md` | 3,140 | `2fb9e233419b9a5d6c8b82c9b27383e80c68096197ba14afbf475699894896d8` |
| `arm-cmdline.py` | 11,479 | `c5ff81d70f8385a402f4cdaf7f1b1a9fbc3ed1509929022d5beebcc537aa2785` |
| `authorized-exact-card-ssh-dev-v1.json` | 532 | `7d8239d93cca2c665f9d92ea3f9e6aec20a67a70237d473e804617427ae6d867` |
| `bootstrap.py` | 92,620 | `e0c813e8a39d6e4ffff3f91f549cc059fec3b0ae176ac7ac393d62e70e504d27` |
| `install.sh` | 5,405 | `3cf33f263f632688e3c63bb4cd9f27c470ab880115ec422d22bcbbcda7d417d8` |
| `SHA256SUMS` | 417 | `1ee510e58ceba23ef780cce46fe9c0ad71651d2ad96fc1a034ca149721e2b0a8` |

The installer leaves the underlying rootfs `/srv/dashcam` directory
non-writable (`0550`) and cannot edit cmdline, enable services, install
packages, partition, format, or reboot.

## Validation

- Full pytest suite after payload hardening: **978 passed, 5 skipped**.
- Ruff: passed.
- Strict mypy: passed.
- `git diff --check`: passed, apart from informational Windows line-ending
  notices.

Focused coverage is in `tests/unit/test_bootstrap_stock_dev.py`, including:

- exact source and target geometry;
- wrong CID, size, source p2, and target geometry refusals;
- release/development trigger confusion;
- stable nonzero prefix acceptance and missing/malformed/drift refusal;
- closed schema-1/schema-2 journal parsing;
- bounded region-reader behavior;
- exact known cloud-init warning classification and drift refusal;
- actionable, deferred, and refused live dry-run outcomes with zero mutation.

## Remaining gate

Do not execute Stage A yet. Transfer only the reviewed payload and verify its
manifest on the Pi; install its files/account scaffolding without enabling or
running anything; arm the distinct boot marker and reboot; then save a real-Pi
`ready=true` dry-run immediately before the authorized Stage A transaction.
