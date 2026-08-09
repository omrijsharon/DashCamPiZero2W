# Milestone 10 disposable retention-loop live validation — 2026-08-10

## Result

The hash-closed Milestone 10 component harness passed matrices A through H on
the verified Raspberry Pi at the declared `.112` address. It exercised source
from commit `7d8e60232a049e6a1fdd96def05b13a426959e43` in a private mount
namespace using a fully allocated, loop-backed disposable exFAT recording
filesystem and a separate loop-backed ext4 catalog filesystem. It did not
install that source, start the production recorder, open the camera, or mutate
the active `/srv/dashcam` recording volume or production catalog.

The accepted result is
`/var/tmp/m10-retention-result-7d8e602.json`:

| Property | Accepted value |
|---|---|
| SHA-256 | `a8406d991e31ddc2c7f498c7783dad38efc4c067248504ad0792642594dce6b1` |
| Size | 5,668 bytes |
| Mode/owner | `0600`, `root:root` |
| Pi identity | exact `.112` target, board serial `00000000db28ffe4` |
| Pi boot ID after controlled recovery | `7f51dbde-bee7-4536-9acf-c1164206705d` |

The result truthfully retains `production_release_tested=false`,
`production_daemon_tested=false`, `production_camera_tested=false`,
`production_gstreamer_no_space_tested=false`,
`physical_power_loss_tested=false`, and `m10_exit_gate_closed=false`.

## Hash closure

The transferred bundle and imported candidate source were bound to:

| Artifact | Accepted identity |
|---|---|
| Git commit | `7d8e60232a049e6a1fdd96def05b13a426959e43` |
| Git tree | `16884e2a376baca0060307665f358fd31f2fef9e` |
| Source archive SHA-256 | `479adcfae72c3d6e0d13e38f785c33b00647ebd233301e636f4de6e3b770d5b1` |
| `SHA256SUMS` SHA-256 | `b5ed6851d6288232097aa2392287dc884c2d1c5a11136f858c386cd7bd0e6195` |
| Harness `run.py` SHA-256 | `c35c70cbef578c783840d5ace18920400c04c7d6d3c6b6da7d5b6249f1895637` |

The parent accepted only the reviewed release interpreter and exact target,
verified every source member before import, and proved imported `dashcam.*`
module provenance from the verified archive. The disposable worker made its
mount namespace recursively private before replacing only its cloned
`/srv/dashcam` view. Loop-device operations were bound to exact backing-file
identity and all work remained under the bounded `/var/tmp` fixture.

## Matrix evidence

All eight matrices reported `passed=true`:

- **A — thresholds and observation safety:** exact low, high, and emergency
  equality semantics passed; classified no-space behavior, durable restart
  hysteresis, identity drift, capacity drift, invalid observations, and the
  bounded observation-failure budget were exercised.
- **B — repeated reclamation:** three fill/reclaim cycles each deleted exactly
  three pairs (`3/3/3`). Nine clips were selected in exact oldest-first order.
  Twelve fresh reclamation observations proved one pair per observation and an
  exact high-water stop. The bounded filler used 163,053,568 bytes across 11
  fully allocated extents.
- **C — exclusions and unknown files:** protected, actively leased, unmanaged,
  pending-mutation, and genuine `FINALIZING` pairs survived unchanged.
  Uncatalogued and Windows-style files survived byte-for-byte. The production
  runtime's in-memory active clip was not exercised.
- **D — event protection core:** catalog selection and pair intents converged
  for previous two, current one, and next one. This was catalog/component
  evidence only; `production_active_clip_callback_tested=false`.
- **E — delete replay:** the harness deliberately preseeded the persisted
  state after one member of a `DELETE`, closed and reopened the SQLite catalog,
  then replayed the pending intent to convergence. It did not kill a process:
  `sigkill_cutpoint_matrix_tested=false` and
  `physical_power_loss_tested=false`.
- **F — filesystem durability surface:** directory-fsync paths ran; exFAT and
  ext4 read-only checks both returned status 0; unmount, remount, and stable
  disposable filesystem identity passed.
- **G — protected/no-eligible behavior:** with the reclaimer enabled, no
  eligible candidate was selected and the protected pair remained unchanged
  during an emergency observation. Protected deletion stayed disabled. The
  integrated daemon safety-stop was not exercised:
  `runtime_no-candidate_safety_stop_tested=false`.
- **H — isolation and cleanup:** source provenance, private mount isolation,
  unchanged network namespace, exact loop identity, bounded fixture privacy,
  production pre/post equality, and cleanup barriers passed.

## Timeout incident and controlled recovery

An earlier invocation reached the live matrix work but exceeded its outer
timeout while the private worker was still writing the bounded filler. The
outer termination orphaned that private worker. This attempt is not accepted
evidence and produced none of the result cited above.

For controlled recovery, the validation operator temporarily disabled `dashcamd.service` and
`dashcam-network-fallback.service`, rebooted the exact Pi, and verified that
the orphaned namespace, loop devices, work directory, and backing images were
gone. Both units were restored to their intended enabled and inactive/dead
state before the final clean run. The production recording volume and catalog
were not mutated. The final accepted result was produced only by the subsequent
clean invocation with the optimized, fully allocated bounded filler path.

## Final poststate

After the accepted run:

- no harness loop devices, work directories, or backing images remained;
- `dashcamd.service` and `dashcam-network-fallback.service` were enabled and
  inactive/dead with `NRestarts=0`;
- the Pi reported `throttled=0x0`;
- `/srv/dashcam` still had exact accepted UUID `7EED-3EA7` and its production
  identity;
- root available space was 2,767,863,808 bytes; and
- the bounded privacy-safe host pre/post digest retained in the result begins
  `428a0f`, with exact pre/post equality.

The poststate was observed on boot
`7f51dbde-bee7-4536-9acf-c1164206705d`. No service, network, active recording,
production catalog, or production filesystem mutation is attributed to the
accepted run.

## Local validation

The final source and harness state passed:

- the full local suite: `2098 passed, 12 skipped`;
- Ruff; and
- strict mypy across 76 source files.

## Gates that remain open

This result closes only the threshold, durable local reclaimer/preflight,
oldest-first, and unknown-file checklist items recorded in `plan.md`.
Milestone 10 itself and its exit gate remain open. In particular, this evidence
does not establish:

- production download lease issuance/expiry through the web/download path;
- production active-clip event callback behavior or full previous-two/current/
  next-one integration;
- recoverable protect/unprotect moves under every interruption;
- an after-every-step SIGKILL matrix or physical power interruption;
- active production recording exclusion during fill/reclaim;
- production protected-full/no-candidate runtime safety-stop behavior;
- absence of camera recording gaps or bounded production startup latency; or
- a deployable, installed Milestone 10 production release.

The accepted installed release remains the `5f95` Milestone 9 candidate.
Milestone 9 resource qualification is still open, so this commit-source
component result must not be presented as an accepted deployment or as closure
of either milestone's exit gate.
