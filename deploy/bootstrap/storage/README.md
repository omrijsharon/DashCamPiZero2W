# DashCam Bootstrap v1 storage payload

These are normal post-root systemd units. They do not contain an initramfs
hook, `partprobe`, `blockdev --rereadpt`, or any other partition-table reread.

The image build installs both units and the Python package, removes exactly the
standalone stock `resize` token from `cmdline.txt`, adds
`dashcam.bootstrap=v1`, and preserves Raspberry Pi Imager `firstrun` tokens.
Stage A defers while those first-run tokens or services are active. The pinned
Trixie Imager entry uses `cloudinit-rpi`, so both units are also ordered after
`cloud-final.service`; runtime evidence remains authoritative and only the
exact terminal-success state `done` permits mutation. Missing cloud-init,
running, error, and unknown states are all non-mutating deferrals.

The checked authorization file is deliberately limited to the named
31,457,280,000-byte trial card. It is not a general-release authorization.
The image installs it as
`/etc/dashcam/bootstrap-v1-authorization.json`; the runtime refuses a missing
or changed closed contract.
Both services are independent of networking. A refusal is persisted in
`/var/lib/dashcam/provisioning/bootstrap-v1.json`; the units do not use
`FailureAction`, so NetworkManager and SSH remain available.

The units execute the installed package through
`/opt/dashcam/venv/bin/python`. Stage B is conditioned on the durable Stage A
journal, requires a different boot ID, and never treats a command's zero exit
alone as resize or format proof. Completion binds the exact card, raw-MBR
hashes, geometry, partition identifiers, filesystem UUIDs, verified mount, and
the canonical `.dashcam-volume` sentinel. The sentinel schema is the same
closed schema consumed by the recorder storage preflight.

Before Stage B advances to `CONFIGURED`, it atomically writes and fsyncs the
closed recorder handoff at `/etc/dashcam/storage-volume.env`, then verifies its
ownership as `root:dashcam-storage` and mode `0640`. The ASCII/LF-only handoff
binds schema/layout version, mount, exFAT UUID, card CID, source-MBR hash, exact
root/data geometry, and the checked policy's minimum data capacity. The
recorder never sources this file as shell text; its storage preflight parses the
closed keys and requires them to match the canonical sentinel and observed
mount. The ext4 completion marker remains the final durable write.

The checked built image must first expand p2 offline to exactly 4 GiB
(8,388,608 sectors at start 1,064,960), install its payload within that
filesystem, and prove the exact source table by readback. Stage A accepts only
that built-image source geometry and later extends p2 to 6 GiB.

The image builder has an additional fail-closed storage contract: the first
4 MiB at the future p3 start must be physically present in the flashed image
and all zero. Stage B reads exactly that bounded prefix and also requires both
`blkid` and `wipefs` to report no signature before it persists format intent.
An image truncated before p3, or a card whose old contents merely happen not
to have a recognized filesystem signature, is not accepted as blank. This
zero-prefix requirement must be represented and verified by the image
builder/readback workflow before any flash is authorized.
