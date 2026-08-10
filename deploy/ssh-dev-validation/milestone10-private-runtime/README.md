# Milestone 10 private production-runtime qualification

This harness validates the M10 candidate without pointing candidate or rollback
code at the production catalog or recording volume. The parent reads bounded
production identity files before and after the run and requires them unchanged.
It is intentionally exact-Pi only and must be
built at the separately reviewed harness commit while embedding candidate
commit `efc16c651511f7d64428c26dc874cb32d663ac42` and rollback companion
`051f98a70039a448ce0b3475617b399429d5a023` as independent exact-source archives.

The builder creates two independent deterministic source archives. Build the
bundle outside both the repository and `/srv/dashcam`, `/var/lib/dashcam`, and
`/run/dashcam`:

```text
python deploy/ssh-dev-validation/milestone10-private-runtime/prepare-bundle.py \
  --repository REPOSITORY \
  --output FRESH_OUTSIDE_REPOSITORY_DIRECTORY \
  --expected-harness-commit REVIEWED_HARNESS_COMMIT \
  --candidate-commit efc16c651511f7d64428c26dc874cb32d663ac42 \
  --rollback-commit 051f98a70039a448ce0b3475617b399429d5a023
```

Before running, independently review the resulting `SHA256SUMS` and its own
SHA-256. The Pi invocation requires the accepted `5f95` interpreter, root,
the exact board serial, the reviewed inert `dashcam-api` group at gid 983,
and at least 2,701,131,776 root bytes free:

```text
sudo /opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python -I \
  BUNDLE/run.py \
  --bundle BUNDLE \
  --expected-manifest-sha256 REVIEWED_SHA256SUMS_SHA256 \
  --expected-harness-commit REVIEWED_HARNESS_COMMIT \
  --expected-board-serial EXACT_16_HEX_SERIAL \
  --output /var/tmp/m10-private-runtime-FRESH12HEX.json
```

The harness takes both live-qualification locks and runtime-masks the ordinary
`dashcamd.service` before any private camera work. On a successful run it keeps
that mask through unit/cgroup drain, loop and owned-work cleanup, then restores
the exact enabled, inactive and restart-count state before publication. It has
a 900-second global deadline and each transient unit has `RuntimeMaxSec=`. It uses one 416 MiB exFAT
image and one 48 MiB ext4 image at a time. Host-visible nonce mounts are bound
over `/srv/dashcam`, `/var/lib/dashcam`, and `/run/dashcam` only inside each
transient unit's private mount namespace. It does not use `StateDirectory=` or
`RuntimeDirectory=`. Only the parent observes the production catalog/sentinel
read-only for exact before/after hashes; transient candidate and rollback code
see only the private bindings.

The fixed phase order is:

1. Candidate startup reclaim, real camera/hardware encoder, recorder-owned
   listener, active-WRITING and protected/leased/finalizing exclusion, event
   NEXT convergence, three decoded ordinary clips, and clean shutdown.
2. Exact rollback quiesce twice, its read-only guard, and a short real rollback
   recorder start on the same private fixture.
3. Fresh protected-only emergency, which must terminate with clean
   `STORAGE_SAFETY_STOP`, exit 0, no restart, camera, listener, or deletion.
4. Fresh 65-DELETE backlog, which must complete exactly 64 and leave exactly
   one pending before the same clean pre-camera refusal. `StartLimitBurst=1`
   prevents an unexpected nonzero first exit from executing a second start.

The result is atomically no-replace published only at the exact direct-child
`/var/tmp/m10-private-runtime-<12 lowercase hex>.json` grammar, after unit/cgroup drain, socket cleanup, loop
unmount/detach, runtime-mask restoration, production-state comparison, root
reserve validation, and `throttled=0x0` validation. On an ownership ambiguity,
the harness refuses rather than deleting an unknown mount, loop, path or unit.
The owned temporary file, final file, and `/var/tmp` directory receive the
required durability barriers, so no partially written final result is exposed.

An interrupted or refused run deliberately keeps the ordinary recorder masked
unless all owned cleanup and exact prior-state restoration have completed. The
fresh work directory contains a durably transitioned recovery journal binding
the exact prior unit facts, absent prior mask, random mask-ownership token, and
`PREPARED`, `MASK_INTENT`, `MASK_OWNED`, `CLEANED_MASKED`, or `RESTORED` phase.
The journal and work directory remain until unmask, daemon reload, and exact
prior-state verification all succeed. After reviewing its exact nonce, recover with
the same hash-closed harness; recovery stops and drains only deterministic
nonce units, verifies and removes only matching loop backings/mounts/work, and
unmasks the ordinary recorder last:

```text
sudo /opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python -I \
  BUNDLE/run.py --recover-work /var/tmp/dashcam-m10-private.EXACT12HEX
```

Recovery is idempotent before mask creation, after durable mask ownership, after
owned cleanup, and after unmask but before journal removal. `MASK_OWNED` binds
the exact `/dev/null` symlink device, inode, owner, mode, link count, and target;
those facts must match before recovery may unmask. `MASK_INTENT` with a present
mask is deliberately ambiguous and never authorizes unmasking, even when its
target is `/dev/null`; recovery refuses and preserves the journal for review.
The work directory, journal, and private runtime directory are likewise checked
without following links and against their exact owner, mode, link, and device
contract before destructive recovery. No qualification claim or result is
published on a refused/interrupted run.

This evidence does **not** test or claim the HTTP/download data plane, UI,
physical GPS, physical microphone, physical power loss, or deterministic
camera-generated FINALIZING/reclaimer overlap. FINALIZING is exercised as a
durable integrated startup exclusion; the real runtime gate covers the active
WRITING clip. No media, coordinates, raw NMEA, or absolute managed paths are
published.
