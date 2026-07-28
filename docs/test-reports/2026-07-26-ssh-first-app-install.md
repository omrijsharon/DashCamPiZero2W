# SSH-first application install — 2026-07-26

## Scope and result

The reviewed SSH-development application bundle and complete working tree were
transferred to the exact authorized Pi/card, installed, repeated idempotently,
and validated without a microphone. The installer enabled only
`dashcam-storage-check.service` and started no service. After installation, the
implemented storage check was started manually and reached `READY`.

The recorder, web, and prepare-removal units remain absent. No camera, GPS, or
microphone functional claim is made by this report.

## Local bundle and transfer evidence

- Final release: `0.1.0.dev0-d2fbd2c78eb80583`
- Final bundle manifest SHA-256:
  `c52cb012c9bb5c471e6f8f6d74f7aafac3dcc0102fa0de6ac4be80673e466a85`
- Final bundle archive: 689,664 bytes, SHA-256
  `a343a473068c9306663e12a96690a991c8347da14c6b20f819bf1b26d3fa118f`
- Locked tzdata wheel: 348,168 bytes, SHA-256
  `dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931`
- Final working-tree archive: 2,832,384 bytes, 297 files, SHA-256
  `ca70e5b8d1b45e372e289374104fade5908e379131d61c670fceda87f18afeb3`
- The working-tree member scan found no absolute/traversal path, symlink,
  `.git`, `.venv`, `artifacts`, cache, bytecode, or build-output member.
- Pi staging directory:
  `/home/dashcamadmin/dashcam-stage-20260726-004`
- Pi archive readback and every bundle `SHA256SUMS` entry passed. The
  working-tree and bundle installer/README copies were byte-identical.
- Focused local validation after the live observer corrections: 25 passed,
  four Windows symlink-privilege skips; Ruff and strict mypy passed.

## Safety corrections found before or during dry-run

No rejected bundle was applied.

1. A real Windows build exposed short low-level reads and text-mode `0x1a`
   handling. Bundle reads/writes now use binary mode, loop to bounded EOF, and
   compare copied size/hash to the source. The malformed short-wheel bundle was
   never transferred.
2. Independent review found that a `0700` release and root:root `0640` config
   would block the unprivileged service. Releases are now normalized
   root:root/traversable, config is root:`dashcam-storage` `0640`, and apply
   proves access as `dashcam` before enabling the unit.
3. APT apply is bound to one saved dry-run: only missing direct
   `package=version` entries are passed with `--no-upgrade`; solver versions,
   package state, storage state, service state, and conservative peak bytes
   must be identical immediately before mutation.
4. Trixie's simulation does not accept `--simulate` with `--no-upgrade` and
   omits the older human size prose. The dry-run now requires zero
   upgrades/removals, parses every exact `Inst` solver entry, and derives a
   conservative peak from exact `apt-cache show` `Size` plus
   `Installed-Size`.
5. The first live dry-run refused the stock exact
   `/etc/os-release -> ../usr/lib/os-release` layout. The gate now accepts only
   that reviewed symlink and safely reads the root-owned regular target.
   Preserved refusal SHA-256:
   `006feef9dc67f0cd3ffc8600ff29eb1b9443ce7f7e3231c672c258e6752d1aed`.
6. The next live dry-run refused the sysfs CID file because its nominal size is
   4,096 bytes although its content is 33 bytes. A dedicated exact-path,
   no-follow, 128-byte-bounded pseudo-file reader now handles this without
   weakening ordinary file reads. Preserved refusal SHA-256:
   `6dc7bd0a9fb8096b9bba50f2451fafcb6a4a89f21064fdecd2c723a011e9b380`.

## APT refresh and authoritative plan

The first valid plan was preserved but not reused after apply failed before
`dpkg`: stale package indexes produced four HTTP 404 responses and one selected
mirror timed out. `dpkg --audit` was empty and `/opt/dashcam`, config, unit, and
install marker were all absent.

- Pre-refresh plan SHA-256:
  `22211634ef3f25f8bd80426e79f9501e391937ae8b111b5771c9369f5a8edd30`
- Refused apply JSON SHA-256:
  `7f96c7501478bdcc9cf5a361ed336ecb330b2871a20e4035d2a395f1ce7e2fcb`
- Download diagnostic stdout/stderr SHA-256:
  `303ce0ec84196a00275512ae294cffef732e3594c90d89c533ec090aa5482705` /
  `3465ccb38df3e5005a9a8d539c1916de59ecf5cd56383fa02dd9e4b8c9944b3d`
- Explicit pre-plan `apt-get update` passed; stdout/stderr SHA-256:
  `30a1ad9cd2dee87f32e9c5fdc9388e78df715a9cdda3b136710edc0a4c440f94` /
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- No APT refresh occurred between the replacement plan and apply.
- Authoritative plan SHA-256:
  `43110a17232de752ded2de22929a441383632e4842e340a2edc2a0d7a80977fc`
- Missing direct packages: 10; exact solver packages: 283
- Conservative download/installed/peak bytes:
  178,229,044 / 556,826,624 / 735,055,668
- Root free/required/projected bytes:
  4,140,539,904 / 3,956,281,140 / 2,331,742,412
- Planned services to enable/start:
  `dashcam-storage-check.service` / none

## Apply and idempotency

- First successful apply JSON SHA-256:
  `9ffdd896453ddbb20874e07b63434bd7042cb0fbd33d3ff63c73675cfa85d11e`
- The large LLVM dependency made the Zero 2 W apply take about 25 minutes;
  the bounded command remained responsive and did not time out.
- All 25 declared direct packages are installed with recorded versions in the
  Pi evidence JSON. Notable versions include ffmpeg
  `8:7.1.5-0+deb13u1+rpt1`, GStreamer
  `1.26.2-1+rpt3+deb13u1`/`deb13u2` variants, libcamera tools
  `0.7.1+rpt20260609-1`, Python `3.13.5-1`, and systemd
  `257.9-1~deb13u1+rpi1`.
- The second dry-run had zero missing/solver packages and observed the storage
  unit already enabled. Plan SHA-256:
  `5f12081bfa48539c76ca86a1e5f498a4d423e32576d1cf614364ae25363fbe43`.
- The second apply installed zero packages and started zero services. Apply and
  final `/var/lib/dashcam/app-install-v1.json` SHA-256:
  `10c3860a44def64ebdf6659dcc537ddb3e2306163487d15d3a34867dee8b0f65`.
- Release-tree aggregate content hash stayed
  `297427440002f2d988a7f20151496c9eb4a46d77f827e996a1f3e3a5207b688b`.
- Config/unit SHA-256 stayed
  `c5d20f05655235744d98c5275250558fd9978a30f74fa19eeef746a4ee780853` /
  `15b8a4e0f1313df72f5cb2a77455ea45a80a7f9597bf8989508d79709c55e5d6`.
- Installed ownership:
  release root:root `0755`, current symlink root:root, config
  root:`dashcam-storage` `0640`, unit root:root `0644`.
- `dashcam` successfully executed the venv Python and imported `dashcam` and
  the locked Python `tzdata`.

## Installed storage preflight

The manually started implemented unit ran as `dashcam:dashcam` with
supplementary group `dashcam-storage` and completed:

- `ActiveState=active`, `SubState=exited`, `Result=success`,
  `ExecMainStatus=0`
- State `READY`; writable probe attempted and succeeded
- exFAT capacity/free: 24,466,423,808 / 24,465,375,232 bytes
- exact `/dev/mmcblk0p3`, label `DASHCAM`, UUID `7EED-3EA7`
- validated options:
  `rw,nosuid,nodev,noexec,noatime,uid=999,gid=984,fmask=0137,dmask=0027,iocharset=utf8,errors=remount-ro`
- final root free: 3,622,191,104 bytes
- `dashcamd.service`, `dashcam-web.service`, and
  `dashcam-prepare-removal.service`: `not-found`

The exact Pi boot ID remained
`a8b7c6c7-bc09-4d8a-866b-7f554780984e`. No reboot was required.
