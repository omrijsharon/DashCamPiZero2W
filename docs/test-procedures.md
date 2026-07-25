# Deferred recovery and interoperability procedures

This document defines test cases for local review. It does not authorize Pi
access, power interruption, filesystem damage, card removal, or Windows claims.

## Abrupt-power recovery matrix

Use only a later declared healthy reference card and controlled power-cut
fixture. Never pull the running Pi's system card by hand. Inject after:

1. active `.partial` media write;
2. finalization before video rename;
3. video rename before sidecar rename;
4. sidecar write before directory flush;
5. finalize/name reconciliation intent and each member move;
6. protect/unprotect intent and each member move;
7. delete intent and each member unlink;
8. catalog commit before/after the corresponding exFAT operation.

For each point, retain controller timing, boot/`fsck.exfat` results, bounded
recovery, catalog/intents, pair state, and independent media validation. Passing
the declared matrix is not an exFAT durability guarantee.

## Small-filesystem retention

Local tests use synthetic capacity/catalog fixtures only. Later target tests use
an expendable bounded filesystem, never the active system card:

1. Populate managed, protected, active/finalizing, leased, orphaned, and unknown
   Windows-created entries.
2. Cross emergency, low, exact-low, high-minus-one, and exact-high boundaries.
3. Verify oldest eligible selection, hysteresis, `DELETING` intent, and recovery
   after interruption at both unlinks.
4. Prove unknown, protected, active, finalizing, unreconciled, and actively
   leased clips are never selected.
5. Fill with protected evidence and require a critical stop, not evidence
   deletion or rootfs fallback.

## Windows 10/11 interoperability

Only after controlled shutdown and the physical safe-power cue:

1. Record Windows version, reader identity, visible partitions, and `DASHCAM`
   label/capacity.
2. Decline every prompt to initialize or format the ext4 partition.
3. Copy representative MP4/JSON pairs from `clips/` and `protected/`.
4. Verify portable names, matching stems, JSON Schema validity, pre/post-copy
   hashes, independent playback, seeking, duration, and audio.
5. Repeat on one supported Windows 10 and one supported Windows 11 system.

Windows-created `System Volume Information` and `$RECYCLE.BIN` must be ignored
and preserved by retention. Local mocks cannot satisfy these evidence tasks.

