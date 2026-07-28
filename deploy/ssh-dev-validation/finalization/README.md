# Live finalization interruption harness

This one-shot payload validates production `ClipCatalog`,
`RecorderClipFinalizer`, and `DurableRootedFinalizationFilesystem` behavior on
the exact authorized Pi. It is intentionally not a general-purpose test tool.
It creates one deterministic, nonempty synthetic ISO-BMFF source on the real
`/srv/dashcam`, commits the real catalog row and `FINALIZE` intent in
`/var/lib/dashcam/catalog.sqlite3`, and SIGKILLs only its worker immediately
after the production filesystem completes and durably flushes the first
no-replace move.

The harness does not start or stop `dashcamd`, replace/copy/edit the catalog,
move or remove a half-pair, format anything, or provide a cleanup command. It
never touches the retained `pending` diagnostic sequences `000000` through
`000010`; its derived sequence is always `900000` through `999999`. Every
filesystem and catalog identity is verified absent before the source is
created. A previous attempt is therefore retained and causes a fail-closed
refusal instead of being overwritten or cleaned.

## Preconditions

- Run the installed release interpreter directly as user `dashcam`, never
  root. The three production modules must resolve below
  `/opt/dashcam/releases/`.
- `dashcamd.service` must be fully inactive for preparation, injection, and
  post-crash inspection.
- The exact authorized CID
  `fe34325344000000200000031a0192d1`, `/dev/mmcblk0p3`, exFAT label `DASHCAM`,
  UUID `7EED-3EA7`, distinct writable `/srv/dashcam` mount, canonical sentinel,
  and production catalog must all pass the closed gate.
- Copy only this reviewed directory to the Pi. From the copied directory,
  verify `sha256sum -c SHA256SUMS`, then record the manifest hash with
  `sha256sum SHA256SUMS`. Pass that exact 64-character value on every command.
- Generate a fresh UUIDv4 once (for example, `python -c "import uuid; print(uuid.uuid4())"`)
  and record it. Reuse exactly that UUID in all phases.

The commands below use shell variables only for readability. Resolve and
record their literal values before execution:

```sh
HARNESS=/path/to/reviewed/finalization/run.py
PYTHON=/opt/dashcam/current/venv/bin/python
MANIFEST_SHA256=<64-character-reviewed-SHA256SUMS-hash>
TEST_ID=<fresh-canonical-lowercase-UUIDv4>
```

## Phase 1: prepare and prove the pre-crash shape

With `dashcamd` inactive:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  prepare --identity "$TEST_ID" \
  | tee /tmp/dashcam-finalization-pre-crash.json
```

The canonical, machine-readable result proves exactly one source MP4 exists in
`pending`, both targets and the pending JSON are absent, and the UUID has no
catalog row or durable intent. Save the JSON and its SHA-256 before proceeding.

## Phase 2: durable first move, SIGKILL, and post-crash proof

Still with `dashcamd` inactive:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  inject-crash --identity "$TEST_ID" \
  | tee /tmp/dashcam-finalization-post-crash.json
```

The parent accepts only a worker death by `SIGKILL`. It then proves the exact
interrupted shape: target MP4 present with the original hash, pending canonical
JSON present, target JSON and pending MP4 absent, one unreconciled `FINALIZING`
row, and one matching pending `FINALIZE` intent. An independent read-only
recheck is available:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  inspect-post-crash --identity "$TEST_ID"
```

Do not move or delete either half and do not edit, copy over, or replace the
catalog. The interrupted shape is the evidence needed by production startup.

## Phase 3: production startup recovery and verification

Start the installed service explicitly as a separate owner-visible action:

```sh
sudo systemctl start dashcamd.service
```

Then, while it is active and running:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  verify-recovered --identity "$TEST_ID" \
  | tee /tmp/dashcam-finalization-recovered.json
```

The verifier waits at most 30 seconds and proves the production startup path
promoted the pending JSON, left both target hashes exact, removed both pending
members, transitioned the row to `FINALIZED` with `pair_reconciled=true`, and
removed the durable intent.

## Retention

No phase auto-cleans any synthetic or production artifact. The recovered pair
is an ordinary unprotected finalized catalog pair and is intentionally left for
normal production retention. Do not use `rm`, manual moves, SQL, or catalog
replacement to remove it. If an explicit operator deletion is later needed,
implement and review a normal catalog API path that first commits a durable
`DELETE` intent and reconciles both members; this harness deliberately does not
provide such a path.

## Separate case-insensitive collision check

The collision workflow is independent of the interruption UUID above. It
places one uppercase spelling of the JSON target only after `dashcamd` has
already selected and opened the caller-specified current-boot pending MP4.
Placing it before service start would be invalid evidence: the production
sequence allocator correctly scans managed names case-insensitively and would
skip that sequence instead of exercising the finalizer.

Start `dashcamd` explicitly, observe its current nonempty
`pending/boot-<current-boot-short-id>-<sequence>.partial.mp4`, and choose a
fresh UUIDv4 collision token. Sequences `000000` through `000010` are refused.
While the service is active and the selected MP4 is still open, arm the
sentinel:

```sh
COLLISION_ID=<second-fresh-canonical-lowercase-UUIDv4>
SEQUENCE=<six-digit-current-open-sequence>
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  prepare-collision --identity "$COLLISION_ID" --sequence "$SEQUENCE" \
  | tee /tmp/dashcam-finalization-collision-armed.json
```

Preparation proves the exact pending MP4 is a nonempty regular file on the
recording mount, all other related exact names are absent, and no catalog row
or pending intent refers to the token or any related path. It exclusively
creates `clips/BOOT-<ID>-<SEQUENCE>.JSON`, reads its canonical payload back,
and proves the active MP4 device/inode did not change while arming it.

Explicitly stop the service so it attempts to finalize that fragment:

```sh
sudo systemctl stop dashcamd.service
```

With the service fully inactive, inspect the refused collision:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  inspect-collision --identity "$COLLISION_ID" --sequence "$SEQUENCE" \
  | tee /tmp/dashcam-finalization-collision-refused.json
```

The inspector requires the uppercase sentinel's exact canonical hash to be
unchanged, the expected pending MP4 to remain nonempty, the pending sidecar and
both exact lowercase targets to be absent, and no related catalog row or
pending intent. The check deliberately reasons about exact directory-entry
spelling because exFAT lookup is case-insensitive.

Only after saving the refusal evidence, remove the helper sentinel with the
closed cleanup phase:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  cleanup-collision-sentinel --identity "$COLLISION_ID" --sequence "$SEQUENCE" \
  | tee /tmp/dashcam-finalization-collision-sentinel-removed.json
```

Cleanup requires `dashcamd` inactive, re-proves the complete refused shape,
re-reads the exact sentinel identity and hash, removes only that one sentinel,
flushes its directory, and proves it absent. It never removes, moves, or
finalizes the retained pending MP4 and never performs partial-pair cleanup.

## Read-only ordinary finalized-pair verification

With `dashcamd` inactive, any one unsynced current implementation pair can be
verified repeatably by its exact 12-character short boot ID and sequence:

```sh
sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  verify-recorded --boot-id <12-lowercase-hex> --sequence <0-999999> \
  | tee /tmp/dashcam-finalization-recorded-pair.json
```

This phase is read-only. It requires both pending members absent; exact
lowercase target MP4 and canonical JSON names; sidecar filename, boot,
sequence, and UUID identity; the exact `FINALIZED`, managed,
`pair_reconciled=true` catalog row; and no related pending intent. It
stream-hashes the bounded MP4 and reports both member sizes and SHA-256 values.
It intentionally does not claim `ffprobe`, decode, IDR, duration, or media
content validation; run those acceptance checks separately against the
reported exact MP4.
