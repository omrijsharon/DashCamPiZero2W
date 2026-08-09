# Milestone 10 disposable retention-loop harness

This is a component-validation harness for the accepted exact Raspberry Pi. It
does **not** install the current checkout, start `dashcamd`, record camera data,
or mutate the active `/srv/dashcam` filesystem or production catalog. It runs
commit-source catalog, threshold, and reclaimer modules from a verified ZIP in
a private mount namespace. Within that namespace only, the cloned production
mount is removed and replaced by a new 480 MiB loop-backed exFAT fixture. A
separate 64 MiB loop-backed ext4 fixture holds all test catalogs.

The checked-in directory deliberately contains no generated archive or
manifest. `prepare-bundle.py` builds these outside the repository from one
clean exact `HEAD`:

```text
python deploy/ssh-dev-validation/milestone10-retention-loop/prepare-bundle.py \
  --repository <clean-repository> \
  --expected-commit <full-40-character-HEAD> \
  --output <new-directory-outside-the-repository>
```

The builder reads candidate source and harness bytes from the named commit,
creates a deterministic uncompressed `dashcam-source.zip`, records the exact
commit, tree, member sizes and SHA-256 hashes in canonical `SOURCE.json`, and
writes `SHA256SUMS`. The output is refused beneath the repository,
`/srv/dashcam`, or `/var/lib/dashcam`. Generated `SOURCE.json`, `SHA256SUMS`,
and `dashcam-source.zip` are review/deployment artifacts. Generated artifacts
must not be committed in this harness directory.

Review the five generated files, transfer only that directory to the Pi, and
record the SHA-256 of `SHA256SUMS`. Run it only while the accepted release is
dormant, using that release's exact interpreter:

```text
MANIFEST_SHA256=<reviewed-sha256-of-SHA256SUMS>
COMMIT=<reviewed-full-source-commit>
sudo /opt/dashcam/current/venv/bin/python -I run.py \
  --bundle "$PWD" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --expected-commit "$COMMIT" \
  --output /var/tmp/m10-retention-result.json
```

The parent refuses unless it is root on the declared exact Pi, the accepted
`5f95` release/config are installed, `dashcamd` is loaded and inactive, the
real exFAT source/UUID/label/sentinel match, NetworkManager and SSH remain
active, throttle is `0x0`, every required tool exists, and a global nonblocking
lock is held. It snapshots the real mount, sentinel, production catalog/WAL/SHM,
services, network, throttle, namespaces, and loop inventory before work and
requires byte-identical structured poststate. It never runs a mutating
`systemctl` or `nmcli` command.

Before creating the lock, frozen bundle, work directory, or either backing
image, the parent binds `/var/tmp` to the expected ext4 root device and observes
space with `f_bavail * f_frsize`. Checked arithmetic requires room for full
allocation of both images, 32 MiB bounded overhead, and at least 2 GiB of
preserved root free space. The same device/capacity identity and 2 GiB reserve
are required again after every cleanup path and before result publication.
Each image is fully allocated and fsynced before loop attachment, with exact
size and allocated-block coverage verified and the remaining budget rechecked
immediately before and after each allocation. The ext4 formatter uses its
documented `-E nodiscard` option, and both images must retain full allocated-
block coverage immediately after formatting and before mounting.

The worker makes `/` recursively private before unmounting the cloned
production mount. Every destructive filesystem command accepts only a numbered
loop block device whose sysfs backing file is the exact new image. Unmount,
detach, parent timeout recovery, and final removal repeat those identity checks.
The worker and parent both require the loop inventory to return to baseline.
Image size, filler bytes, files, archive members, command output, individual
commands, total worker time, reconciliation steps, privacy traversal, and
result size all have hard bounds.

The matrices cover:

- **A:** exact low/high/emergency equality, ENOSPC seam semantics, durable
  restart hysteresis, identity/capacity drift, and invalid-observation budget;
- **B:** three real exFAT low/high cycles, oldest-first pair deletion, one pair
  per fresh observation, and exact high-water stopping;
- **C:** protected, leased, unmanaged, pending-mutation, and FINALIZING
  exclusions plus survival of unknown/Windows-style files;
- **D:** durable previous-two/current/next-one event selection and protected
  pair-intent convergence;
- **E:** sixteen actual process-`SIGKILL` cells: FINALIZE, PROTECT, UNPROTECT,
  and DELETE are each killed after durable intent creation/before member one,
  after member one's real parent-directory fsync, after member two's real fsync
  but before catalog completion, and after committed completion. Every cell
  uses a fresh subprocess and ext4 catalog, then reopens, reconciles, repeats
  reconciliation idempotently, and verifies the exact exFAT pair/catalog end
  state. This is process-loss evidence, not physical-power-loss evidence;
- **F:** actual directory-fsync paths, unmount, read-only `fsck.exfat` and
  `e2fsck`, remount, and stable disposable identity;
- **G:** protected-full/no-eligible behavior with the reclaimer enabled and no
  protected mutation; and
- **H:** source import provenance, namespace/identity/privacy bounds, exact
  cleanup, and production pre/post equality.

Result schema 2 makes the complete 4-by-4 SIGKILL evidence mandatory. The
result always states `production_release_tested=false`,
`physical_power_loss_tested=false`, and `m10_exit_gate_closed=false`. This loop
evidence cannot close production daemon/camera behavior, structured GStreamer
no-space handling on the recording path, physical power interruption,
performance, or a deployable installed Milestone 10 release. Those remain
explicit deferred gates, even when every component matrix in this harness
passes. The complete worker, including all sixteen bounded crash subprocesses
and their recovery checks, remains under the parent's 900-second timeout and
exact cleanup barrier.

If a crash subprocess misses its closed contract, the worker reports only its
allow-listed operation and cutpoint, numeric return code, bounded captured
stdout/stderr byte counts and SHA-256 values, and a four-bit failure mask. The
mask order is return code, stdout bound, empty stderr, canonical intent UUID;
`1` identifies a failed predicate. Raw child output, paths, and intent IDs are
never forwarded.
