# Milestone 9 direct anchored finalization candidate — rejected (2026-08-09)

## Scope and source state

This report records the bounded exact-Pi screen of direct anchored
finalization. Source commit `864bbef` was pushed. The implementation writes a
canonical GPS-anchored pair through the ordinary durable `FINALIZE` path when
a trusted anchor already exists, while retaining provisional-to-canonical
late-lock recovery for clips that close before lock.

Local verification passed `2001` tests with `10` host skips, Ruff, and source
mypy. The candidate is functionally correct, but this report does not mark the
Milestone 9 resource gate, exit gate, or milestone complete.

## Candidate identity and installation

- Release: `0.1.0.dev0-b3d3a3e42919950b`
- Manifest SHA-256:
  `323443e1efb508728fa9557569c37aa376504932500a71af775cc4a05afebab7`
- `SHA256SUMS` SHA-256:
  `72f335bd3f495df7e547833e295ecd63545f787a298927286c4b37efc7b342c9`
- Wheel SHA-256:
  `21cbb0801699acf997a38f2487af2e557476dba7aa5c2b4ebda77706c04a9e5f`

Exact-version application and a separate idempotent plan/apply made zero
package changes and started no services.

## Direct-finalization proof

The Pi-local catalog proof established that sequences 65 and 66 were canonical
at their first `FINALIZE`, with `pair_reconciled=true`, exactly two complete
`FINALIZE` intents, and zero `RECONCILE_NAME` intents. The privacy-safe proof
SHA-256 is
`ed01e3383b08f5154ae1c6e0bc37af6e1bb26856de17a1ee66c533c560c38a8e`.
A separate privacy-safe `sidecar-proof.json` SHA-256 is
`959889eedc2c475bd73d8c2053ddf21622399f0f149077748a326a30f990f919`.
It established that retention orders 211 and 212 (sequences 65 and 66) are
each `GPS_ANCHORED`/`GPS_TIME_VALID`, use a GPS `time_anchor`, include civil
start/end UTC, and retain 438/422 samples respectively. It exports no
coordinates.

This proves removal of the redundant transaction for already anchored clips;
it does not replace the established late-lock recovery, collision-refusal,
stable-UUID, or durability contracts.

## Same-boot screen

The accepted `5f95` control and candidate screen were measured on the same
boot.

| Arm | Mean CPU | p95 CPU | Max CPU | Encoded | Sampled encoded fps | Drops | GPS sample state |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Accepted `5f95` control | 93.5873995% | 105.9875290% | 151.9813232% | 2,101 | 27.9967 | 4 | valid at start, then stale |
| Direct-anchor candidate screen 1 | 92.4680983% | 102.9846293% | 149.9806537% | 2,104 | 28.0389 | 2 | valid at start and end |

Both arms recorded zero service/pipeline restarts, renderer failures, sync
failures, and throttling. The control result and samples SHA-256 values are
`ac9a38522be16ef69827dc53918620bfbf042c295e9116ae98aed185fe1c7aba` and
`4ee108622ba21072294a9affd12d78cc84aa1bdc5f8991cb10ad7069b6606f3a`.
The candidate result and samples SHA-256 values are
`c6ab763214b9531885f37115b8d453de27f5472f3d86f4a02082c66e3a6a587d` and
`ee0875216980e8f3549a4cc0f76301433a0fb4abc1ab0f5e0354d0ba5e0c1bdc`.

The same-boot p95 reduction is 3.0028996 percentage points, clearing the
relative threshold. The candidate nevertheless fails the absolute screen:
p95 is above 98%, drops are nonzero, and the harness encoded delta divided by
its sampling-window duration is below the 29.9 fps screening floor. Candidate
screen 2 and the formal matrix were not run.

## Decision and rollback

Keep source commit `864bbef`: its direct anchored-finalization behavior is
functionally correct and preserves the required late-lock recovery path. The
candidate release is rejected for resource acceptance.

Exact rollback restored accepted `0.1.0.dev0-5f95dd806342ac9e`, unchanged
managed configuration SHA-256
`1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`,
inactive dashcam/AP units, successful result, `NRestarts=0`,
`ExecMainStatus=0`, and `throttled=0x0`. Root available space was
2,769,801,216 bytes. The candidate temporary and release directories were
safely removed. Its persistent bundle remains at
`/var/lib/dashcam/m9-direct-anchor-bundle-864bbef`.

Do not run candidate screen 2 or the formal matrix from this candidate. Do not
check any Milestone 9 resource, exit, or milestone gate.
