# Installation, upgrade, and rollback draft

## Validation and authorization status

This is not a supported Pi installation procedure yet. Milestone 4 is complete,
including the camera/media, GPS, and USB-audio capability decisions. Every
physical target still requires a separate destructive gate. On 2026-07-24 the
owner authorized complete erase, reflash, repartition, and format of only the
31,457,280,000-byte card with CID
`fe34325344000000200000031a0192d1`.

The first CID-bound v1 image trial booted but failed safe without partition or
filesystem mutation: the 32-bit Zero 2 W loaded stock `initramfs7`, while v1
had customized the unused generic `initramfs`. The post-root service refused
the missing early-state evidence. V1 is retired and must not be reflashed.
A corrected armv7-targeted v2 regular image is now built and independently
re-read. Before the next physical write, shut the Pi down, move the card
offline, resolve the target again, and recheck all available identity
evidence. Evidence:
`docs/test-reports/2026-07-24-authorized-exact-card-image.json`.

## Tested Phase 0B image

- Raspberry Pi OS Lite reference image date: 2026-06-18
- Distribution: Raspbian GNU/Linux 13.4 (`trixie`)
- Architecture: 32-bit `armhf`, running `armv7l`
- Kernel: `6.18.34+rpt-rpi-v7`, package `1:6.18.34-1+rpt1`
- Python: 3.13.5
- Camera: IMX219 through libcamera 0.7.1 and rpicam-apps 1.12.0
- GStreamer: 1.26.2 with libcamera, base, good, bad, and X/Pango plugins
- Selected encoder: V4L2 hardware H.264 `/dev/video11`
- Selected UART: `/dev/serial0` -> `/dev/ttyAMA0`; Bluetooth disabled

The exact boot-file snapshots and measurements are recorded in
`docs/test-reports/2026-07-24-milestone4-progress.md`. This is evidence for the
connected reference Pi only, not permission to deploy.

## Release layout

```text
/opt/dashcam/releases/<build-id>/   application wheel, locked environment, docs
/opt/dashcam/current -> releases/<build-id>
/etc/dashcam/config.toml            non-secret validated configuration
/etc/dashcam/secrets/               root-owned write-only application secrets
/var/lib/dashcam/                   ext4 catalog, intents, migrations, fault state
/srv/dashcam/                       verified exFAT DASHCAM mount only
```

Never put configuration, catalog state, or secrets on exFAT. Never use an
unmounted `/srv/dashcam` directory as a recording fallback.

## Pre-install evidence sequence

After the later authorization, an operator will run these from the checked-out
release and preserve the JSON output:

```text
python scripts/capability_probe.py --output capability-report.json
python scripts/collect_storage_observation.py /dev/<verified-device> --output /absolute/new/device-observation.json
python scripts/verify_layout.py --layout deploy/storage/layout-v1.toml --observation device-observation.json
python scripts/plan_provision.py --layout deploy/storage/layout-v1.toml --observation device-observation.json --expected-identity device-identity.json
python scripts/validate_media.py --help
python scripts/monitor_endurance.py --help
```

The storage collector is read-only and fail-closed: it invokes only bounded,
allow-listed inventory commands and emits the exact evidence schema consumed by
the verifier. A successful observation or dry run is not permission to execute
a provisioning plan. The current local provisioner emits only a dry-run plan
and refuses execution; physical writes remain behind the explicit image and
target authorization workflow.

Before installation is enabled, the target workflow must:

1. Verify image release, architecture, kernel, package/boot versions, device
   identities, and absence of stock whole-card expansion.
2. Back up the partition table before any mutation.
3. Create least-privilege service accounts/groups and secret storage.
4. Install the locked release without runtime internet dependency.
5. Generate the mount unit from the observed exFAT UUID and the AP profile from
   a probed interface plus unique secret; refuse every unresolved template token.
6. Run `systemd-analyze verify` on generated units before enablement.
7. Run storage preflight; failure leaves the recorder in `STORAGE_FAULT`.
8. Enable services only after their entry points and watchdog protocols pass.

The future release manager must expose this non-shell, dry-run-first command
contract; these commands are specifications and do not exist yet:

```text
dashcam-release install --bundle <signed-local-bundle> --build-id <id> --dry-run
dashcam-release upgrade --bundle <signed-local-bundle> --from <id> --to <id> --dry-run
dashcam-release rollback --from <id> --to <retained-id> --dry-run
dashcam-release collect-logs --since <bounded-UTC-time> --output <new-directory>
```

Each mutating command must print the exact validated action plan in dry-run mode,
require a separate explicit execution confirmation, and re-check identities
immediately before mutation. `collect-logs` must redact and bound its output.
Every output path must resolve below an explicitly selected evidence directory
and be created exclusively; existing files/directories, symlinks, special
filesystems, and traversal are refusals. Signed-bundle verification and the
trusted key source remain unresolved until the release-security design is
approved; the placeholder must not be represented as implemented.

## Upgrade contract

An upgrade is non-destructive to partition 3:

1. Record active build/config/catalog versions, root free space, mount identity,
   and service state.
2. Install into a new build-ID directory without changing `current`.
3. Validate migrations on copies and retain backups.
4. Stop writers with bounded timeouts, atomically switch `current`, reload units,
   and start the recorder.
5. Require storage preflight and health success before declaring completion.
6. Never run `mkfs`, recreate partition 3, replace its UUID, or erase clips.

## Rollback contract

Keep the previous release and pre-migration state until the health gate passes.
Rollback restores the prior release pointer and only restores state whose schema
compatibility is proved. If backward migration is unavailable, stop; do not guess
or delete state.

## Recovery and removal

- Missing, wrong, dirty-unrepairable, or read-only data volumes are preserved and
  reported, never automatically formatted.
- Pending repair is bounded and idempotent; unrecoverable artifacts follow the
  implemented quarantine policy.
- Card removal uses the authenticated prepare-removal workflow: stop downloads,
  finalize with a deadline, flush, stop writers, unmount, and shut down. Wait for
  the documented physical power cue.
- If stock first boot already expanded rootfs, reflash the validated custom image.
  Do not shrink a mounted expanded root filesystem.

Read-only diagnostics are in [`operations.md`](operations.md); fault-injection
and interoperability cases are in [`test-procedures.md`](test-procedures.md).
