# Milestone 10 private production-runtime qualification

This harness validates the M10 candidate without pointing candidate or rollback
code at the production catalog or recording volume. The parent reads bounded
production identity files before and after the run and requires them unchanged.
It is intentionally exact-Pi only and must be
built at the separately reviewed harness commit while embedding candidate
commit `efc16c651511f7d64428c26dc874cb32d663ac42` and rollback companion
`5268bd8e2f0dfe18d2a70ec142af45e8198b3f1a` as independent exact-source archives.

The builder creates two independent deterministic source archives. Build the
bundle outside both the repository and `/srv/dashcam`, `/var/lib/dashcam`, and
`/run/dashcam`:

```text
python deploy/ssh-dev-validation/milestone10-private-runtime/prepare-bundle.py \
  --repository REPOSITORY \
  --output FRESH_OUTSIDE_REPOSITORY_DIRECTORY \
  --expected-harness-commit REVIEWED_HARNESS_COMMIT \
  --candidate-commit efc16c651511f7d64428c26dc874cb32d663ac42 \
  --rollback-commit 5268bd8e2f0dfe18d2a70ec142af45e8198b3f1a
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

The harness takes both live-qualification locks and installs a fresh, owned
runtime drop-in for ordinary `dashcamd.service` before any private camera work.
The nonce-bound drop-in combines `RefuseManualStart=yes` with a deliberately
absent `ConditionPathExists=` target. After `daemon-reload`, the harness requires
the exact `DropInPaths`, `RefuseManualStart=yes`, inactive state, and absent
condition target before proceeding. On a successful run it keeps that exclusion
through unit/cgroup drain, loop and owned-work cleanup, then restores the exact
prior unit properties before publication. It has
a 900-second global deadline and each transient unit has `RuntimeMaxSec=`. It uses one 416 MiB exFAT
image and one 48 MiB ext4 image at a time. Host-visible nonce mounts are bound
over `/srv/dashcam`, `/var/lib/dashcam`, and `/run/dashcam` only inside each
transient unit's private mount namespace. It does not use `StateDirectory=` or
`RuntimeDirectory=`. Only the parent observes the production catalog/sentinel
read-only for exact before/after hashes; transient candidate and rollback code
see only the private bindings.

Because the inherited production mount remains as a hidden lower mount row in
that namespace, transient units bind the exact reviewed harness over
`/usr/bin/findmnt`. This adapter accepts only the production preflight argv,
calls the real binary at a read-only private bind, and returns only the single
row whose `maj:min` equals `stat(/srv/dashcam)`. The pre-camera bind probe
requires one normalized row and exact device equality; every other shape
refuses before camera startup.

On this exact systemd 257 stack, `systemctl show` serializes the structured
`Conditions` property only as the exact sentinel `[unprintable]`. The harness
requires that sentinel and records `conditions_property_parsed=false`; it does
not claim a separately parsed D-Bus condition. Admission instead rests on the
descriptor-bound canonical drop-in content and hash, exact loaded drop-in path,
observed `RefuseManualStart=yes`, absent nonce marker, inactive unit, and
successful reload.

The fixed phase order is:

1. Candidate startup pending-FINALIZE convergence followed by reclaim, real
   camera/hardware encoder, recorder-owned listener, active-WRITING and
   protected/leased exclusion, event NEXT convergence, three decoded ordinary
   clips, and clean shutdown. During the three live threshold crossings only,
   an upward free-space jump is accepted because the immediately following
   catalog gate requires a new completed DELETE; all offline filler phases
   retain the exact target gate. Live crossings use paced 256 KiB extents rather
   than the offline 8 MiB extent size so the test driver does not manufacture
   burst I/O pressure. Per-clip sidecar drop-counter unavailability
   remains truthful as `null` evidence; the final runtime status must instead
   provide a real drop source and an exact aggregate zero, alongside strict
   packet PTS/DTS and boundary checks.
2. Exact rollback quiesce twice, its read-only guard, and a short real rollback
   recorder start on the same private fixture.
3. Fresh protected-only emergency, which must terminate with clean
   `STORAGE_SAFETY_STOP`, exit 0, no restart, camera, listener, or deletion.
4. Fresh 65-DELETE backlog, which must complete exactly 64 and leave exactly
   one pending before the same clean pre-camera refusal. `StartLimitBurst=1`
   prevents an unexpected nonzero first exit from executing a second start.

The result is atomically no-replace published only at the exact direct-child
`/var/tmp/m10-private-runtime-<12 lowercase hex>.json` grammar, after unit/cgroup drain, socket cleanup, loop
unmount/detach, runtime-exclusion restoration, production-state comparison, root
reserve validation, and `throttled=0x0` validation. On an ownership ambiguity,
the harness refuses rather than deleting an unknown mount, loop, path or unit.
The owned temporary file, final file, and `/var/tmp` directory receive the
required durability barriers, so no partially written final result is exposed.

An interrupted or refused run deliberately keeps the ordinary recorder excluded
unless all owned cleanup and exact prior-state restoration have completed. The
fresh work directory contains a durably transitioned recovery journal binding
the exact prior unit facts, absent prior drop-in, random exclusion-ownership
token, and `PREPARED`, `EXCLUSION_INTENT`, `EXCLUSION_OWNED`,
`CLEANED_EXCLUSION`, or `RESTORED` phase. `EXCLUSION_OWNED` records the exact
directory and file device, inode, owner, mode, link count, canonical content,
content hash, and impossible condition path. The journal and work directory
remain until identity-bound drop-in removal, daemon reload, and exact
prior-state verification all succeed. After reviewing its exact nonce, recover with
the same hash-closed harness; recovery stops and drains only deterministic
nonce units, verifies and removes only matching loop backings/mounts/work, and
removes the owned exclusion last:

```text
sudo /opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python -I \
  BUNDLE/run.py --recover-work /var/tmp/dashcam-m10-private.EXACT12HEX
```

Recovery is idempotent before exclusion creation, after durable exclusion
ownership, after owned cleanup, and after removal but before journal deletion.
The exact directory/file facts must match before recovery may remove either.
`EXCLUSION_INTENT` with a present drop-in is deliberately ambiguous and never
authorizes removal; recovery refuses and preserves the journal for review.
The work directory, journal, and private runtime directory are likewise checked
without following links and against their exact owner, mode, link, and device
contract before destructive recovery. No qualification claim or result is
published on a refused/interrupted run.

This evidence does **not** test or claim the HTTP/download data plane, UI,
physical GPS, physical microphone, physical power loss, or deterministic
camera-generated FINALIZING/reclaimer overlap. The durable pending FINALIZE is
exercised as integrated startup convergence and is required to complete before
the first recorded DELETE completion; it is not claimed as simultaneous
reclaimer exclusion. The real runtime gate covers the active WRITING clip.
No media, coordinates, raw NMEA, or absolute managed paths are published.
