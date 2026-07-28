# Milestone 6 live finalization and recovery validation

Date: 2026-07-26

## Result

The reviewed recorder finalization path is now installed and exercised on the
declared Pi and its mounted `DASHCAM` exFAT volume. It wrote production video
to `pending`, read back canonical JSON sidecars, persisted the ext4 lifecycle
and `FINALIZE` intent, promoted pairs into `clips` without replacement,
reconciled an intentionally interrupted promotion at the next service start,
and cleanly finalized the active shutdown fragment.

This completes the two exact-Pi Milestone 6 finalization/reconciliation tasks.
It does not complete Milestone 6: status metrics, camera/encoder recovery with
backoff, ten consecutive clips, normalized continuity, and endurance remain
unaccepted.

## Exact target and hash-closed deployment

- Pi/card CID: `fe34325344000000200000031a0192d1`
- Boot ID: `601693e3-fa96-427e-906b-1621463a15cd`
- Recording mount: `/dev/mmcblk0p3`, exFAT label `DASHCAM`, UUID `7EED-3EA7`
- Installed release: `0.1.0.dev0-3d4551fdc05ae7ad`
- Bundle archive SHA-256:
  `acbcb3ac51b65afc9d57b409fdff9f3d5da15c86e0a3822b68aa63f30c86f3b7`
- Manifest SHA-256:
  `7b20e6f5374026c1c4543d3412f3055beb803466c1c2caf52b90a24888cbb707`
- Application wheel SHA-256:
  `b851cfe251fff6daecd44b7ae7beb58cb7bb4be24e6c7545ecadbdabdbe0177b`

The authoritative dry-run, apply, idempotency dry-run, and idempotency apply
all reported zero APT work. Their saved JSON SHA-256 values are respectively:

- `478c355f98133a0b74a583a5d0ea2c08e568cbc37d924236f98a0109f507bef4`
- `2299f2da144a71f2007e5bb1a6ce56ea029d6bca771ef353d4e7294d4892ff57`
- `f235c34f57b0bd18dbc8fe9539efcd7187d70d450fccd1258a9d6f518794fdb8`
- `8bdbd00aea83aa4da221f04a1cb4d1d1962205f2fe215909621cd44047609c83`

All four paired stderr files were empty (the SHA-256 of the empty file). Raw
deployment evidence is ignored under
`artifacts/pi-finalization-live-v2/deployment/`.

## Fail-closed correction before acceptance

An early exact-Pi finalization attempt for sequence `000011` stopped
fail-closed when a sidecar's `fps_nominal` was constructed as JSON integer `30`
but its strict parser normalized it as `30.0`. The pending diagnostic was not
promoted, altered, or synthesized. Its preserved 60,023,931-byte MP4 has
SHA-256
`5ea2653c1e162e52151d0ccf321879c67ecee655b88e569f9dd246344289ade9`;
the corresponding 973-byte staged sidecar evidence is
`artifacts/pi-finalization-live-v2/boot-601693e3fa96-000011.partial.json.evidence`
with SHA-256
`574fd16ca1eca1931ab6827323efa246a7feafbd918520d2bae3bddbe38e6ac8`.

The corrected release uses the canonical numeric representation and passed the
new finalization tests below. Existing diagnostic pending members, including
the earlier sequence range, remain preserved and are not catalogued clips.

## Live promotion, collision, and shutdown evidence

The finalization harness itself was hash closed (manifest SHA-256
`5955c22a5ced2f3432d2d4c59aac86381da71f71a9f4b32ac0f79aea814971ef`).

### Collision refusal

While `dashcamd` was recording sequence `000014`, the harness armed the
case-colliding sentinel `clips/BOOT-601693E3FA96-000014.JSON`. The active
source video grew from 56,021,347 to 60,029,729 bytes during the natural
near-60-second rollover. The promotion refused the existing case-insensitive
target: no target MP4/JSON, source sidecar, or `FINALIZE` intent was created.
The fail-closed condition occurred at the rollover; it was not caused by the
subsequent stop command. After the service was inactive, the harness removed
only its closed sentinel, with no manual pair move or delete.

Evidence: `collision-armed.json` (SHA-256
`c25886922397f36ed2d54fd7a51f115213e3777b67f2f94271166048f7e5c8a0`),
`collision-refused.json` (`61e8f05807b5c0a60e9048178d9c24932e3317188649c87262dce6814b6906dd`),
and `collision-cleanup.json`
(`5c8012497b97a469e05a622f377ea7712f46f817e8a321f4413e3d2bec295d0c`).

### Interrupted promotion and restart reconciliation

The harness injected a process interruption after the synthetic `FINALIZE`
intent was durable and exactly one target member had moved. The synthetic test
identity was clip `d07aa00c-b05d-427b-938d-f45d4f016b16`, sequence `921014`:

- before restart, its `FINALIZING` catalog row and `FINALIZE` intent remained;
  the 969-byte source sidecar SHA-256 was
  `7232237976c6212ed6346ac7b777a604c6fff2f1f04ef89b349cf6bd9f50bf7b`,
  while the 64-byte target MP4 SHA-256 was
  `3357ce855877083c7750ac20e6db0d5d6a6644211d6aaecff24182b81d4f4802`;
- on the next normal `dashcamd` start, reconciliation promoted the sidecar,
  removed both pending sources and the intent, and marked the row `FINALIZED`
  with `pair_reconciled=true`.

This is intentionally synthetic recovery data: its 64-byte MP4 proves the
pair-operation state machine only and is not claimed to be playable production
media. The post-crash inspection SHA-256 is
`00055e86b02d2c9ad4d02fd89a6430b3c5f912c74e89ee8a4dd133a10732af8d`; the
recovered-state SHA-256 is
`2dc578292fffb3d63c379f7538e63b4fc59ca97fdcbed5a44c47809cd4e6ebf6`.

### Production clips and final shutdown fragment

The ordinary production clip `000016` and the final active shutdown fragment
`000017` were both promoted as MP4+JSON pairs. Each had no pending members or
related pending intent, passed catalog readback as managed `FINALIZED` with
`pair_reconciled=true`, passed independent FFmpeg decode, and began with an
I/IDR frame.

| Sequence | Role | Duration | MP4 SHA-256 | JSON SHA-256 |
| --- | --- | ---: | --- | --- |
| `000016` | ordinary rollover | 59.988667 s | `3dbad34e34c04826f4ec4067e229ac76943adad83b0a924961a9bf11105a691e` | `de2cdb257ea3496a870d1a0cd575f88d3b9f483d9cc0c36b184c2e125a9fd2c8` |
| `000017` | active fragment finalized by clean shutdown | 24.528667 s | `be69b427cae52fa573ee3f34070be6d72faced797d2d712d3d22529e327ec339` | `edf8425f0bcca5e442aa659a7bafbb5acfe3ee4a8c36665b3adfd8f2f4ddd8bb` |

`ffprobe` reported H.264 High, Level 4.1, 1920x1080 at 30/1 for both files;
their saved outputs and first-frame checks are under
`artifacts/pi-finalization-live-v2/`. The final service state at
2026-07-26T22:58:47+03:00 was `Result=success`, `ExecMainStatus=0`, zero
restarts, and `inactive/dead`. The AP fallback service stayed inactive and was
never started.

The final catalog contains exactly three reconciled `FINALIZED` rows: synthetic
`921014` plus production `000016` and `000017`; `pending_intents` is empty.
Its saved-state SHA-256 is
`a560e497178d43ec5b4b778bd0b307e1858b1d9797c17d8c11023b1f132873e4`.
Final free space was 2,992,340,992 bytes on root and 23,861,264,384 bytes on
exFAT. The final system-state capture SHA-256 is
`0923fbf220da80c575ec104f45708f6b329432b6464fc7d61069bda66ae3c3c1`.

## Remaining acceptance boundaries

This run does not establish ten consecutive production clips, normalized
boundary continuity, two-hour endurance, production frame/drop/effective
settings metrics, or camera/encoder recovery/backoff. It also does not make an
exFAT power-loss guarantee. Those tasks remain unchecked.
