# Operations and evidence collection draft

These commands are read-only, but they remain deferred until Pi access is
authorized. Do not collect unrestricted environment dumps or secret files.

## Service and journal evidence

```text
systemctl --no-pager --full status dashcamd.service dashcam-web.service dashcam-storage-check.service
systemctl show dashcamd.service -p ActiveState -p SubState -p Result -p NRestarts -p WatchdogUSec
journalctl --no-pager --utc --since "30 minutes ago" -u dashcamd.service -u dashcam-web.service -u dashcam-storage-check.service
```

Review bundles before sharing. Logs must already redact AP/session secrets,
authorization values, cookies, query tokens, and unrestricted paths.
Bound every journal query by both time and output size in the future collection
tool. Treat card/device serials and stable IDs as sensitive diagnostic data:
retain only when needed and redact them before sharing outside the trusted team.

## Storage evidence

```text
findmnt --json --target /srv/dashcam
lsblk --json --bytes --output NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,SERIAL,RO
df --block-size=1 --output=source,fstype,size,used,avail,pcent,target /srv/dashcam
```

These commands do not authorize repair, mount, unmount, partition, or format
operations. Stop if identity differs from the accepted capability report.

## Version and health evidence

```text
uname -a
cat /etc/os-release
python --version
python scripts/capability_probe.py --output capability-report.json
python scripts/monitor_endurance.py --help
```

Retain UTC and monotonic ordering, build/config/catalog versions, storage
identity/free space, bounded journals, intent IDs, recorder state, and exact
action outcomes. Never include passwords, session keys, cookies, private keys,
or complete environment/config dumps.
