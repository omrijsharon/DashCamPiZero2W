# Controlled card-removal security and ordering contract

This is a design contract only. The helper entry point and authorization policy
do not exist, and `dashcam-prepare-removal.service` is intentionally not
installable.

1. The browser must pass authenticated-session, CSRF, explicit confirmation, and
   recent reauthentication checks.
2. The unprivileged web process may send only the versioned prepare request over
   the restricted recorder socket. It receives no systemd, polkit, mount, or
   shutdown privilege.
3. `dashcamd` owns the operation state. It atomically enters
   `PREPARING_REMOVAL`, rejects new downloads/events/settings work, expires or
   waits for bounded leases, requests a keyframe, finalizes with a deadline,
   persists/reconciles intents, flushes the catalog and recording filesystem,
   and stops every recorder-owned writer.
4. Only the `dashcam` service identity may ask polkit/D-Bus to start the exact
   root unit. The future policy must allow that one action and no arbitrary unit,
   command, argument, shell, mount, or sudo access.
5. The fixed-argument root helper accepts no browser/user path or device input.
   It re-verifies recorder readiness plus the configured mount identity, performs
   a bounded flush/unmount, and requests orderly shutdown. Any disagreement or
   timeout fails closed while preserving evidence.
6. Concurrent calls are idempotently rejected or join the existing operation.
   A web disconnect cannot cancel the safety sequence. The response says
   “shutdown in progress,” never “safe to remove” while Linux is still serving.

Tests before activation must cover reauth/CSRF failure, direct web-to-root denial,
recorder busy/fault states, concurrent requests, every timeout/failure boundary,
mount identity change, helper crash/restart, and proof that no writer remains
when unmount begins.
