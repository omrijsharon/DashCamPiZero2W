#!/usr/bin/env python3
"""One-off recovery for the exact 2026-07-25 FSSIZE false refusal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

STATE = Path("/var/lib/dashcam/provisioning/bootstrap-v1.json")
AUDIT_DIR = Path("/var/lib/dashcam/provisioning/fssize-recovery-v1")
BOOTSTRAP = Path("/opt/dashcam-bootstrap/bootstrap.py")
CONTRACT = Path("/etc/dashcam/bootstrap-v1-authorization.json")
SOURCE_SHA = "fd067e46b2b889d4bf2f0a95bc5a4afa365779ce8136500a5e790c6e388f2c80"
BOOTSTRAP_SHA = "85b67435dd03a792ccdb342eb9c4a2857412079b807837bb616224e71cf9f87b"
CONTRACT_SHA = "7d8239d93cca2c665f9d92ea3f9e6aec20a67a70237d473e804617427ae6d867"
PREFIX_SHA = "8c01bea511d15baa18fdbecb8caf88af33f16811a4c7fb8da68a4ea26a22a058"
MBR_SHA = "04eeff87140367c3046fcab407ff87e695cd3cfcb70f897049e612dd6ad2dc0a"
BOOT_ID = "a8b7c6c7-bc09-4d8a-866b-7f554780984e"
CID = "fe34325344000000200000031a0192d1"
ROOT_UUID = "e9ef4083-101b-46b4-b87d-de84fe1169f8"
PARTUUIDS = ("4f2c9ea0-01", "4f2c9ea0-02", "4f2c9ea0-03")
TRIGGER = "dashcam.bootstrap=ssh-dev-v1"
WARNING = (
    "Could not find module named cc_netplan_nm_patch "
    "(searched ['cc_netplan_nm_patch', 'cloudinit.config.cc_netplan_nm_patch'])"
)
MAX = 256 * 1024

SOURCE = (
    b'{"cid":"fe34325344000000200000031a0192d1","committed_mbr_sha256":'
    b'"04eeff87140367c3046fcab407ff87e695cd3cfcb70f897049e612dd6ad2dc0a",'
    b'"data_partition":"/dev/mmcblk0p3","data_prefix_sha256":"8c01bea511d15baa'
    b'18fdbecb8caf88af33f16811a4c7fb8da68a4ea26a22a058","data_uuid":null,"disk":'
    b'"/dev/mmcblk0","phase":"refused","refusal_code":"target_layout_mismatch",'
    b'"refusal_message":"resize2fs returned but exact ext4 target size was not observed",'
    b'"root_partition":"/dev/mmcblk0p2","schema_version":2,"size_bytes":31457280000,'
    b'"source_mbr_sha256":"2487b1f9af5151759ad1ec762d077424736d38d01a68b72b8'
    b'd6d4e1634545c3a","stage_a_boot_id":"cf1614e7-35cb-42e1-9223-f2473dd80978",'
    b'"target":{"data":{"bootable":false,"number":3,"size_sectors":47790080,'
    b'"start_sector":13647872,"type_code":7},"root":{"bootable":false,"number":2,'
    b'"size_sectors":12582912,"start_sector":1064960,"type_code":131},'
    b'"sector_size":512,"total_sectors":61440000}}\n'
)
_source_object = json.loads(SOURCE)
_replacement_object = dict(_source_object)
_replacement_object.update(phase="table_committed", refusal_code=None, refusal_message=None)
REPLACEMENT = (
    json.dumps(_replacement_object, sort_keys=True, separators=(",", ":")) + "\n"
).encode()


class RecoveryError(RuntimeError):
    pass


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise RecoveryError(message)


def _read(path: Path) -> bytes:
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode), f"unsafe {path}")
    _need(info.st_size <= MAX and info.st_nlink == 1, f"unbounded {path}")
    return path.read_bytes()


def _bounded_text(path: Path, limit: int = 16 * 1024) -> str:
    with path.open("rb", buffering=0) as stream:
        value = stream.read(limit + 1)
    _need(len(value) <= limit, f"{path} exceeded its bound")
    return value.decode()


def _command(argv: tuple[str, ...], accepted: frozenset[int] = frozenset({0})) -> tuple[int, str]:
    allowed = {
        "/usr/bin/cloud-init",
        "/usr/bin/findmnt",
        "/usr/bin/lsblk",
        "/usr/sbin/blkid",
        "/usr/sbin/dumpe2fs",
        "/usr/sbin/wipefs",
    }
    _need(bool(argv) and argv[0] in allowed, "non-read-only command refused")
    result = subprocess.run(argv, text=True, capture_output=True, timeout=15, check=False)
    _need(result.returncode in accepted, f"{argv[0]} returned {result.returncode}")
    _need(len(result.stdout.encode()) <= MAX, "command output exceeded bound")
    return result.returncode, result.stdout


def _nodes() -> dict[str, dict[str, object]]:
    _, output = _command(
        (
            "/usr/bin/lsblk",
            "-J",
            "-b",
            "-o",
            "PATH,TYPE,PKNAME,SIZE,LOG-SEC,PARTN,FSTYPE,LABEL,UUID,PARTUUID,START",
            "/dev/mmcblk0",
        )
    )
    result: dict[str, dict[str, object]] = {}
    for disk in json.loads(output)["blockdevices"]:
        result[str(disk["path"])] = disk
        for child in disk.get("children", []):
            result[str(child["path"])] = child
    return result


def _parse_p3_blkid(output: str) -> None:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        _need(bool(separator) and key not in fields, "p3 blkid export is malformed")
        fields[key] = value
    _need(not ({"TYPE", "LABEL", "UUID"} & fields.keys()), "p3 has filesystem identity")
    expected = {
        "PART_ENTRY_SCHEME": "dos",
        "PART_ENTRY_UUID": PARTUUIDS[2],
        "PART_ENTRY_NUMBER": "3",
        "PART_ENTRY_OFFSET": "13647872",
        "PART_ENTRY_SIZE": "47790080",
    }
    _need(all(fields.get(key) == value for key, value in expected.items()), "p3 metadata differs")
    _need(int(fields.get("PART_ENTRY_TYPE", "-1"), 0) == 7, "p3 type differs")
    optional = {
        "DEVNAME": "/dev/mmcblk0p3",
        "PART_ENTRY_DISK": "179:0",
        "PTTYPE": "dos",
        "PTUUID": "4f2c9ea0",
    }
    _need(
        all(
            key in expected or key == "PART_ENTRY_TYPE" or optional.get(key) == value
            for key, value in fields.items()
        ),
        "p3 blkid export has unexpected fields",
    )


def _verify_live(state: bytes) -> None:
    _need(state in {SOURCE, REPLACEMENT}, "journal is not the exact source or replacement")
    _need(_sha(_read(BOOTSTRAP)) == BOOTSTRAP_SHA, "corrected bootstrap hash differs")
    _need(_sha(_read(CONTRACT)) == CONTRACT_SHA, "authorization contract hash differs")
    _need(
        _bounded_text(Path("/proc/sys/kernel/random/boot_id"), 128).strip() == BOOT_ID,
        "boot ID differs",
    )
    tokens = _bounded_text(Path("/proc/cmdline")).split()
    _need(tokens.count(TRIGGER) == 1 and "dashcam.bootstrap=v1" not in tokens, "trigger differs")
    _need(
        _bounded_text(Path("/sys/class/block/mmcblk0/device/cid"), 128).strip() == CID,
        "CID differs",
    )

    with Path("/dev/mmcblk0").open("rb", buffering=0) as stream:
        mbr = stream.read(512)
    _need(len(mbr) == 512 and _sha(mbr) == MBR_SHA, "MBR differs")
    _need(int.from_bytes(mbr[440:444], "little") == 0x4F2C9EA0, "disk ID differs")
    expected = ((0x0C, 16384, 1048576), (0x83, 1064960, 12582912), (7, 13647872, 47790080))
    actual = tuple(
        (
            mbr[446 + index * 16 + 4],
            int.from_bytes(mbr[446 + index * 16 + 8 : 458 + index * 16], "little"),
            int.from_bytes(mbr[446 + index * 16 + 12 : 462 + index * 16], "little"),
        )
        for index in range(3)
    )
    _need(actual == expected, "partition table differs")
    with Path("/dev/mmcblk0").open("rb", buffering=0) as stream:
        stream.seek(13647872 * 512)
        _need(_sha(stream.read(4 * 1024**2)) == PREFIX_SHA, "p3 prefix differs")

    nodes = _nodes()
    disk, boot, root, data = (
        nodes[path]
        for path in ("/dev/mmcblk0", "/dev/mmcblk0p1", "/dev/mmcblk0p2", "/dev/mmcblk0p3")
    )
    _need(
        disk.get("type") == "disk"
        and disk.get("size") == 31457280000
        and disk.get("log-sec") == 512,
        "disk size differs",
    )
    _need(
        boot.get("fstype") == "vfat"
        and boot.get("uuid") == "89F4-4546"
        and boot.get("partuuid") == PARTUUIDS[0]
        and boot.get("start") == 16384
        and boot.get("size") == 1048576 * 512,
        "boot ID differs",
    )
    _need(
        root.get("fstype") == "ext4"
        and root.get("uuid") == ROOT_UUID
        and root.get("partuuid") == PARTUUIDS[1]
        and root.get("start") == 1064960
        and root.get("size") == 12582912 * 512,
        "root identity differs",
    )
    _need(
        data.get("partuuid") == PARTUUIDS[2]
        and data.get("start") == 13647872
        and data.get("size") == 47790080 * 512
        and all(data.get(field) is None for field in ("fstype", "label", "uuid")),
        "p3 identity differs",
    )
    _, root_mount = _command(("/usr/bin/findmnt", "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET", "/"))
    mounted = json.loads(root_mount).get("filesystems")
    _need(
        mounted
        == [
            {
                "source": "/dev/mmcblk0p2",
                "fstype": "ext4",
                "uuid": ROOT_UUID,
                "target": "/",
            }
        ],
        "mounted root identity differs",
    )
    _, dump = _command(("/usr/sbin/dumpe2fs", "-h", "/dev/mmcblk0p2"))
    fields = {
        line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
        for line in dump.splitlines()
        if ":" in line
    }
    _need(fields.get("Filesystem magic number", "").lower() == "0xef53", "not ext4")
    _need(
        fields.get("Block count") == "1572864" and fields.get("Block size") == "4096",
        "ext4 geometry differs",
    )
    _rc, blkid = _command(
        ("/usr/sbin/blkid", "-p", "-o", "export", "/dev/mmcblk0p3"),
        frozenset({0, 2}),
    )
    _parse_p3_blkid(blkid)
    _, wipefs = _command(("/usr/sbin/wipefs", "--json", "/dev/mmcblk0p3"))
    _need(json.loads(wipefs) == {"signatures": []}, "p3 has a signature")
    for argv in (
        ("/usr/bin/findmnt", "-J", "-S", "/dev/mmcblk0p3"),
        ("/usr/bin/findmnt", "-J", "-M", "/srv/dashcam"),
    ):
        rc, output = _command(argv, frozenset({1}))
        _need(rc == 1 and not output.strip(), "forbidden storage mount exists")
    _need(
        not Path("/var/lib/dashcam/provisioning/layout-v1.complete.json").exists(),
        "complete exists",
    )
    _need(not Path("/srv/dashcam/.dashcam-volume").exists(), "sentinel exists")
    fstab = _read(Path("/etc/fstab")).decode()
    _need(
        all(
            "/srv/dashcam" not in line and "/dev/mmcblk0p3" not in line
            for line in fstab.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        "fstab contains p3 storage",
    )
    rc, cloud = _command(("/usr/bin/cloud-init", "status", "--format=json"), frozenset({2}))
    value = json.loads(cloud)
    _need(
        rc == 2
        and value.get("status") == "done"
        and value.get("extended_status") == "degraded done"
        and value.get("errors") == []
        and value.get("stage") is None
        and value.get("recoverable_errors") == {"WARNING": [WARNING]},
        "cloud-init state differs",
    )


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_dir(path.parent)


def _atomic(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{SOURCE_SHA}.tmp")
    if temporary.exists():
        _need(_read(temporary) == value, "recovery temporary differs")
    else:
        _exclusive(temporary, value)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _sync() -> None:
    if os.name == "nt":
        return
    sync = getattr(os, "sync", None)
    if sync is None:
        raise RecoveryError("POSIX sync is unavailable")
    sync()


def recover(
    *,
    apply: bool,
    state_path: Path = STATE,
    audit_dir: Path = AUDIT_DIR,
    verify: Callable[[bytes], None] = _verify_live,
    fault: Callable[[str], None] = lambda _point: None,
) -> dict[str, object]:
    current = _read(state_path)
    _need(current in {SOURCE, REPLACEMENT}, "journal is not the exact source or replacement")
    verify(current)
    if os.path.lexists(audit_dir):
        audit_info = audit_dir.lstat()
        _need(
            stat.S_ISDIR(audit_info.st_mode) and not stat.S_ISLNK(audit_info.st_mode),
            "audit directory is unsafe",
        )
    else:
        parent_info = audit_dir.parent.lstat()
        _need(
            stat.S_ISDIR(parent_info.st_mode) and not stat.S_ISLNK(parent_info.st_mode),
            "audit parent is unsafe",
        )
    archive = audit_dir / f"bootstrap-v1.refused-{SOURCE_SHA}.json"
    audit = audit_dir / f"recovery-{SOURCE_SHA}.json"
    prepared = (
        json.dumps(
            {
                "operation": "fssize-false-refusal-v1",
                "source_sha256": SOURCE_SHA,
                "status": "prepared",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    complete = prepared.replace(b'"prepared"', b'"complete"')
    if archive.exists():
        _need(_read(archive) == SOURCE, "exact archive differs")
    if current == REPLACEMENT:
        _need(archive.exists() and _read(archive) == SOURCE, "exact archive is absent")
        _need(audit.exists() and _read(audit) in {prepared, complete}, "audit differs")
    elif audit.exists():
        _need(
            archive.exists() and _read(archive) == SOURCE and _read(audit) == prepared,
            "partial audit differs",
        )
    if not apply:
        return {
            "operation": "dry-run",
            "ready": True,
            "state": "refused" if current == SOURCE else "restored",
        }
    if current == REPLACEMENT and _read(audit) == complete:
        return {"operation": "apply", "ready": True, "state": "already-restored"}
    if not audit_dir.exists():
        audit_dir.mkdir(mode=0o700, parents=False)
        _fsync_dir(audit_dir.parent)
    if not archive.exists():
        _exclusive(archive, SOURCE)
    fault("after_archive")
    if not audit.exists():
        _exclusive(audit, prepared)
        _sync()
    fault("after_prepared")
    if current == SOURCE:
        _atomic(state_path, REPLACEMENT)
        fault("after_state_replace")
        _sync()
        _need(_read(state_path) == REPLACEMENT, "state replacement readback differs")
    _atomic(audit, complete)
    _sync()
    return {"operation": "apply", "ready": True, "state": "restored"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        _need(sys.platform == "linux" and os.geteuid() == 0, "Linux root is required")
        _need(_sha(SOURCE) == SOURCE_SHA, "embedded source journal hash differs")
        result = recover(apply=args.apply)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, RecoveryError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"operation": "refused", "ready": False, "reason": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
