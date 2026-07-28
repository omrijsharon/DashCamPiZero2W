# Milestone 8 UTC and filename reconciliation

Date: 2026-07-28  
Reference boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Accepted release: `0.1.0.dev0-921164f96ad53e0b`

## Scope and outcome

This slice adds bounded, durable reconciliation of provisional clip metadata
and filenames after a trusted GPS UTC anchor becomes available. It passed:

- canonical UTC and `Asia/Jerusalem` projection from monotonic clip/sample
  times;
- stable clip UUIDs and idempotent replay;
- durable `RECONCILE_NAME` intent with replacement-sidecar payload and expected
  source-sidecar SHA-256;
- device-bound, no-replace MP4/JSON promotion on the exact exFAT volume;
- startup recovery of two previously interrupted/refused intents;
- same-boot late-lock processing through a bounded 64-entry backlog, with at
  most two reconciliations per fragment;
- case-insensitive collision refusal before any source, catalog, or intent
  mutation;
- continued 1080p30 hardware-H.264 plus AAC recording with zero media restart.

Together with the UART, no-GPS, anchor, sidecar, and clock-step reports, this
closes Milestone 8. Overlay rendering and its shared-snapshot performance gates
remain Milestone 9 work.

## Implementation

Catalog schema 4 stores the canonical replacement sidecar and the expected hash
of its provisional source. `ClipMetadataCoordinator` validates stable identity,
boot ownership, source contents, destination collisions, and canonical
readback. The filesystem layer performs bounded durable atomic replacement of
the sidecar and no-replace moves for each pair member. Startup reconciliation
replays both `FINALIZE` and `RECONCILE_NAME` intents.

The recorder queues a finalized clip when no anchor exists. The queue is
bounded to 64 UUIDs. Once a trusted anchor exists, each fragment finalization
attempts at most two oldest entries, allowing the backlog to catch up by one
clip per ordinary segment. An optional reconciliation failure is counted and
rotated to the tail; it cannot stop or backpressure recording.

Canonical civil timestamps are independently serialized to millisecond
precision. Their duration-consistency tolerance is therefore one millisecond,
while monotonic media timing remains nanosecond-valued and authoritative.

## Refused first release and recovery

Release `0.1.0.dev0-2dc9ffa1aeef63e3` safely refused name reconciliation for
sequences 393 and 394 with
`INVALID_NAME_RECONCILIATION_SIDECAR`. Video continued, the provisional files
were unchanged, the intents remained durable, and service/pipeline restart
counts stayed zero.

The cause was a one-microsecond duration-consistency tolerance applied after
the independently serialized start/end civil timestamps had each been rounded
to milliseconds. The accepted fix uses the schema's actual one-millisecond
precision. Release `0.1.0.dev0-e15cf80bca2fc2fb` replayed both intents on
startup and preserved their original UUIDs:

- sequence 393: `fc5da822-41e7-449c-a432-266e705c663a`;
- sequence 394: `a123bdc3-04ae-447f-9dbe-754b76c837cc`.

Fresh sequence 395 then reconciled as
`20260728T175106.947Z_601693e3fa96_s000395` with UUID
`202ceb72-5709-4f31-b8b4-e6c8ead8484d`. Its sidecar contained 423 ordered,
unique GPS samples, all projected as `GPS_ANCHORED`, and retained
`Asia/Jerusalem` local time. Full `h264_v4l2m2m` video and AAC decode passed.
The privacy-safe result is
`artifacts/pi-m8-20260728/m8-reconcile-result-69.json`, SHA-256
`2701be57c22a13fcefb4b0ec5aca99ab7d85a2969852d1017dd63e99979fb716`.

## Final hash-closed release

Release `0.1.0.dev0-6f943f3a4edf7117` adds the bounded same-boot backlog.

- bundle manifest SHA-256:
  `5292caa14fbd631db92362f6f2d3cd9431af097fa9de55acebab2291ae926c3f`;
- final `SHA256SUMS` SHA-256:
  `b746cc7add48c33b7b83328a812ae1bdf7eb8acdf2c107c78b50824ca8796d5a`;
- wheel SHA-256:
  `64b1b11685b750ccda0d5fcf578d2027c561f5fefa7a132d6c9c300602b1ac78`.

Exact-version plan/apply and a second idempotent plan/apply passed. Evidence
hashes:

- apply:
  `600a15a3c5673d67418cbe0c9922a5b394503060c7261a34f8203e4c4d4f94b2`;
- idempotent plan:
  `13200007263372b4625aab8b3f4372854e8424b75911ac60c3362127d20d545f`;
- idempotent apply:
  `dbefca119de8f6ede8af86ea774ad8bda308127eb4099fffefdb6812fd16345d`.

The superseded `e15cf80bca2fc2fb` release was removed only after proving it was
neither current nor the rollback target.

## Exact-Pi late-lock result

The service started with access to the exact `/dev/serial0 -> /dev/ttyAMA0`
character device temporarily set to mode `000`. A transient systemd timer
provided an automatic bounded restoration to the exact original `0660` mode.
The microphone remained connected.

The recorder reached `RECORDING` with:

- GPS `UART_UNAVAILABLE`, permission denied, no accepted anchor;
- audio `MATCHED`;
- verified writable exFAT `DASHCAM`;
- zero dropped frames and zero pipeline restarts.

Sequence 398 finalized under provisional
`clips/boot-601693e3fa96-000398.{mp4,json}` with UUID
`2f983f7b-9a7f-4446-87b5-980d6f6e9a50`. Its backlog count became one.
After UART access was restored, the receiver accepted one RMC anchor. At the
next fragment boundary the bounded drain reconciled both sequences 398 and 399:

- backlog `0`, completed `2`, failures `0`, overflows `0`;
- both durable intents `COMPLETE`, with no pending intent;
- both source pairs absent and target pairs present;
- both catalog UUIDs unchanged;
- audio remained available;
- no drops, pipeline restart, service restart, or throttle flag.

The final names were:

- `20260728T180419.643Z_601693e3fa96_s000398`;
- `20260728T180519.635Z_601693e3fa96_s000399`.

Both clips intentionally have no navigation samples because GPS was unavailable
through their recording windows. Reconciliation projects their clip-level UTC
and local timestamps from the later trusted anchor without inventing historical
coordinates or speed.

The recorder stopped cleanly with `Result=success`, `ExecMainStatus=0`,
`NRestarts=0`; UART mode was `0660` and throttling was `0x0`. Privacy-safe
result:

- `artifacts/pi-m8-20260728/m8-late-lock-result-71.json`, SHA-256
  `cb0177a9a19754fcf2c881df47f39db2790abf770032df8bff9388c8d3ca1b20`;
- active post-lock snapshot SHA-256
  `a00e386098e3ebe3e48d09bcfb0488c77e81acbdcabde039163a59823cc9c5d9`;
- validator SHA-256
  `b97e8688ad33bb0590fb284cf22af5215a7e746fb26af0b83670177ca8ab209e`.

No coordinate-bearing sidecar was copied to Windows.

## Media validation

Both reconciled clips independently passed full Raspberry Pi hardware video
decode with `h264_v4l2m2m` and AAC audio decode. All four decoder error logs
were empty.

- sequence 398: H.264 High 1920x1080, 30 fps, 8,000,863 bit/s,
  59.992667 seconds; AAC-LC mono 48 kHz, 128 kbit/s, 60.053333 seconds;
  ffprobe SHA-256
  `dfb1340090eaf5d56d8c0bb280987d18281e08ca93e86006fc02f8a08184eb0e`;
- sequence 399: H.264 High 1920x1080, 30 fps, 8,001,635 bit/s,
  59.025667 seconds; AAC-LC mono 48 kHz, 128 kbit/s, 59.008021 seconds;
  stream-edge skew about 16 ms; ffprobe SHA-256
  `9b54fce1b6e7795d753c587dff8f4ed2115b231b5ec175e34e86dee493cc46a9`.

## Exact-exFAT collision refusal

A disposable isolated recording root was created below
`/srv/dashcam/quarantine` on device `179:3`, filesystem `exfat`, label
`DASHCAM`. It used a separate temporary schema-4 SQLite catalog. A foreign
uppercase sidecar occupied the case-insensitive UTC target before
reconciliation.

The coordinator refused with `MetadataReconciliationRefused: refusing filename
collision`. The source MP4 and JSON hashes, foreign collision hash, stable UUID,
catalog paths, and zero-pending-intent state were unchanged. The production
catalog hash was identical before and after:
`de9d544fc4be73aa7c801da7067b3628447aeee60bb6afaa56c5c11e56aa8b22`.
The disposable directory and temporary catalog were then removed.

- privacy-safe result SHA-256:
  `0c4cea7c475b03127d5fe2208dd36ebe9ff25ec3e35f30ba1d45f6e07c343d1f`;
- validator SHA-256:
  `254c293b604b226b37cdd42334aa9d7cff234acd6afa7baf9efea5125d56d7bb`.

## Stale/lost navigation and GPS/time fault matrix

The installed production wheel was exercised on the exact Pi through a
privacy-safe, bounded target harness. It retained neither coordinates nor raw
NMEA. The accepted result is
`artifacts/pi-m8-20260728/m8-gps-fault-matrix-result-73.json`, SHA-256
`84b63cd5bef42e4056e04263abef945f2440734cf8ac4ac6261ec94310c08040`.

It proved:

- silence clears current navigation, reports `STALE`, and retains the existing
  UTC anchor only as `GPS_TIME_STALE`;
- transport loss clears navigation and reconnect restores
  `NAVIGATION_VALID` only after a new valid fix, with two connections, one
  disconnect, and one reconnect;
- malformed, checksum-failed, unsupported, and oversized lines remain bounded
  while a later valid fix is accepted;
- implausible UTC refuses with `IMPLAUSIBLE_UTC` and creates no anchor;
- a conflicting anchor refuses and leaves the original anchor unchanged;
- projection across UTC midnight rolls the date correctly; and
- `Asia/Jerusalem` resolves the tested summer and winter instants to UTC+03:00
  and UTC+02:00 respectively with the pinned tzdata wheel.

The already accepted production no-GPS and late-lock runs supply the physical
recorder/media halves of the matrix. No physical GPS unplug was required.

A later uncontested integrated run strengthened the production-wheel result.
Hash-closed harness manifest
`9ea3a712a3a4167fc63b1339ef072e6d3a1b121f380b33506923d08bb6fc7bc8`
ran the installed `0.1.0.dev0-921164f96ad53e0b` daemon in a transient,
non-restarting systemd unit against a PTY-backed source while using the real
camera, hardware encoder, exFAT volume, catalog, and reconciliation code.
Sequence 423 finalized provisional, reconciled under one durable complete
intent with its UUID unchanged, and retained truthful empty historical
navigation. Silence published `STALE` with no current navigation, a conflicting
anchor was refused without replacing the accepted anchor, and a replaced PTY
transport recovered through exactly two connections, one disconnect, and one
reconnect. Encoded frames advanced from 1 to 2,179 with zero drops and zero
pipeline or systemd restart. The ordinary recorder and network fallback stayed
inactive, all transient paths were removed, and throttling stayed `0x0`.

- privacy-safe integrated result:
  `artifacts/pi-m8-20260728/m8-fault-matrix-result-9ea3a712-passed.json`;
- result SHA-256:
  `a89df67c2c9735127bf360718738f9d1b09049bcb429034e25793b1f2be17ffd`.

Earlier overlapping integrated attempts remain refused diagnostics. The
checked harness now also holds a nonblocking kernel qualification lock before
any live mutation, so concurrent future runs refuse before touching the
transient recorder, PTY, or camera.

## Controlled no-GPS boot

The remaining literal boot case used the same final release and one reversible,
hash-gated configuration change. The physical receiver remained connected, but
the configured device was changed from `/dev/serial0` to the deliberately
absent `/dev/dashcam-gps-deliberately-absent`. The original configuration was
saved with SHA-256
`1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`;
the temporary configuration read back as
`62c626a6a1ceb6e2a3dd348d4ea089d354dbaa5706e0601e5ec98354f943eab4`.

After the controlled reboot, boot ID
`0c5464fe-25a1-4a76-973f-e73d38287e06` automatically started the enabled
ordinary recorder. It reached `RECORDING` with:

- GPS `UART_UNAVAILABLE`, `UNSYNCED`, disconnected, and no current navigation;
- nine bounded unavailable attempts with no accepted anchor or GPS sample;
- hardware-H.264 1920x1080/30 plus the connected AAC microphone;
- 2,106 encoded frames, zero dropped frames, and zero pipeline/systemd restart;
- verified writable exFAT `DASHCAM`, inactive AP fallback, and
  `throttled=0x0`.

The privacy-safe raw status is
`artifacts/pi-m8-20260728/m8-no-gps-boot-status-current.json`, SHA-256
`f29ede9e2ccba2c4aa64608d14206ec4c00b2706b6a34bb0f88f2d7534136fec`.
The recorder was then stopped, the original configuration was restored
byte-for-byte and revalidated by the first hash above, the temporary backup was
removed, and ordinary recording restarted with `NRestarts=0`. It later stopped
cleanly with `Result=success` and status 0 after final readback. This proves
boot-time optionality of GPS; it does not claim a physical-unplug test.

## Final hash-closed deployment

Release `0.1.0.dev0-921164f96ad53e0b` adds durable catalog enumeration for
reconciliation across process restarts, an independent bounded periodic worker,
three-attempt retry/terminal parking, serialized shutdown flushing, and both a
consecutive and one-second GPS parse-error-rate guard. The measured M10 stream,
including its unsupported sentence mix, remained below the rate guard.

- manifest SHA-256:
  `2421ce2595815814c6de91c0ae55f8c5ca4a9f5dc05871caafcad34be81264f6`;
- final `SHA256SUMS` SHA-256:
  `9e77ac1a7a71194b8b2864e73016e860b4c6b4d316bf44c2603846766df13eca`;
- application wheel SHA-256:
  `09714c688566ef45338e2ac20056d653afc71356250b81ce972b6ad9c219b0ea`.

After an explicit APT refresh, the authoritative plan/apply and independent
idempotent plan/apply all passed with zero package changes and no services
started. Their SHA-256 values were:

- plan: `c072e56828f4d054db4ebd5bf358cefac42731d97ebe2b924dd5de6d1881d916`;
- apply: `c685297907f894ba5be7de537517a9d53c6b8496a8e4e2cb2ff2eb09887c7782`;
- repeat plan:
  `a0ebbeea2e7e611db8416903b2c1ccbcd3d5dd949defedc8f88900ec23058704`;
- repeat apply:
  `dee6c63bfaba8446a87735c998cf197b538cab3a0fccee3eafe5c79af678279d`.

The ordinary recorder then ran for one full segment on the real GPS UART and
connected microphone. Sequence 418 is 59.988667 seconds of 1920x1080 High/4.1
H.264 at 30 fps and 8,000,994 bit/s plus 60.053333 seconds of AAC-LC mono
48 kHz at 128 kbit/s. Full Raspberry Pi `h264_v4l2m2m` video decode and AAC
decode produced empty error logs. The ffprobe evidence SHA-256 is
`ffc8f8d9d0ae9a6a2be96b3fbc71f988630eb2e22289dc66a09d68e82cad45d1`.
The service stopped with status 0, `NRestarts=0`, UART mode `0660`, and
`throttled=0x0`.

Final privacy-safe state readback found catalog schema 4 with 349 clips, 697
complete intents, zero pending/problem intents, exact managed configuration,
3,084,857,344 root bytes free, storage verification active/successful, and both
the recorder and AP fallback inactive. Result SHA-256:
`1c7e56f7b848e7994733b602a818332da318830a8e38e0f7420151500668e0e8`.

Transient integrated fault-harness runs used synthetic UTC anchors and
reconciled their own test clips in the shared catalog. Therefore their
filenames, including the post-run name of sequence 418, are explicitly not
physical-receiver UTC/filename acceptance evidence. Sequences 398/399 and
result 71 remain the authority for late-lock UTC/name reconciliation; the
final uncontested integrated result above is acceptance evidence for fault
isolation and media continuity.

## Local validation

The final source state passed:

- `uv run --frozen pytest -q -p no:cacheprovider`: `1845 passed, 10 skipped`;
- repository-wide Ruff;
- strict MyPy over all 73 source files.

Unit coverage includes late-anchor backward projection, stable UUID replay,
interruption before sidecar replacement, source-drift refusal,
case-insensitive collision refusal, bounded backlog drain/overflow, and
optional reconciliation failure that does not terminate recording.

## Milestone result

Milestone 8 is accepted. Metadata stays internally consistent across no-GPS
boot, unsynced
startup, late GPS lock, stale/lost navigation, reconnect, malformed data,
anchor-policy faults, civil-time boundaries, and Linux wall-clock steps without
terminating or restarting media capture. The exact normal configuration is
restored; the recorder and deferred AP fallback are inactive after acceptance.
