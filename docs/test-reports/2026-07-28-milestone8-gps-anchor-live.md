# Milestone 8 trusted GPS UTC anchor — exact Pi

Date: 2026-07-28  
Reference Pi: `00000000db28ffe4`  
Boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Release: `0.1.0.dev0-75947a15db03f4b3`  
Recording volume: verified exFAT `DASHCAM` at `/srv/dashcam`

## Scope

This result accepts the Milestone 8 trusted-GPS-anchor task. It covers
sentence-specific RMC/ZDA qualification, configured date plausibility,
continuity/conflict policy, explicit source/provenance, uncertainty, bounded
runtime status, and a production run with the actual open-sky receiver.

It does not accept system-clock discipline, GPS sample capture in clip
sidecars, provisional filename reconciliation, or the final GPS/time fault
matrix. The recorder still owns media timing through the pipeline/monotonic
clock. No location coordinates were retained in this evidence.

## Anchor contract

The production service constructs one `NmeaAnchorTracker` with these managed
defaults:

- plausible UTC interval: `2024-01-01T00:00:00Z` through
  `2100-01-01T00:00:00Z`
- initial uncertainty: 250 ms
- maximum ordinary-anchor conflict: 2,000 ms
- maximum reacquisition disagreement: 5,000 ms
- maximum anchor interval: 86,400 seconds
- GPS freshness limit: 2 seconds

Only checksum-valid, successfully parsed RMC or ZDA records are candidates.
RMC additionally requires the active-valid status and a complete date/time.
The tracker publishes an immutable monotonic/UTC pair with source,
provenance, and uncertainty; later observations must confirm continuity,
qualify as an allowed reacquisition, or be rejected explicitly. A transport
loss makes GPS time stale without discarding or silently replacing the last
accepted anchor.

Runtime status exposes only privacy-safe anchor/counter information. It does
not publish coordinates.

## Local validation

The anchor/configuration/UART/service/runtime/deployment group passed
221 tests with four Windows symlink-privilege skips. Ruff passed all selected
files, and strict MyPy passed the five changed source files. Tests cover RMC
and ZDA validity, bad checksums and malformed records, plausible-date bounds,
continuity, conflict, reacquisition, staleness, transport loss, idempotent
observations, configuration bounds, and production status wiring.

## Hash-closed deployment

The release bundle was built outside the working tree:

- manifest SHA-256:
  `34a8883f9562684b24bd74e4c3627e6701c0219c751206f7c7e681b6288b4979`
- `SHA256SUMS` SHA-256:
  `ccea76368dce13cba3b69e63291b4bdec9f0515d56b73678ce7fcbbef97f5e6b`
- application wheel SHA-256:
  `03b0e45bd59ca3df484ee0f31b1f6df4d56d3b445bde7ae039f512dbfb88af1f`
- locked tzdata wheel SHA-256:
  `dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931`

The exact-version plan/apply passed with no package change or service start.
The first repeat plan correctly refused because retaining an older,
non-current release made projected root headroom less than 2 GiB. After the
exact obsolete release was proven neither current nor rollback and removed,
a fresh dry-run and apply passed idempotently: all managed-file before/after
hashes were identical and root free space remained 2,698,170,368 bytes.

Ignored machine-readable evidence:

- `artifacts/pi-m8-20260728/dashcam-app-plan-65.json`
- `artifacts/pi-m8-20260728/dashcam-app-apply-65.json`
- `artifacts/pi-m8-20260728/dashcam-app-plan-65-idempotent-refused.json`
- `artifacts/pi-m8-20260728/dashcam-app-plan-65-idempotent.json`
- `artifacts/pi-m8-20260728/dashcam-app-apply-65-idempotent.json`

## Exact-Pi result

After 20 seconds of the ordinary production service:

- lifecycle was `RECORDING`, verified storage was `READY`, audio was
  `MATCHED`, and 1080p H.264 remained hardware encoded;
- GPS was connected and `NAVIGATION_VALID`, with eight satellites and HDOP
  2.87 in the privacy-safe navigation summary;
- the time state was `GPS_TIME_VALID`;
- one GN RMC anchor was accepted at monotonic time
  `211804435572809`, UTC `2026-07-28T16:14:01.900Z`, source
  `GPS_RMC_VALID`, provenance
  `NMEA:GNRMC:active-valid:complete-utc`, and uncertainty 250 ms;
- the following 199 candidates confirmed that anchor, with zero rejection,
  reacquisition, or idempotent duplicate;
- the last confirmation disagreement was 2.722 ms, well inside the configured
  2,000 ms continuity bound;
- GPS recorded 197,237 bytes and 3,410 lines with zero checksum, transport,
  reconnect, or stale-transition errors in this run;
- media recorded 602 raw and 604 encoded observations with zero drops and
  `pipeline_restart_count=0`.

The installed service then stopped cleanly with `Result=success`,
`ExecMainStatus=0`, `NRestarts=0`, and `throttled=0x0`. It remains inactive.
The accepted privacy-safe status is
`artifacts/pi-m8-20260728/m8-anchor-status-65.json`, SHA-256
`499ce4fb37fd40c93ec10da3ccd57052087185dbb17681faee722c24370293d3`.

## Current-release follow-up

The later sidecar release `0.1.0.dev0-7fd1e73debb731b6` was also exercised
for a fresh bounded production run on the same Pi. The service accepted one
GN RMC anchor, recorded 183 continuity confirmations, remained
`GPS_TIME_VALID`, and ended with a confirmed 11.561 ms disagreement. Recording
remained hardware-H.264 with `pipeline_restart_count=0`; the service stopped
cleanly with `Result=success`, `ExecMainStatus=0`, `NRestarts=0`, and
`throttled=0x0`.

That snapshot also recorded 17 transient anchor-policy rejections before the
last successful confirmation. The current bounded status retains the total
but not a reason histogram, and a later successful observation clears the
single `last_error` field, so the saved snapshot cannot attribute those
rejections. This is retained as a diagnostics/fault-matrix gap and is not
described as an error-free NMEA run. It does not invalidate the accepted
anchor: the tracker remained valid and bounded, recording was unaffected, and
the earlier exact release run had zero anchor rejections.

Fresh ignored evidence:

- `artifacts/pi-m8-20260728/m8-anchor-current-7fd.json`, SHA-256
  `99562867f74d710e9b4b539a317cc437cab1df17902a2a1110534a57857ac2db`
- `artifacts/pi-m8-20260728/m8-anchor-current-7fd-journal.log`, SHA-256
  `5db9b4baa2791f627bb5a05ec3d80a14cd2e019e4112e2749034e465cefb18c9`

## Conclusion

The production recorder now accepts and reports a source-qualified,
plausibility-checked, continuity-checked GPS UTC anchor from the exact receiver
without changing media timing or disturbing recording. This checks the trusted
GPS UTC anchor task only. System-clock ownership, per-clip GPS metadata,
sidecar/filename reconciliation, and the remaining fault matrix stay open.
