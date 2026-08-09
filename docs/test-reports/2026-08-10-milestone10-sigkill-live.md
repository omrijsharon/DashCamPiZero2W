# Milestone 10 every-step SIGKILL validation — 2026-08-10

## Result

The hash-closed Milestone 10 disposable-loop harness passed matrices A through
H on the verified exact `.112` Raspberry Pi. The accepted run exercised source
commit `ec226bf2fc0a01159f4e729ee2cc375e442693fb` from a private mount namespace
against separate disposable loop-backed exFAT recording and ext4 catalog
filesystems. It did not install that source, start the production recorder,
open the camera, or mutate the production recording volume or catalog.

The accepted privacy-safe result was written as root-owned mode `0600` data:

| Property | Accepted value |
|---|---|
| Bundle-manifest SHA-256 | `fda594df0af51984ba2560c2970393812b1170436ecbcebdcf9cc2a176cd4229` |
| Source commit | `ec226bf2fc0a01159f4e729ee2cc375e442693fb` |
| Result SHA-256 | `313b5c6c1059657ec1e96cedef1cf5652df9013570e41f50eb505d1ce74827cc` |
| Result size | 8,066 bytes |
| Result owner/mode | `root:root`, `0600` |

All A--H matrix results were true. The result truthfully retains
`production_release_tested=false`, `production_daemon_tested=false`,
`production_camera_tested=false`, `physical_power_loss_tested=false`, and
`m10_exit_gate_closed=false`.

## Every-step process-loss matrix

Matrix E passed all sixteen fresh-subprocess cells. Each operation used a fresh
catalog and was terminated by actual `SIGKILL` at every defined boundary:

| Operation | After durable intent | After member 1 | After member 2/before completion | After completion |
|---|---:|---:|---:|---:|
| `FINALIZE` | 2 | 1 | 0 | 0 |
| `PROTECT` | 2 | 1 | 0 | 0 |
| `UNPROTECT` | 2 | 1 | 0 | 0 |
| `DELETE` | 2 | 1 | 0 | 0 |

The values are recovery actions required after reopening. Each child satisfied
the exact negative-`SIGKILL` return, canonical intent-UUID stdout, and empty
stderr contract. The parent reopened the fresh ext4 catalog, reconciled the
exact durable intent against exFAT, ran a second idempotent reconciliation, and
verified the final catalog lifecycle/protection/path state and both pair
members. The after-completion kill occurred while the catalog connection was
still open, immediately after the completion transaction was validated.

This closes the process-interruption coverage in Milestone 10 plan item 225.
It is process-loss evidence with clean filesystem and remount checks. It is not
physical-power-loss evidence and does not simulate loss of controller caches or
power during an exFAT metadata write.

## Other matrix and filesystem evidence

- Matrix B completed three repeated reclamation cycles with deletion counts
  `[3,3,3]`.
- Matrices A--H all reported `passed=true`.
- Directory-fsync paths, read-only filesystem checks, unmount/remount identity,
  exact loop backing, and full backing allocation checks passed.
- Matrix E used sixteen fresh catalogs and sixteen actual killed subprocesses;
  no preseeded state was substituted for these interruption cells.

## Refused development iterations

Several earlier attempts correctly refused because of harness defects rather
than product failures. The bounded diagnostics isolated an argument separator
error, a source-archive API mismatch, and ext4 backing allocation loss caused
by formatter/mount initialization behavior. The accepted source uses explicit
no-discard formatting and mounting, eager ext4 inode-table/journal
initialization, and unchanged post-format, post-mount, and child allocation
gates. Every refused attempt produced no accepted result and completed exact
disposable-loop/work cleanup. No refused attempt is counted as evidence.

## Final poststate

The result's privacy-safe host pre/post digest was identical and is recorded in
abbreviated form as `428a0f...d449`. After cleanup:

- result-recorded root available space was 2,752,532,480 bytes, above the
  preserved 2 GiB minimum;
- the final independent observation was 2,752,524,288 bytes;
- no owned loop devices, backing images, or harness work directories remained;
- `dashcamd.service` and `dashcam-network-fallback.service` remained enabled,
  inactive/dead, and at `NRestarts=0`;
- `throttled=0x0`; and
- the production `/srv/dashcam` mount and its accepted identity remained
  intact.

## Remaining gates

This result does not prove a physical power interruption, installed or
deployable Milestone 10 release, production daemon/camera behavior, active-clip
exclusion, production protected-full safety-stop, absence of recording gaps,
or the Milestone 10 exit gate. Those checklist items remain open.
