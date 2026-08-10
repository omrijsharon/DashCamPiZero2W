# Milestone 10 private production-runtime validation — 2026-08-10

## Result

Milestone 10 passed its remaining exact-Pi runtime gates. A hash-closed
production candidate ran through a transient, recorder-UID service with the real
camera, Raspberry Pi hardware H.264 encoder, production runtime, bounded Unix
listener, disposable loop-backed exFAT recording volume, and separate ext4
catalog. The accepted result is:

| Property | Accepted value |
|---|---|
| Pi result | `/var/tmp/m10-private-runtime-c8a01e317301.json` |
| SHA-256 | `3ed369ebd44d1370e094548613985b1c98da0a1911da5d98e5a062ff55046bad` |
| Size | 5,844 bytes |
| Mode/owner | `0600`, `root:root` |
| Schema/result | schema 1, `passed=true` |
| Wall-clock duration | 528.7 seconds, within the 900-second global bound |

The log contained the same canonical result bytes. The parent published the
result only after all transient units, mounts, loops, private runtime state, and
owned service exclusion state had converged and the exact host poststate gate
passed.

## Hash closure and isolation

| Role | Commit | Tree |
|---|---|---|
| Harness | `c8a01e3173010480c42250200a7ddbc32fcd5ee7` | `76aed0b3d1b2b976b85d92cd9de6bac9075f770b` |
| Production candidate | `3d027042c798c6dd7d92e3701689862f28ed2d9e` | `20181515e3817a03757e44a7b1a9443313cfaf93` |
| Rollback companion | `5268bd8e2f0dfe18d2a70ec142af45e8198b3f1a` | `2e2258a45ce939edf97a9ebc140f01aa8066c435` |

The transferred bundle's `SHA256SUMS` SHA-256 was
`6549b8e27eb6408e0c675bf19f030756892d5fcc3de468bc064dd3346f2606c9`.
Its candidate and rollback source archives contained 73 and 71 verified members
respectively. The builder, frozen verifier, canonical metadata checks, ZIP
metadata/CRC checks, and byte-for-byte Git provenance checks passed before the
live run.

The fixture used one fully allocated 436,207,616-byte exFAT image and one fully
allocated 50,331,648-byte ext4 image. Mounts were private and bind-mounted only
inside the transient units. The harness preserved at least 2 GiB of root space,
observed only 3,813,376 bytes of non-image overhead against its 64 MiB allowance,
and finished with 2,758,479,872 bytes free. Production mount/catalog paths were
read-only from the harness's perspective and their identities and bytes were
unchanged. Final host checks found only the baseline `/dev/loop0` swap backing,
the accepted production mount identity, `dashcamd` enabled but inactive/dead
with `NRestarts=0`, no loaded harness drop-in, and `throttled=0x0`.

The harness temporarily excluded ordinary recorder activation using a uniquely
owned runtime drop-in. It verified exact path/content, mode, ownership, loaded
`DropInPaths`, `RefuseManualStart=yes`, inactive state, and an absent condition
marker. On systemd 257 the `Conditions` projection was exactly
`[unprintable]`; the result records `conditions_property_parsed=false` and does
not treat that projection as condition evidence. Descriptor-bound cleanup and
daemon reload restored the exact prior unit state before evidence publication.

## Phase A — production recording, reclaim, event, and media

The production candidate reached `RECORDING` in 20,987,714,379 ns measured from
before the blocking transient-unit launch, below the 40-second gate. Startup
first converged the seeded pending `FINALIZE`, then deleted three eligible pairs;
zero deletion occurred before finalization. Seeded protected and leased pairs
were excluded.

During real recording, deletion progressed by seven pairs while the observed
active clip remained `WRITING`; neither that clip nor its members entered a
delete operation while active. A second reclaim pass preserved protected,
leased, active-current, and event-window members. The production listener was
reachable at mode `0660` under the intended service identities.

An actual listener event protected exactly previous two, the durable current
`WRITING` clip, and the next successor. The stable event-ID retry was
idempotent, the successor pair was preserved, the next protection intent
completed, and the entire window converged. This is the missing active-runtime
authority. The separate component harness remains the authority for a seeded
durable `FINALIZING` exclusion; the accepted result truthfully retains
`camera_generated_finalizing_overlap_tested=false`.

Three ordinary clips passed independent hardware decode and strict IDR-first
inspection:

| Clip | Duration | Packets | Packet rate | Adjacent normalized gap | Min PTS/DTS delta |
|---|---:|---:|---:|---:|---:|
| 1 | 59.988667 s | 1,800 | 30.005668 fps | n/a | 99/99 |
| 2 | 59.022333 s | 1,771 | 30.005591 fps | 0 ns | 99/99 |
| 3 | 59.022333 s | 1,771 | 30.005591 fps | 0 ns | 99/99 |

The runtime used `v4l2h264enc`, encoded 5,409 frames, and reported zero
encoder-input-PTS-gap drops, pipeline restarts, renderer contract mismatches,
mapping-limit rejections, synchronization failures, transformation failures,
and update rejections. The first finalized sidecar did not expose the later
drop counter, so its per-clip field remains truthfully unavailable rather than
being coerced to zero. Sidecar frame-observer counts also remain distinct from
packet counts. Catalog identities, canonical sidecar targets, media members,
and event targets were bound and checked. The unit exited with status 0,
`Result=success`, and `NRestarts=0`.

## Phase B — protected-only emergency behavior

A fresh fixture contained eight protected pairs and was filled below the
16 MiB emergency threshold. The production candidate entered
`STORAGE_SAFETY_STOP` before opening the camera or listener, created no delete
intent, preserved every protected member hash, exited with status 0, and left
`Result=success` and `NRestarts=0`. This proves explicit fail-closed behavior
without evidence destruction when no eligible candidate exists.

The production correction needed for this gate is intentionally narrow: after
a startup storage safety stop, the notify service publishes its terminal
`FAULTED/STORAGE_FAULT` status and sends one readiness notification before
clean teardown. It still never opens the camera or control listener. Focused
unit tests bind that clean systemd outcome.

## Phase C — bounded startup backlog

A fresh fixture began with 65 committed `DELETE` intents. The exact startup
budget completed the first 64 in order and left the 65th durably `DELETING`.
The candidate then entered the same clean storage safety stop before camera or
listener creation. The observer proved no detached work, and the unit exited
status 0 with `Result=success` and `NRestarts=0`. This establishes the bounded
startup behavior without allowing a restart to consume the final intent and
silently proceed to camera startup.

## Phase D — schema-5 rollback companion

On the stopped phase-A private catalog and volume, the separately hash-closed
rollback companion initialized an absent latch exactly once only after a fresh
post-recovery storage preflight. Quiesce was idempotent, the read-only
pre-camera guard admitted the exact quiescent schema-5 state, and the rollback
recorder then opened the real camera and hardware encoder without creating the
candidate control listener. It encoded 303 frames with zero drops, pipeline
restarts, or renderer failures and exited status 0 with `Result=success` and
`NRestarts=0`.

This validates a private rollback route. It does not authorize pointing either
candidate at the production catalog, installing a release, downgrading schema,
or erasing catalog state.

## Refusal and recovery history

Earlier iterations are not acceptance evidence. They refused on exact root
reserve, mount lifetime, transient reclaim progress, a nonzero drop observation,
notify safety-stop semantics, and transient-unit terminal-state projection.
Each refusal emitted only a closed reviewed function/line token or fixed class,
published no accepted result, and either completed its exact cleanup or used the
reviewed recovery journal before the next attempt. The fixes tightened lifetime,
privacy, cleanup, systemd, and timing contracts; no gate was waived. The final
accepted run is the only authority for the facts above.

## Milestone mapping and explicit nonclaims

Together with the prior disposable-retention, sixteen-cell process-`SIGKILL`,
and control-component reports, this result closes plan items 226 through 228
and the Milestone 10 exit gate:

- active `WRITING`, seeded durable `FINALIZING`, protected, and leased clips are
  excluded while real deletion progresses;
- protected-only emergency pressure stops cleanly before camera/listener and
  destroys no evidence;
- production recording and rollover retain strictly increasing packet PTS/DTS,
  zero adjacent normalized gaps, zero runtime drops/restarts, and a bounded
  20.99-second startup; and
- durable replay is covered at all member boundaries, bounded startup recovery
  is demonstrated, unknown files remain preserved, and all cleanup/poststate
  barriers pass.

The result explicitly does **not** claim physical power-loss qualification,
camera-generated overlap with a `FINALIZING` row, physical GPS or audio testing,
HTTP/UI behavior, or an M11 download byte data plane. The accepted installed
release remains the dormant `5f95` Milestone 9 release; the Milestone 9 resource
exit gate remains open. Milestone 10 completion does not waive, alter, or close
that independent gate.
