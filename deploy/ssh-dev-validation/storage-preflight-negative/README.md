# Pi storage-preflight negative validation

This harness exercises the installed production
`dashcam.storage.preflight` entry point against six negative storage states:

- an unmounted `/srv/dashcam` directory (the rootfs-fallback hazard);
- a disposable ext4 filesystem instead of exFAT;
- a disposable exFAT filesystem with the wrong label;
- a disposable exFAT filesystem with the wrong expected UUID;
- a disposable exFAT filesystem with the wrong sentinel identity;
- a disposable exFAT filesystem mounted read-only.

It does **not** unmount, remount, format, write to, or run a probe against the
host's real `/srv/dashcam` mount. Every case runs in a fresh private mount
namespace. Each worker detaches the private namespace's cloned host mount
before either leaving the rootfs directory unmounted or mounting a newly
formatted, loop-backed sparse image. The parent namespace verifies that the host
mount snapshot and sentinel hash remain byte-for-byte unchanged after every
case.

The harness also checks after every refusal that:

- `NetworkManager.service` is active and `nmcli` answers;
- `ssh.service` is active;
- a bounded loopback connection receives an SSH protocol banner;
- the worker retained the host network namespace (only the mount namespace is
  unshared).

## Prerequisites

- Run on the declared Raspberry Pi OS Lite 32-bit Trixie target as root.
- Invoke it with the installed application interpreter:
  `/opt/dashcam/current/venv/bin/python`.
- `/srv/dashcam` must initially be the healthy, writable, distinct exFAT
  `DASHCAM` mount described by `/etc/dashcam/storage-volume.env`, on the
  authorized card CID `fe34325344000000200000031a0192d1` with UUID
  `7EED-3EA7` and source `/dev/mmcblk0p3`.
- Required executables:
  `/usr/bin/findmnt`, `/usr/bin/mount`, `/usr/bin/umount`,
  `/usr/bin/unshare`, `/usr/bin/systemctl`, `/usr/bin/nmcli`,
  `/usr/bin/sync`,
  `/usr/sbin/losetup`, `/usr/sbin/blkid`, `/usr/sbin/mkfs.exfat`, and
  `/usr/sbin/mkfs.ext4`.
- The kernel must permit a root process to create a private mount namespace
  and disposable loop devices.
- Keep the SSH session open. Running through SSH supplies additional
  end-to-end evidence that the controlling connection survived all cases.

## Execution

From a freshly transferred, hash-verified working tree:

```text
sudo /opt/dashcam/current/venv/bin/python \
  deploy/ssh-dev-validation/storage-preflight-negative/run.py \
  > storage-preflight-negative-v1.json
```

Exit status `0` means every production preflight invocation refused with the
expected stable reason, never attempted its write probe, the real recording
mount remained unchanged, and NetworkManager/SSH stayed usable. Any
prerequisite, namespace, loop-device, cleanup, mount-identity, service, or
result mismatch fails the harness and emits `ready=false`.

The sparse images live only in root-owned `0700` temporary directories under
`/var/tmp`. Before either `mkfs` command, the harness requires a `/dev/loopN`
block device whose sysfs `backing_file` resolves to that invocation's newly
created image. Workers explicitly detach their loop device; after each worker
has exited, the parent requires the global loop-device inventory to return to
its exact pre-case baseline within five seconds.
The parent also copies the already loaded harness into a root-owned `0500`
file below `/run` before launching workers, so an unprivileged working-tree
change cannot alter a later case.
