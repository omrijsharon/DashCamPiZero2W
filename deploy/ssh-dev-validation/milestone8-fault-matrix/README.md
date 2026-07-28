# Milestone 8 exact-Pi GPS fault-matrix harness

This hash-closed harness runs the installed production `dashcam.daemon` in a
temporary systemd unit while the ordinary `dashcamd.service` is exactly
inactive/dead. It uses a root-owned `/dev/dashcam-m8-fault-matrix` symlink to a
PTY slave and feeds bounded synthetic NMEA from the PTY master. The camera,
hardware encoder, audio path, storage preflight, finalizer, catalog, GPS Linux
transport, parser, anchor policy, telemetry, and reconciliation coordinator are
the production implementations. Parser-only or mocked media results are not
accepted.

The scenario proves:

- recording starts during PTY silence with `UNSYNCED` time and no current
  navigation;
- malformed and bad-checksum records are rejected, and a checksum-valid 1980
  date is rejected by anchor plausibility policy;
- a later valid RMC stream reaches navigation/time validity without a camera
  restart;
- a finalized provisional pair is renamed from monotonic time after the anchor,
  with the same UUID, canonical reconciled sidecar, complete durable
  `RECONCILE_NAME` intent, and an exact idempotent no-op replay;
- a conflicting but plausible RMC observation is rejected;
- silence beyond the configured two-second stale timeout publishes `STALE`,
  `GPS_TIME_STALE`, and no current navigation;
- closing the first PTY transport, atomically installing a new PTY generation,
  and resuming valid input produces a counted disconnect/reconnect and valid
  recovery while encoded frames continue, drops do not regress, and pipeline
  restarts remain zero;
- a case-variant target collision is refused without changing its source or
  collision members. This collision check uses a disposable, exact-exFAT
  fixture below `quarantine` and never touches the production catalog.

The synthetic feed uses the non-private zero coordinate. Raw NMEA, latitude,
longitude, and per-sample records are excluded from the rootfs result. Product
MP4/JSON pairs remain on `/srv/dashcam`; do not copy raw sidecars to Windows.
The result contains only privacy-safe state, counters, hashes, names, UUIDs,
and time-quality facts.

Safety and cleanup

- The harness refuses a wrong Pi model/serial, wrong installed release, wrong
  `/dev/mmcblk0p3` exFAT `DASHCAM` UUID, a throttled Pi, an active ordinary
  recorder, an active AP fallback service, or a competing clock owner.
- It never runs partition, format, mount, network, AP, package, or wall-clock
  mutation commands.
- It writes one config under `/run`, one transient unit under
  `/run/systemd/system`, one `/dev` PTY link, normal product media/catalog
  state, the isolated collision fixture, and the requested exclusive rootfs
  evidence file.
- `finally` stops the transient unit and removes its unit, config, runtime
  directory, PTY link, and PTY descriptors. The collision fixture is removed
  by exact member/path checks. Generated product media and durable production
  reconciliation evidence are preserved.
- Every poll, command, read, sentence count, directory scan, result, service
  start/stop, and the complete scenario have hard bounds.

Run only after installing a new hash-closed release containing the current
workspace interfaces. The current pre-harness exact-Pi release is deliberately
not assumed to be sufficient.

```sh
cd /path/to/milestone8-fault-matrix
MANIFEST_SHA256="$(sha256sum SHA256SUMS | cut -d' ' -f1)"
RELEASE="0.1.0.dev0-<installed-commit-id>"
sudo "/opt/dashcam/releases/$RELEASE/venv/bin/python" run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --expected-release "$RELEASE" \
  --expected-board-serial 00000000db28ffe4 \
  --expected-storage-uuid 7EED-3EA7 \
  --output /var/lib/dashcam/m8-fault-matrix-result.json
```

The output path must be a new file in an existing real rootfs directory.
`passed=true` is evidence only for that exact Pi/image/release. UTC-midnight and
`Asia/Jerusalem` DST transition logic remain deterministic local tests; this
hardware harness does not alter its trusted base date or the Linux wall clock
to repeat those pure conversion cases.
