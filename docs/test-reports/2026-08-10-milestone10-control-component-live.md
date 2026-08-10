# Milestone 10 control-component live validation — 2026-08-10

## Result

The hash-closed Milestone 10 disposable-loop harness passed matrices A through
H on the verified Raspberry Pi using commit-source control, catalog, finalizer,
retention, and filesystem code. The worker remained in its private mount
namespace with separate loop-backed exFAT recording and ext4 catalog fixtures.
It created a fresh harness-owned `/run` socket and did not install the candidate,
start the production recorder, open the camera, or mutate the production
catalog or active `/srv/dashcam` recording filesystem.

The accepted result is `/var/tmp/m10-retention-result-0992bf7.json`:

| Property | Accepted value |
|---|---|
| SHA-256 | `266931a84eb862bf5878750a2be42c9fcb642915dfc080c22c46a74a16e388ad` |
| Size | 11,554 bytes |
| Mode/owner | `0600`, `root:root` |
| Matrix result | A–H all `passed=true` |
| Root available after cleanup | 2,765,840,384 bytes |
| Host pre/post digest | exact match; bounded digest begins `428a0f7e` |

## Hash closure

The transferred bundle and imported candidate source were bound to:

| Artifact | Accepted identity |
|---|---|
| Git commit | `0992bf7385de2aa6ae0026e0a4a7e6427fa5d721` |
| Git tree | `e22c168a334e2c4e1772ff2929535fbdaeec5193` |
| Source archive SHA-256 | `17c294e859ad227e563472d7483b58ba42a5266e7d52ed301c99eda35c0eb636` |
| `SHA256SUMS` SHA-256 | `dec914900377e337b5fa2c6ff854606bc97785d88e16979ff71778d50ccfcc72` |

The parent retained the existing exact release/interpreter, source-member,
import-provenance, root-reserve, private-namespace, loop-backing identity,
privacy, timeout, and cleanup gates. The result was published only after the
worker completed and the parent proved exact host poststate.

## Control and lease evidence

Matrix H instantiated the commit-source `RecorderUnixServer`,
`BoundedConnectionHandler`, and `RecorderControlDispatcher` against the real
disposable ext4 catalog and exFAT clip pairs. This was actual AF_UNIX component
execution, not a source-shape simulation:

- the fresh socket was a Unix socket owned by the root harness worker, with
  mode `0660` and the actual `dashcam-api` group;
- the eight-client listener admission cap refused the excess connection and
  bounded drain removed the socket;
- raw protocol acquisition returned an opaque lease authority and no recording
  path;
- after confirmed acquisition, the harness actually sent `SIGKILL` to the
  abandoned client and proved that client loss did not release the lease;
- listener/dispatcher reconstruction preserved the lease and its exact release
  authority; a wrong authority was refused and a second release was
  idempotent;
- the global active-lease cap was 32, the harness-configured expiry was one
  second, and excess issuance was refused;
- an active lease excluded its clip from retention before expiry, exact
  same-boot expiry cleared it, and the clip then became eligible;
- a lease from the previous boot identity was cleared on reopen; and
- no lease identifier, response body, filesystem path, or clip member content
  was retained in the result.

This closes the bounded lease and abandoned-client expiry behavior in the
commit-source component scope. It does not exercise an HTTP client, stream
media, or establish the Milestone 11 download data plane.

## Protection and event evidence

Raw socket `PROTECT` and `UNPROTECT` commands drove the production dispatcher
and durable two-member pair operations. A leased manual-protection target
remained frozen without partial movement; exact-authority release allowed the
repair to converge. The pair then protected and unprotected with no pending
intent. The sixteen-cell process-`SIGKILL` result in
`docs/test-reports/2026-08-10-milestone10-sigkill-live.md` remains the authority
for interruption at every durable member boundary.

The event path exercised the real runtime control callback seam around a
durable `WRITING` current clip. It selected exactly two previous clips, one
current clip, and one next clip. One leased event target remained frozen until
exact expiry, after which finalizer lease recovery converged the pair. Repeating
the stable event ID after rollover with no active clip was idempotent, and all
event pair intents converged.

This is component callback evidence. It is not an installed production-runtime
or camera-active callback run, and the earlier matrix-D field
`production_active_clip_callback_tested=false` remains truthful.

## Safe refusal and correction history

Preceding diagnostic iterations are not accepted evidence. A cross-source
harness/API mismatch was removed rather than silently adapting the candidate.
The first control-component attempt then refused when the abandoned child did
not produce its fixed acquisition confirmation. A privacy-safe diagnostic
reported only a closed lifecycle state, canonical return code, bounded byte
counts and SHA-256 values, plus an independently reviewed function/line token;
it forwarded no response, authority, path, exception message, or traceback.

That evidence isolated a harness-only parser mismatch: production responses use
compact insertion-order JSON, while the harness had required sorted canonical
JSON. The dedicated response validator was corrected to match the production
wire encoding while refusing duplicate keys, alternate top-level order,
whitespace, non-ASCII, unknown fields, invalid key bounds, non-production value
types, and excess nesting. Strict source-manifest and sidecar parsing was not
weakened. The accepted result came only from the subsequent reviewed run.
Every refused attempt published no accepted result and passed the exact
disposable cleanup/poststate barriers.

## Explicit nonclaims and remaining gates

The result retains all of these false claims:

- `production_release_tested=false`;
- `production_daemon_tested=false`;
- `production_camera_tested=false`;
- `production_gstreamer_no_space_tested=false`;
- `production_control_listener_service_tested=false`;
- `physical_power_loss_tested=false`;
- `download_data_plane_tested=false`; and
- `m10_exit_gate_closed=false`.

The nested control evidence also retains
`production_listener_service_tested=false`, `production_runtime_tested=false`,
`production_camera_tested=false`, and `download_data_plane_tested=false`.
The unchanged matrix-D and matrix-G scope fields remain
`production_active_clip_callback_tested=false` and
`runtime_no_candidate_safety_stop_tested=false`.
Socket ownership proves the harness's root-owned component fixture with the
real group and mode; it does not prove an installed service UID or systemd
listener lifecycle.

This result closes only plan items 221–223: bounded leases/expiry,
previous-two/current/next-one event protection, and recoverable protect/
unprotect component behavior. Items 226–229 remain open: no active production
recording clip was present during fill/reclaim; the production protected-full
safety-stop path was not run; recording-gap and startup-delay behavior was not
qualified; and the Milestone 10 exit gate remains open. Milestone 10 itself is
not complete. The Milestone 11 web service and controlled download data-plane
item also remain open.

The accepted installed release remains the dormant `5f95` Milestone 9 release.
Source `0992bf7385de2aa6ae0026e0a4a7e6427fa5d721` was tested from its verified
archive but was not installed and is not a deployable production acceptance
candidate while the Milestone 9 resource gate and production listener/web
integration remain unresolved.
