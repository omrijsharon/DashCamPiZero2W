# Milestone 6 exact-Pi recovery injection

This hash-closed harness proves one bounded production recorder recovery on the
exact Pi. It imports the installed release and runs the real
`RecorderDaemon`, `GStreamerRecorderRuntime`, `GStreamerBackend`, storage
preflight, finalizer, catalog, and `/srv/dashcam` exFAT volume as the `dashcam`
service account.

Only the first real backend is wrapped. After its first fragment opens and it
has encoded for at least five seconds and 150 frames, the wrapper raises exactly
one `RecoverablePipelineError`. Its inner run task is joined or cancelled, while
normal runtime cleanup still delegates to the real backend's EOS/NULL shutdown.
All replacement backends are unmodified. After `RECOVERED`, the harness waits
for another 150 replacement frames and requests a clean daemon stop.

The pass gate requires exactly one restart, ordered
`FAULTED`/`STARTING`/`RECORDING` status, READY exact storage, canonical and
reconciled old/replacement pairs, independent H.264 decode and IDR checks, zero
catalog intents, no generated pending members, and released process-local camera
ownership. It writes one new, exclusive, bounded JSON evidence file on rootfs.
If the daemon terminates before either live frame predicate, the harness refuses
immediately and retains the exact bounded daemon result or exception, notifier
events, wrapper state/counters, backend count, runtime snapshot, and camera
ownership in that failure evidence.

The harness never invokes service management, changes networking or AP state,
formats storage, changes configuration/identity, changes pipeline source or
flags, removes catalog/media members, or overwrites evidence. The real finalizer
does only its normal pair finalization work for the two clips created by this
run.

## Review and hash closure

Review this directory outside the Pi working tree, then verify:

```sh
sha256sum -c SHA256SUMS
sha256sum SHA256SUMS
```

Pass the exact lowercase hash from the second command as
`--expected-manifest-sha256`.

## External inactive-unit proof

The harness deliberately does not call `systemctl`. Immediately before the
run, an external controller must prove that `dashcamd.service` is inactive on
the current boot and place a root-owned, non-group/world-writable canonical JSON
file on rootfs. Its exact closed schema is:

```json
{"active_state":"inactive","boot_id":"00000000-0000-0000-0000-000000000000","main_pid":0,"observed_monotonic_ns":123,"schema_version":1,"sub_state":"dead","unit":"dashcamd.service"}
```

Use the real lowercase kernel boot UUID and a current `time.monotonic_ns()`
value from the Pi. The proof must be no more than 120 seconds old. Hash the
exact bytes and pass that digest as `--expected-inactive-proof-sha256`. The
external controller must also retain its read-only `systemctl show` observation
with the resulting evidence; do not start or stop the unit from this harness.

## Run

Use the exact installed release interpreter path, not the `current` symlink, and
a new rootfs output path. The harness binds that lexical executable path,
`sys.prefix`, and the imported package to the same release. The venv's `python`
file may itself be the normal symlink to the system interpreter:

```sh
PYTHON=/opt/dashcam/releases/<installed-release>/venv/bin/python
HARNESS=/path/to/reviewed/milestone6-recovery/run.py
MANIFEST_SHA256=<reviewed-SHA256SUMS-hash>
PROOF=/run/dashcam/m6-dashcamd-inactive.json
PROOF_SHA256=<exact-proof-hash>

sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --inactive-proof "$PROOF" \
  --expected-inactive-proof-sha256 "$PROOF_SHA256" \
  --output /var/tmp/dashcam-m6-recovery.json
```

Do not run this concurrently with systemd `dashcamd`, another media probe that
opens the camera, or a second copy of the harness. The microphone may remain
connected; the current Milestone 6 recorder path is intentionally video-only.
