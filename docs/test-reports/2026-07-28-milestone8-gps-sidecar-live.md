# Milestone 8 bounded GPS sidecars — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Accepted release: `0.1.0.dev0-7fd1e73debb731b6`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope

This result accepts the Milestone 8 task to generate one bounded, versioned,
atomic JSON sidecar per finalized clip. It adds production GPS navigation
samples to the already proven canonical sidecar/finalization path.

It does not accept UTC projection into sidecars, system-clock discipline,
provisional filename reconciliation, or the final GPS/time fault matrix.
Until durable reconciliation runs, sidecars and their GPS samples remain
truthfully `MONOTONIC_ONLY`, with null UTC fields and provisional boot/sequence
filenames.

## Bounded telemetry model

The GPS service owns one in-memory history with these constraints:

- receiver-provided navigation is retained at no more than the configured
  10 Hz;
- alternating RMC/GGA records with the same receiver UTC sample epoch are
  coalesced while preserving the first monotonic receive timestamp, so a later
  complementary record cannot move a sample across a clip boundary;
- RMC motion and GGA fix-detail fields complement one another only while their
  own observations remain inside the configured freshness limit;
- invalid RMC/GGA immediately clears that sentence's complementary fields;
- three minutes, at most 1,800 samples, are retained for asynchronous
  finalization;
- clip queries use half-open `[start_monotonic_ns, end_monotonic_ns)` windows
  and return at most 600 samples;
- eviction or per-window truncation is explicit, never silently hidden;
- telemetry collection/snapshot failure produces a bounded sidecar warning and
  cannot terminate or backpressure recording.

The recorder converts the immutable window to the version-1 `GpsSample`
schema. While provisional, each sample keeps its actual monotonic timestamp,
null UTC, and `MONOTONIC_ONLY` quality. Later reconciliation can project those
same samples through the accepted stable anchor without changing clip UUID.

## Local validation

The focused GPS/config/metadata/reconciliation/runtime group passed 158 tests.
Ruff passed all selected files and strict MyPy passed all seven selected source
files. Coverage includes source-epoch coalescing under host jitter, configured
rate limiting, stale and invalid complementary fields, monotonic/source-time
regression, bounds, eviction, truncation, half-open windows, GPS snapshot
failure isolation, canonical sidecar parsing, and an exact end-boundary
rejection.

The final repository-wide run passed 1,796 tests with ten documented
Windows/POSIX capability skips, and repository-wide Ruff passed. Full-tree
MyPy still reports three unrelated existing test-double/protocol errors in
`test_recorder_finalizer.py` and `test_pi_m7_force_key_harness.py`; none is in
the changed source or the selected strict-MyPy set.

## Rejected preliminary cadence

Release `0.1.0.dev0-ce80ef8015a42304` safely finalized sequences 386/387 with
ordered, half-open, monotonic-only GPS samples and healthy A/V media. However,
the full 59.989-second recorder interval retained only 469 samples because
strict host-receive spacing discarded valid receiver epochs when scheduler
jitter made adjacent records slightly less than 100 ms apart. This did not
meet the native-cadence goal and was not accepted.

The privacy-safe preliminary result is
`artifacts/pi-m8-20260728/m8-telemetry-result-66.json`, SHA-256
`47cc019c9ca4fbc004221b8da870d6248a0a19e4be300da72757e4b2aad6f687`.
Its raw Windows sidecar copies were deleted after canonical validation and
privacy-safe summarization; the production sidecars remain on `DASHCAM`.

## Hash-closed accepted deployment

The accepted bundle was built outside the working tree:

- manifest SHA-256:
  `1810f695464dd1ec22bb958b30315ce782babb9d8f606c062f421a5ffc7bf3a0`
- `SHA256SUMS` SHA-256:
  `e4cca0a5c40b6e028cd49916a765573a4b451a4dd89cb5a8388265f90338ecfc`
- application wheel SHA-256:
  `536c3e0f342cbe3eb864706e09f9f6a8f5c2ad08c687b25a4c46b721fac3af9d`

APT indexes were refreshed immediately before the authoritative dry-run.
Exact-version plan/apply passed with no package change or service start. The
first repeat plan correctly refused projected root headroom below 2 GiB. After
the just-superseded preliminary telemetry release was proven neither current
nor rollback and its exact 14,559,698-byte directory was removed, a fresh
dry-run/apply passed byte-idempotently. Final root free space is
2,698,096,640 bytes.

Ignored installer evidence is under `artifacts/pi-m8-20260728/` with the
`dashcam-app-{plan,apply}-67*.json` and `m8-apt-update-67.log` names.

## Exact-Pi result

The ordinary production service crossed a full one-minute boundary:

- status stayed `RECORDING`, GPS stayed `NAVIGATION_VALID`, GPS time stayed
  `GPS_TIME_VALID`, storage stayed `READY`, audio stayed `MATCHED`, and H.264
  stayed hardware encoded;
- 1,601 valid RMC/GGA navigation observations produced 801 samples: 800
  receiver-epoch coalesces, zero rate-limit loss, zero eviction, zero
  monotonic regression, and zero source-time regression;
- media counters reached 2,406 raw and 2,406 encoded observations with zero
  drops and `pipeline_restart_count=0`;
- runtime remained unthrottled and the service stopped with
  `Result=success`, `ExecMainStatus=0`, and `NRestarts=0`.

Sequence 388 covered monotonic interval
`213597082883345..213657071641634` and contained exactly 600 unique, ordered
GPS samples, all inside the half-open interval. Sequence 389 began at exactly
the prior end, contained 431 unique samples in its shutdown interval, and had
zero sample timestamp overlap with sequence 388. Both sidecars reported
`gps.available=true`, `audio.available=true`, null sample UTC, null
`first_fix_utc`, and `MONOTONIC_ONLY` sample quality. Both canonical payloads
parsed through the repository's strict sidecar reader.

Sequence 388 independently reported 60.032 seconds, 8,132,335 bit/s, H.264
High/4.1, and 48 kHz mono AAC-LC. Full `h264_v4l2m2m` video plus audio decode
passed with empty error output.

Privacy-safe accepted result:
`artifacts/pi-m8-20260728/m8-telemetry-result-67.json`, SHA-256
`a168d8574a0f28bca1111a44955de9c501e4b70d77b461f884c49733bbeab9d9`.
It retains counts, identities, intervals, raw sidecar hashes, and media facts
but no coordinates. Raw Windows sidecar copies were deleted after validation;
the actual product sidecars remain on the exFAT recording volume.

## Conclusion

The production recorder now writes bounded native-cadence GPS navigation into
one canonical, recoverable sidecar for every finalized clip without affecting
media capture. Half-open ownership prevents boundary duplication, and
monotonic-only samples preserve all information needed for later trusted-anchor
UTC and filename reconciliation. Those reconciliation and clock tasks remain
open.
