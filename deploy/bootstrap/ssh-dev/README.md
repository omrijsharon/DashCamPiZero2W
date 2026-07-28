# SSH-development bootstrap payload

This is a deliberately small first transfer for a stock Raspberry Pi OS Lite
root filesystem. It installs only the audited storage bootstrap, exact-card
contract, and cmdline armer. It does not install packages, services, network
configuration, or the full application; it does not arm or execute storage
provisioning.

The only authorized card is CID `fe34325344000000200000031a0192d1`, size
`31457280000`, with the stock p1/p2 source geometry in
`authorized-exact-card-ssh-dev-v1.json`. The target policy is a 6 GiB root,
at least 28 GiB media, at least 8 GiB data, and 1 MiB alignment/reserve.

## Prepare, transfer, and install

On the development computer, create a clean local transfer directory (this
does not contact the Pi):

```sh
python deploy/bootstrap/ssh-dev/prepare-payload.py /absolute/path/to/DashCamPiZero2W /absolute/path/to/ssh-dev-payload
scp -r /absolute/path/to/ssh-dev-payload dashcamadmin@192.168.68.107:/tmp/
```

On the Pi, inspect the manifest and install it. This is the intentionally
live-target-only step and requires root:

```sh
cd /tmp/ssh-dev-payload
sha256sum -c SHA256SUMS
sudo sh ./install.sh /tmp/ssh-dev-payload
```

## Arm only after review

First perform a non-mutating check of the real boot cmdline. The JSON output
contains only hashes, token counts, and readiness; it never prints cmdline
contents.

```sh
cat /proc/sys/kernel/random/boot_id  # record this pre-reboot boot ID
sudo /opt/dashcam-bootstrap/arm-cmdline.py --dry-run
```

Proceed only if that JSON says `"ready":true`. Copy its `before_sha256`, then
explicitly arm and verify the boot marker:

```sh
sudo /opt/dashcam-bootstrap/arm-cmdline.py --apply --expected-before-sha256 <before_sha256>
sudo /opt/dashcam-bootstrap/arm-cmdline.py --verify
sudo reboot
```

The armer creates an exclusive, durable pre-arm backup under
`/boot/firmware/dashcam-bootstrap`, then atomically writes exactly one
`dashcam.bootstrap=ssh-dev-v1` token. It never reboots itself. Reconnect with
the pinned project host-key file, confirm a new boot ID, and require a second
`"ready":true` marker verification before any storage preflight:

```sh
ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=artifacts/pi-ssh-known-hosts \
  dashcamadmin@192.168.68.107 'cat /proc/sys/kernel/random/boot_id; sudo /opt/dashcam-bootstrap/arm-cmdline.py --verify'
```

Record the old and new boot IDs, require `"ready":true` from the reconnect
command, then run the Stage-A preflight separately through the same pinned SSH
host key:

```sh
ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=artifacts/pi-ssh-known-hosts \
  dashcamadmin@192.168.68.107 'sudo /usr/bin/python3 /opt/dashcam-bootstrap/bootstrap.py --stage a --contract /etc/dashcam/bootstrap-v1-authorization.json --dry-run'
```

Review that Stage-A dry-run evidence. The following is the separate
non-dry-run command, shown for review only; do **not** execute it until the
owner explicitly authorizes the destructive exact-card transaction:

```sh
# DO NOT RUN YET
sudo /usr/bin/python3 /opt/dashcam-bootstrap/bootstrap.py --stage a \
  --contract /etc/dashcam/bootstrap-v1-authorization.json
```
