# systemd unit drafts

These units define the intended process, ordering, watchdog, restart, timeout,
socket-permission, and privilege contracts. They are **not installable yet**:
several referenced entry points deliberately do not exist until their local
implementation milestones, and the mount template contains required replacement
tokens.

Before installation, provisioning must:

1. Create the dedicated `dashcam`, `dashcam-web`, `dashcam-api`, and
   `dashcam-storage` accounts/groups.
2. Resolve the exact exFAT UUID and numeric service/storage IDs into a generated
   `srv-dashcam.mount`; refuse unresolved tokens.
3. Validate units with the target image's `systemd-analyze verify`.
4. Confirm the effective sandbox still permits only the recorder's required
   camera, UART, audio, state, and verified recording-volume access.
5. Implement and test the recorder/web notify/watchdog protocols and the narrow
   prepare-removal helper before enabling their units.

The web account is intentionally absent from camera/audio/UART groups. The
prepare-removal unit has no `[Install]` section and must not be enabled at boot.

The storage-check unit is intentionally a `Wants`/`After` dependency rather than
`Requires`: the daemon must still start far enough to publish `STORAGE_FAULT`.
The recorder implementation remains the mandatory enforcement point and must not
open the camera or create media until it freshly verifies the distinct writable
exFAT mount, label/UUID, and sentinel. It must stop writes immediately if that
identity changes; target integration will decide whether a mount `BindsTo`
relationship improves safety without hiding fault status.

`PrivateDevices=no` is an explicit unvalidated exception for recorder camera,
encoder, UART/audio, and storage inspection and for read-only storage preflight.
Phase 0B must inventory the exact nodes and replace broad visibility with
`DevicePolicy`/`DeviceAllow` where the selected media stack permits it, or record
the measured reason it cannot.
