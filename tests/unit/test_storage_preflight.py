from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

import dashcam.storage.preflight as preflight_module
from dashcam.config import default_config
from dashcam.state import StorageState
from dashcam.storage.preflight import (
    IDENTITY_PATH,
    MAX_MOUNT_OPTIONS,
    PROBE_NAME,
    PROBE_PAYLOAD,
    PosixFactsCollector,
    PreflightFactsError,
    PreflightPolicy,
    PreflightReason,
    PreflightRuntimeError,
    ProbeFile,
    StorageIdentityError,
    load_storage_identity,
    policy_from_identity,
    recording_root_facts_from_mapping,
    run_live_storage_preflight,
    run_storage_preflight,
    storage_identity_from_env,
)

GIB = 1024**3
UUID = "A1B2-C3D4"
FINGERPRINT = "f0a25c0de0000000000000000000000000000000000000000000000000000000"


def _identity_payload() -> bytes:
    return (
        "DASHCAM_STORAGE_SCHEMA_VERSION=1\n"
        "DASHCAM_STORAGE_LAYOUT_VERSION=1\n"
        "DASHCAM_STORAGE_MOUNT=/srv/dashcam\n"
        f"DASHCAM_STORAGE_UUID={UUID}\n"
        "DASHCAM_STORAGE_CID=fixture-card-32gb\n"
        f"DASHCAM_STORAGE_SOURCE_MBR_SHA256={FINGERPRINT}\n"
        "DASHCAM_STORAGE_ROOT_END_SECTOR=20000000\n"
        "DASHCAM_STORAGE_DATA_START_SECTOR=20002048\n"
        "DASHCAM_STORAGE_DATA_END_SECTOR=62000000\n"
        f"DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES={20 * GIB}\n"
    ).encode()


def _policy() -> PreflightPolicy:
    return PreflightPolicy(
        expected_uuid=UUID,
        expected_uuid_suffix="C3D4",
        expected_serial="fixture-card-32gb",
        expected_source_table_fingerprint=FINGERPRINT,
        expected_root_end_sector=20_000_000,
        expected_data_start_sector=20_002_048,
        expected_data_end_sector=62_000_000,
        minimum_capacity_bytes=20 * GIB,
        reserve_bytes=2 * GIB,
    )


def _facts() -> dict[str, object]:
    return {
        "mount": {
            "target": "/srv/dashcam",
            "mounted": True,
            "source": "/dev/mmcblk0p3",
            "filesystem": "exfat",
            "label": "DASHCAM",
            "uuid": UUID,
            "mount_options": ["rw", "noatime", "uid=1000", "gid=1000"],
            "device_id": "179:3",
            "os_root_device_id": "179:2",
        },
        "space": {
            "capacity_bytes": 25 * GIB,
            "free_bytes": 10 * GIB,
        },
        "sentinel": {
            "layout_version": 1,
            "serial": "fixture-card-32gb",
            "dashcam_uuid": UUID,
            "source_table_fingerprint": FINGERPRINT,
            "root_end_sector": 20_000_000,
            "data_start_sector": 20_002_048,
            "data_end_sector": 62_000_000,
        },
    }


class FakeProbeFile:
    def __init__(self, calls: list[tuple[str, object]], fail_at: str | None = None) -> None:
        self.calls = calls
        self.fail_at = fail_at

    def write(self, payload: bytes) -> int:
        self.calls.append(("write", payload))
        if self.fail_at == "write":
            raise OSError("injected write failure")
        if self.fail_at == "short_write":
            return len(payload) - 1
        return len(payload)

    def fsync(self) -> None:
        self.calls.append(("fsync", None))
        if self.fail_at == "fsync":
            raise OSError("injected fsync failure")

    def close(self) -> None:
        self.calls.append(("close", None))
        if self.fail_at == "close":
            raise OSError("injected close failure")


class FakeFilesystem:
    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[str, object]] = []

    def create_exclusive(self, recording_root: str, relative_name: str) -> ProbeFile:
        self.calls.append(("create", (recording_root, relative_name)))
        if self.fail_at == "create":
            raise OSError("injected create failure")
        return FakeProbeFile(self.calls, self.fail_at)

    def unlink(self, recording_root: str, relative_name: str) -> None:
        self.calls.append(("unlink", (recording_root, relative_name)))
        if self.fail_at == "unlink":
            raise OSError("injected unlink failure")


def _nested(table: dict[str, object], name: str) -> dict[str, object]:
    value = table[name]
    assert isinstance(value, dict)
    return value


def test_success_requires_exact_identity_and_bounded_durable_write_probe() -> None:
    filesystem = FakeFilesystem()

    result = run_storage_preflight(_facts(), policy=_policy(), filesystem=filesystem)

    assert result.ready
    assert result.state is StorageState.READY
    assert result.reasons == ()
    assert result.facts is not None
    assert filesystem.calls == [
        ("create", ("/srv/dashcam", PROBE_NAME)),
        ("write", PROBE_PAYLOAD),
        ("fsync", None),
        ("close", None),
        ("unlink", ("/srv/dashcam", PROBE_NAME)),
    ]


def test_unmounted_directory_never_attempts_a_write_or_rootfs_fallback() -> None:
    raw = _facts()
    mount = _nested(raw, "mount")
    mount.update(
        mounted=False,
        source=None,
        filesystem=None,
        label=None,
        uuid=None,
        mount_options=[],
        device_id=None,
    )
    filesystem = FakeFilesystem()

    result = run_storage_preflight(raw, policy=_policy(), filesystem=filesystem)

    assert result.state is StorageState.FAULTED
    assert PreflightReason.UNMOUNTED in result.reasons
    assert PreflightReason.MISSING_MOUNT_IDENTITY in result.reasons
    assert not result.probe_attempted
    assert filesystem.calls == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda raw: _nested(raw, "mount").update(filesystem="ext4"),
            PreflightReason.WRONG_FILESYSTEM,
        ),
        (
            lambda raw: _nested(raw, "mount").update(label="OTHER"),
            PreflightReason.WRONG_LABEL,
        ),
        (
            lambda raw: _nested(raw, "mount").update(uuid="DEAD-BEEF"),
            PreflightReason.WRONG_UUID,
        ),
        (
            lambda raw: _nested(raw, "mount").update(device_id="179:2"),
            PreflightReason.ROOTFS_ALIAS,
        ),
        (
            lambda raw: _nested(raw, "sentinel").update(layout_version=2),
            PreflightReason.WRONG_SENTINEL_VERSION,
        ),
        (
            lambda raw: _nested(raw, "sentinel").update(serial="other-card"),
            PreflightReason.WRONG_SENTINEL_IDENTITY,
        ),
        (
            lambda raw: _nested(raw, "sentinel").update(dashcam_uuid="DEAD-BEEF"),
            PreflightReason.WRONG_SENTINEL_UUID,
        ),
    ],
)
def test_identity_mismatch_fails_closed_without_probe(
    mutate: Callable[[dict[str, object]], None], reason: PreflightReason
) -> None:
    raw = _facts()
    mutate(raw)
    filesystem = FakeFilesystem()

    result = run_storage_preflight(raw, policy=_policy(), filesystem=filesystem)

    assert result.state is StorageState.FAULTED
    assert reason in result.reasons
    assert not result.probe_attempted
    assert filesystem.calls == []


def test_read_only_or_conflicting_options_return_explicit_read_only_state() -> None:
    for options, reason in [
        (["ro", "noatime"], PreflightReason.READ_ONLY),
        (["rw", "ro"], PreflightReason.CONFLICTING_MOUNT_OPTIONS),
        (["noatime"], PreflightReason.READ_ONLY),
    ]:
        raw = _facts()
        _nested(raw, "mount")["mount_options"] = options
        filesystem = FakeFilesystem()

        result = run_storage_preflight(raw, policy=_policy(), filesystem=filesystem)

        assert result.state is StorageState.READ_ONLY
        assert reason in result.reasons
        assert filesystem.calls == []


def test_capacity_free_and_reserve_must_be_sane_before_probe() -> None:
    invalid = _facts()
    _nested(invalid, "space")["free_bytes"] = 26 * GIB
    too_small = _facts()
    _nested(too_small, "space")["capacity_bytes"] = 19 * GIB
    exhausted = _facts()
    _nested(exhausted, "space")["free_bytes"] = 2 * GIB

    invalid_result = run_storage_preflight(invalid, policy=_policy(), filesystem=FakeFilesystem())
    small_result = run_storage_preflight(too_small, policy=_policy(), filesystem=FakeFilesystem())
    exhausted_result = run_storage_preflight(
        exhausted, policy=_policy(), filesystem=FakeFilesystem()
    )

    assert invalid_result.reasons == (PreflightReason.INVALID_SPACE,)
    assert small_result.reasons == (PreflightReason.INSUFFICIENT_CAPACITY,)
    assert exhausted_result.state is StorageState.EMERGENCY
    assert exhausted_result.reasons == (PreflightReason.RESERVE_EXHAUSTED,)


@pytest.mark.parametrize("fail_at", ["create", "write", "short_write", "fsync", "close", "unlink"])
def test_every_write_probe_failure_is_explicit_and_cleanup_is_attempted(
    fail_at: str,
) -> None:
    filesystem = FakeFilesystem(fail_at)

    result = run_storage_preflight(_facts(), policy=_policy(), filesystem=filesystem)

    assert result.state is StorageState.FAULTED
    assert result.reasons == (PreflightReason.WRITE_PROBE_FAILED,)
    assert result.probe_attempted
    assert not result.probe_succeeded
    if fail_at != "create":
        assert ("close", None) in filesystem.calls
        assert ("unlink", ("/srv/dashcam", PROBE_NAME)) in filesystem.calls


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(unexpected=True),
        lambda raw: _nested(raw, "mount").pop("target"),
        lambda raw: _nested(raw, "mount").update(mounted="yes"),
        lambda raw: _nested(raw, "mount").update(mount_options=["rw"] * (MAX_MOUNT_OPTIONS + 1)),
        lambda raw: _nested(raw, "space").update(capacity_bytes=True),
        lambda raw: _nested(raw, "sentinel").update(source_table_fingerprint="not-a-fingerprint"),
    ],
)
def test_malformed_or_excessive_structured_facts_are_bounded_refusals(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    raw = _facts()
    mutate(raw)
    filesystem = FakeFilesystem()

    result = run_storage_preflight(raw, policy=_policy(), filesystem=filesystem)

    assert result.state is StorageState.FAULTED
    assert result.reasons == (PreflightReason.MALFORMED_FACTS,)
    assert result.facts is None
    assert filesystem.calls == []


def test_direct_parser_rejects_shell_text_and_sentinel_geometry_is_checked() -> None:
    with pytest.raises(PreflightFactsError, match="object"):
        recording_root_facts_from_mapping(
            cast(Mapping[str, object], "TARGET=/srv/dashcam FSTYPE=exfat")
        )

    raw = _facts()
    sentinel = _nested(raw, "sentinel")
    sentinel["data_start_sector"] = sentinel["root_end_sector"]
    result = run_storage_preflight(raw, policy=_policy(), filesystem=FakeFilesystem())

    assert result.reasons == (PreflightReason.INVALID_SENTINEL_GEOMETRY,)


def test_storage_identity_handoff_is_closed_and_builds_exact_policy() -> None:
    identity = storage_identity_from_env(_identity_payload())
    policy = policy_from_identity(default_config(), identity)

    assert identity.mount == "/srv/dashcam"
    assert policy.expected_uuid == UUID
    assert policy.expected_uuid_suffix == "C3D4"
    assert policy.expected_root_end_sector == 20_000_000
    assert policy.expected_data_start_sector == 20_002_048
    assert policy.expected_data_end_sector == 62_000_000
    assert policy.reserve_bytes == 2 * GIB


def test_live_preflight_binds_fresh_facts_and_probe_to_identity() -> None:
    identity = storage_identity_from_env(_identity_payload())
    raw = _facts()
    calls: list[tuple[str, str]] = []
    filesystem = FakeFilesystem()

    class Collector:
        def collect(self, recording_root: str) -> Mapping[str, object]:
            assert recording_root == "/srv/dashcam"
            return raw

    def filesystem_factory(
        *,
        recording_root: str,
        expected_device_id: str,
    ) -> FakeFilesystem:
        calls.append((recording_root, expected_device_id))
        return filesystem

    result = run_live_storage_preflight(
        default_config(),
        identity_path="/fixture/storage-volume.env",
        identity_loader=lambda path: identity,
        facts_collector=Collector(),
        filesystem_factory=filesystem_factory,
    )

    assert result.ready
    assert calls == [("/srv/dashcam", "179:3")]
    assert filesystem.calls


def test_live_preflight_refuses_before_probe_when_mount_is_absent() -> None:
    identity = storage_identity_from_env(_identity_payload())
    raw = _facts()
    mount = _nested(raw, "mount")
    mount.update(
        {
            "mounted": False,
            "source": None,
            "filesystem": None,
            "label": None,
            "uuid": None,
            "device_id": None,
        }
    )
    raw["space"] = {"capacity_bytes": None, "free_bytes": None}
    raw["sentinel"] = None
    filesystem = FakeFilesystem()

    class Collector:
        def collect(self, recording_root: str) -> Mapping[str, object]:
            return raw

    result = run_live_storage_preflight(
        default_config(),
        identity_path="/fixture/storage-volume.env",
        identity_loader=lambda path: identity,
        facts_collector=Collector(),
        filesystem_factory=lambda **kwargs: filesystem,
    )

    assert not result.ready
    assert PreflightReason.UNMOUNTED in result.reasons
    assert filesystem.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        _identity_payload() + b"DASHCAM_STORAGE_UNKNOWN=x\n",
        _identity_payload().replace(
            b"DASHCAM_STORAGE_UUID=A1B2-C3D4\n",
            b"DASHCAM_STORAGE_UUID=A1B2-C3D4\nDASHCAM_STORAGE_UUID=A1B2-C3D4\n",
        ),
        _identity_payload().replace(b"SCHEMA_VERSION=1", b"SCHEMA_VERSION=$(id)"),
        _identity_payload().replace(b"DATA_START_SECTOR=20002048", b"DATA_START_SECTOR=1"),
        _identity_payload().rstrip(b"\n"),
    ],
)
def test_storage_identity_rejects_unknown_duplicate_shell_geometry_and_noncanonical(
    payload: bytes,
) -> None:
    with pytest.raises(StorageIdentityError):
        storage_identity_from_env(payload)


def test_identity_loader_rejects_symlink_and_unsafe_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows does not expose the target POSIX mode semantics")
    identity = tmp_path / "storage-volume.env"
    identity.write_bytes(_identity_payload())
    identity.chmod(0o640)

    loaded = load_storage_identity(identity, require_root_owner=False)
    assert loaded.uuid == UUID

    identity.chmod(0o666)
    with pytest.raises(StorageIdentityError, match="mode"):
        load_storage_identity(identity, require_root_owner=False)

    identity.chmod(0o640)
    alias = tmp_path / "alias.env"
    alias.symlink_to(identity)
    with pytest.raises(StorageIdentityError, match="open"):
        load_storage_identity(alias, require_root_owner=False)


def test_exact_sentinel_geometry_mismatch_fails_before_probe() -> None:
    raw = _facts()
    _nested(raw, "sentinel")["data_end_sector"] = 61_999_999
    filesystem = FakeFilesystem()

    result = run_storage_preflight(raw, policy=_policy(), filesystem=filesystem)

    assert result.reasons == (PreflightReason.INVALID_SENTINEL_GEOMETRY,)
    assert filesystem.calls == []


def test_posix_sentinel_reader_requires_canonical_regular_non_symlink_json(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Linux directory-device and O_NOFOLLOW semantics")
    sentinel = cast(dict[str, object], _facts()["sentinel"])
    sentinel_path = tmp_path / ".dashcam-volume"
    sentinel_path.write_text(
        json.dumps(sentinel, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    device_id = preflight_module._directory_device_id(str(tmp_path))

    assert preflight_module._read_canonical_sentinel(str(tmp_path), device_id) == sentinel

    sentinel_path.write_text(json.dumps(sentinel, indent=2) + "\n", encoding="ascii")
    with pytest.raises(Exception, match="not canonical"):
        preflight_module._read_canonical_sentinel(str(tmp_path), device_id)

    sentinel_path.unlink()
    sentinel_path.symlink_to(tmp_path / "foreign")
    with pytest.raises(OSError):
        preflight_module._read_canonical_sentinel(str(tmp_path), device_id)


def test_findmnt_parser_accepts_singleton_and_exact_systemd_bind_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findmnt_row = {
        "target": "/srv/dashcam",
        "source": "/dev/mmcblk0p3",
        "fstype": "exfat",
        "label": "DASHCAM",
        "uuid": UUID,
        "options": "rw,noatime",
        "maj:min": "179:3",
    }

    class Completed:
        returncode = 0
        stderr = b""
        stdout = json.dumps({"filesystems": [findmnt_row]}).encode()

    monkeypatch.setattr(
        "dashcam.storage.preflight.subprocess.run", lambda *args, **kwargs: Completed()
    )
    row = PosixFactsCollector()._find_mount("/srv/dashcam")
    assert row is not None
    assert row["uuid"] == UUID

    Completed.stdout = json.dumps({"filesystems": [findmnt_row, findmnt_row]}).encode()
    duplicate_row = PosixFactsCollector()._find_mount("/srv/dashcam")
    assert duplicate_row == row


def test_findmnt_parser_refuses_differing_duplicates_and_excessive_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findmnt_row = {
        "target": "/srv/dashcam",
        "source": "/dev/mmcblk0p3",
        "fstype": "exfat",
        "label": "DASHCAM",
        "uuid": UUID,
        "options": "rw,noatime",
        "maj:min": "179:3",
    }

    class Completed:
        returncode = 0
        stderr = b""
        stdout = b""

    monkeypatch.setattr(
        "dashcam.storage.preflight.subprocess.run", lambda *args, **kwargs: Completed()
    )
    differing = dict(findmnt_row)
    differing["options"] = "rw,relatime"
    Completed.stdout = json.dumps({"filesystems": [findmnt_row, differing]}).encode()
    with pytest.raises(PreflightRuntimeError, match="differing"):
        PosixFactsCollector()._find_mount("/srv/dashcam")

    Completed.stdout = json.dumps({"filesystems": [findmnt_row] * 3}).encode()
    with pytest.raises(PreflightRuntimeError, match="bound"):
        PosixFactsCollector()._find_mount("/srv/dashcam")


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps(
            {
                "filesystems": [
                    {
                        "target": "/srv/dashcam",
                        "source": "/dev/mmcblk0p3",
                        "fstype": "exfat",
                        "label": "DASHCAM",
                        "uuid": UUID,
                        "options": "rw,noatime",
                        "maj:min": "179:3",
                        "children": [],
                    }
                ]
            }
        ).encode(),
        (
            b'{"filesystems":[{"target":"/srv/dashcam","source":"/dev/mmcblk0p3",'
            b'"fstype":"exfat","label":"DASHCAM","uuid":"A1B2-C3D4",'
            b'"uuid":"A1B2-C3D4","options":"rw,noatime","maj:min":"179:3"}]}'
        ),
    ],
)
def test_findmnt_parser_refuses_nested_extra_fields_and_duplicate_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    class Completed:
        returncode = 0
        stderr = b""
        stdout = payload

    monkeypatch.setattr(
        "dashcam.storage.preflight.subprocess.run", lambda *args, **kwargs: Completed()
    )
    with pytest.raises(PreflightRuntimeError):
        PosixFactsCollector()._find_mount("/srv/dashcam")


def test_status_is_redacted_and_does_not_emit_source_cid_or_fingerprint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_storage_preflight(_facts(), policy=_policy(), filesystem=FakeFilesystem())
    preflight_module._emit_status(preflight_module._redacted_status(result))

    output = capsys.readouterr().out
    assert json.loads(output)["ready"] is True
    assert UUID not in output
    assert "fixture-card-32gb" not in output
    assert FINGERPRINT not in output
    assert "/dev/mmcblk0p3" not in output
    assert IDENTITY_PATH not in output


def test_systemd_unit_allows_only_the_verified_mount_probe_write() -> None:
    root = Path(__file__).resolve().parents[2]
    unit = (root / "systemd" / "dashcam-storage-check.service").read_text()

    assert "ExecStart=/opt/dashcam/venv/bin/python -m dashcam.storage.preflight" in unit
    assert "--identity /etc/dashcam/storage-volume.env" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/srv/dashcam" in unit
    assert "ReadOnlyPaths=/srv/dashcam" not in unit
    assert "TimeoutStartSec=20s" in unit
