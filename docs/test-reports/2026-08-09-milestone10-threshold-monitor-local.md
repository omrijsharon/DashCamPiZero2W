# Milestone 10 local threshold-monitor groundwork — 2026-08-09

## Scope and result

Source commit `75ce2b8` implements a local, fail-closed free-space observer.
It is partial Milestone 10 groundwork, not a Pi qualification, release, or
authorization to reclaim media.

The catalog migration advances to schema 5 and persists one singleton
`RetentionThresholdLatch` bound to the verified volume UUID and capacity. The
runtime initializes the monitor and obtains a fresh valid sample before pending
catalog reconciliation or camera/backend opening. Sampling obtains available
space as `f_bavail * f_frsize` and capacity as `f_blocks * f_frsize` through
one descriptor rooted on the verified recording mount. Identity, capacity, and
latch faults fail closed immediately; repeated observation failures stop only
after the bounded observation budget is exhausted.

The monitor persists low/high hysteresis before publishing it. Exact boundaries
are intentional: free space equal to the low threshold remains normal; reclaim
starts strictly below low; a reclaim latch persists until free space is at least
high; free space equal to emergency remains reclaiming; emergency starts
strictly below emergency. A classified no-space write forces emergency even if
the preceding free-space sample was higher.

The runtime exposes only an advisory reclamation directive and reports
`reclaimer_enabled=false`. It neither enumerates clips nor deletes, unlinks, or
permits protected-media deletion. GStreamer `ResourceError.NO_SPACE_LEFT` is
classified structurally (not by message text); `OSError` `ENOSPC` and `EDQUOT`
are also handled. These paths persist the emergency latch and end in
`STORAGE_SAFETY_STOP`, producing final `FAULTED/STORAGE_FAULT` status while
returning a clean process outcome so systemd `Restart=on-failure` cannot loop
the camera. Runtime snapshot schema 3 includes the bounded monitor status.

## Local validation

The completed source slice passed:

- Focused threshold/storage/recorder tests: `459 passed`.
- Full test suite: `2033 passed, 10 skipped`.
- Ruff, scoped strict mypy across 79 files, and `git diff --check`.
- Independent safety review, including targeted no-space paths and scoped
  mypy, approved the source commit.

Unit coverage includes boundary equality and durable hysteresis, latch-binding
drift, sample-age/public UUID privacy, bounded observation failure, no deletion
authority, startup emergency before reconciliation/camera access, active and
pre-first-fragment no-space handling, structured GStreamer no-space
classification, and daemon clean-stop status.

## Residual gates

No durable `DELETING` reclaimer exists yet. The current storage preflight still
refuses `free <= minimum_free_gib`, which can preempt a restart in much of the
low/emergency range before the new monitor can direct recovery. Those contracts
must be designed together with protected/leased clip coordination and
idempotent two-member cleanup.

No exact-Pi threshold, stale-sampling, or actual ENOSPC validation has run.
Any future live test must use a disposable, bounded exFAT fixture volume. It
must not fill, delete, or otherwise mutate active recordings without separate
explicit authorization. The source tree also inherits the rejected Milestone 9
resource state, so it must not be represented as an accepted/deployable image.
