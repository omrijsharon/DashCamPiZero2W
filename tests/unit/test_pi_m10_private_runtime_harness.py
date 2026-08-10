from __future__ import annotations

import hashlib
import importlib.util
import io
import sqlite3
import sys
import zipfile
from collections.abc import Callable
from datetime import UTC
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "deploy" / "ssh-dev-validation" / "milestone10-private-runtime"


def _load(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


run = _load("m10_private_runtime", HARNESS / "run.py")
builder = _load("m10_private_runtime_builder", HARNESS / "prepare-bundle.py")
HARNESS_COMMIT = "f" * 40


def _archive(members: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = compression
            archive.writestr(info, payload)
    return stream.getvalue()


def _source(commit: str, archive_name: str, payload: bytes, members: dict[str, bytes]) -> bytes:
    return run.canonical_json(
        {
            "schema_version": 1,
            "git_commit": commit,
            "git_tree": "a" * 40,
            "archive_name": archive_name,
            "archive_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_size": len(payload),
            "members": {
                name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
                for name, value in sorted(members.items())
            },
        }
    )


def _bundle(tmp_path: Path, *, compression: int = zipfile.ZIP_STORED) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    candidate_members = {
        "dashcam/__init__.py": b"candidate\n",
        "dashcam/control/runtime_server.py": b"listener\n",
    }
    rollback_members = {
        "dashcam/__init__.py": b"rollback\n",
        "dashcam/rollback.py": b"guard\n",
    }
    candidate = _archive(candidate_members, compression=compression)
    rollback = _archive(rollback_members)
    payloads = {
        "README.md": b"reviewed\n",
        "run.py": b"#!/usr/bin/env python3\n",
        "BUNDLE.json": run.canonical_json(
            {
                "schema_version": 1,
                "harness_commit": HARNESS_COMMIT,
                "harness_tree": "e" * 40,
                "candidate_commit": run.EXPECTED_CANDIDATE,
                "candidate_tree": "a" * 40,
                "rollback_commit": run.EXPECTED_ROLLBACK,
                "rollback_tree": "a" * 40,
            }
        ),
        "candidate-source.zip": candidate,
        "rollback-source.zip": rollback,
        "CANDIDATE_SOURCE.json": _source(
            run.EXPECTED_CANDIDATE, "candidate-source.zip", candidate, candidate_members
        ),
        "ROLLBACK_SOURCE.json": _source(
            run.EXPECTED_ROLLBACK, "rollback-source.zip", rollback, rollback_members
        ),
    }
    for name, payload in payloads.items():
        (root / name).write_bytes(payload)
    manifest = b"".join(
        f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n".encode("ascii")
        for name in sorted(payloads)
    )
    (root / "SHA256SUMS").write_bytes(manifest)
    return root


def test_virtual_reader_accepts_procfs_zero_size_and_reads_to_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter((b"Raspberry Pi Zero 2 W Rev 1.0\0", b""))
    opened: list[int] = []
    closed: list[int] = []
    monkeypatch.setattr(
        run.os,
        "open",
        lambda _path, flags: opened.append(flags) or 7,
    )
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=0o100444, st_size=0),
    )
    monkeypatch.setattr(run.os, "read", lambda _descriptor, _size: next(reads))
    monkeypatch.setattr(run.os, "close", closed.append)

    assert (
        run._bounded_virtual_read(Path("/proc/device-tree/model"), 256)
        == b"Raspberry Pi Zero 2 W Rev 1.0\0"
    )
    assert closed == [7]
    if getattr(run.os, "O_NOFOLLOW", 0):
        assert opened[0] & run.os.O_NOFOLLOW
    if getattr(run.os, "O_CLOEXEC", 0):
        assert opened[0] & run.os.O_CLOEXEC


@pytest.mark.parametrize(
    ("mode", "chunks", "message"),
    (
        (0o040755, (b"data", b""), "type differs"),
        (0o100444, (b"",), "made no progress"),
        (0o100444, (b"12345",), "exceeded its bound"),
    ),
)
def test_virtual_reader_refuses_type_no_progress_and_oversize(
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    chunks: tuple[bytes, ...],
    message: str,
) -> None:
    reads = iter(chunks)
    monkeypatch.setattr(run.os, "open", lambda _path, _flags: 7)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=mode, st_size=0),
    )
    monkeypatch.setattr(run.os, "read", lambda _descriptor, _size: next(reads))
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match=message):
        run._bounded_virtual_read(Path("/proc/example"), 4)


def test_virtual_identity_parsers_are_canonical_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        Path("/proc/sys/kernel/random/boot_id"): b"11111111-1111-1111-1111-111111111111\n",
        Path("/proc/device-tree/model"): b"Raspberry Pi Zero 2 W Rev 1.0\0",
        Path("/proc/cpuinfo"): b"Processor\t: ARMv7\nSerial          : 0123456789abcdef\n",
    }
    monkeypatch.setattr(
        run,
        "_bounded_virtual_read",
        lambda path, _maximum: payloads[path],
    )

    assert run._read_boot_id() == "11111111-1111-1111-1111-111111111111"
    assert run._read_board_model() == "Raspberry Pi Zero 2 W Rev 1.0"
    assert run._read_cpu_serial() == "0123456789abcdef"

    payloads[Path("/proc/sys/kernel/random/boot_id")] = (
        b"11111111-1111-1111-1111-111111111111\nextra\n"
    )
    with pytest.raises(run.HarnessError, match="boot ID virtual-file shape"):
        run._read_boot_id()
    payloads[Path("/proc/device-tree/model")] = b"model-without-nul"
    with pytest.raises(run.HarnessError, match="board model virtual-file shape"):
        run._read_board_model()
    payloads[Path("/proc/cpuinfo")] = b"Serial : 0123456789abcdef\nSerial : fedcba9876543210\n"
    with pytest.raises(run.HarnessError, match="CPU serial record count"):
        run._read_cpu_serial()


def test_only_declared_virtual_files_use_virtual_reader() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert '_bounded_read(Path("/proc' not in source
    assert '_bounded_read(Path("/sys' not in source
    for declaration in (
        '_bounded_virtual_read(Path("/proc/sys/kernel/random/boot_id"), 37)',
        '_bounded_virtual_read(Path("/proc/device-tree/model"), 256)',
        '_bounded_virtual_read(Path("/proc/cpuinfo"), 256 * 1024)',
        '_bounded_virtual_read(Path("/sys/block") / loop.name / "loop/backing_file", 4096)',
    ):
        assert declaration in source


def test_exfat_sentinel_writer_inherits_mount_identity_without_fchown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"sentinel":true}\n'
    opened: list[tuple[object, int | None]] = []
    fsynced: list[int] = []
    parent = _metadata(
        mode=0o40750,
        uid=42,
        gid=84,
        nlink=2,
        device=7,
        inode=10,
    )
    member = _metadata(
        mode=0o100640,
        uid=42,
        gid=84,
        nlink=1,
        device=7,
        inode=11,
        size=len(payload),
    )

    def open_file(path: object, _flags: int, *args: object, **kwargs: object) -> int:
        opened.append((path, kwargs.get("dir_fd")))
        return 10 if len(opened) == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(run.os, "fstat", lambda descriptor: parent if descriptor == 10 else member)
    monkeypatch.setattr(run.os, "write", lambda _descriptor, view: len(view))
    monkeypatch.setattr(run.os, "fsync", fsynced.append)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(
        run.os,
        "fchown",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fchown must not run on exFAT")),
        raising=False,
    )

    run._write_exfat_sentinel_exclusive(
        Path("/fixture/.dashcam-volume"),
        payload,
        dashcam_uid=42,
        storage_gid=84,
    )

    assert opened == [(Path("/fixture"), None), (".dashcam-volume", 10)]
    assert fsynced == [11, 10]


@pytest.mark.parametrize(
    ("uid", "gid", "mode"),
    (
        (43, 84, 0o100640),
        (42, 85, 0o100640),
        (42, 84, 0o100660),
    ),
)
def test_exfat_sentinel_writer_refuses_wrong_effective_identity(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    payload = b"sentinel\n"
    parent = _metadata(mode=0o40750, uid=42, gid=84, nlink=2, device=7)
    member = _metadata(
        mode=mode,
        uid=uid,
        gid=gid,
        nlink=1,
        device=7,
        size=len(payload),
    )
    opened = 0

    def open_file(_path: object, _flags: int, *args: object, **kwargs: object) -> int:
        nonlocal opened
        opened += 1
        return 10 if opened == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(run.os, "fstat", lambda descriptor: parent if descriptor == 10 else member)
    monkeypatch.setattr(run.os, "write", lambda _descriptor, view: len(view))
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match="effective identity differs"):
        run._write_exfat_sentinel_exclusive(
            Path("/fixture/.dashcam-volume"),
            payload,
            dashcam_uid=42,
            storage_gid=84,
        )


def test_private_state_routes_only_exfat_sentinel_to_fixed_ownership_writer() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    install = source[source.index("def _install_private_state(") : source.index("BIND_PROBE =")]

    assert "_write_exfat_sentinel_exclusive(" in install
    assert '_write_exclusive(recording / ".dashcam-volume"' not in install
    assert "dashcam_uid=dashcam_uid" in install
    assert "storage_gid=storage_gid" in install
    assert int(
        run.MINIMUM_FREE_GIB * 1024**3
    ) < run.PRIVATE_MINIMUM_CAPACITY_BYTES
    assert run.PRIVATE_MINIMUM_CAPACITY_BYTES < run.EXFAT_IMAGE_BYTES


def test_pending_finalizing_sidecar_writer_inherits_exfat_identity_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"fixture":"canonical"}\n'
    root = Path("/var/tmp/dashcam-m10-private.123456789abc/recording")
    opened: list[tuple[object, int | None]] = []
    fsynced: list[int] = []
    metadata = {
        10: _metadata(mode=0o40750, uid=42, gid=84, nlink=1, device=7, inode=10),
        11: _metadata(mode=0o40750, uid=42, gid=84, nlink=1, device=7, inode=11),
        12: _metadata(
            mode=0o100640,
            uid=42,
            gid=84,
            nlink=1,
            device=7,
            inode=12,
            size=len(payload),
        ),
    }

    def open_file(path: object, _flags: int, *args: object, **kwargs: object) -> int:
        opened.append((path, kwargs.get("dir_fd")))
        return 9 + len(opened)

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(run.os, "fstat", metadata.__getitem__)
    monkeypatch.setattr(run.os, "write", lambda _descriptor, view: len(view))
    monkeypatch.setattr(run.os, "fsync", fsynced.append)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(
        run.os,
        "fchown",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fchown must not run on exFAT")),
        raising=False,
    )
    monkeypatch.setattr(
        run.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fchmod must not run on exFAT")),
        raising=False,
    )

    run._write_exfat_pending_finalizing_sidecar(
        root,
        run.FIXTURE_FINALIZING_SIDECAR.as_posix(),
        payload,
        dashcam_uid=42,
        storage_gid=84,
    )

    assert opened == [
        (root, None),
        ("pending", 10),
        ("boot-m10private-000022.partial.json", 11),
    ]
    assert fsynced == [12, 11]


@pytest.mark.parametrize(
    "relative",
    (
        "clips/boot-m10private-000022.partial.json",
        "pending/boot-m10private-000023.partial.json",
        "pending/../boot-m10private-000022.partial.json",
        "/pending/boot-m10private-000022.partial.json",
    ),
)
def test_pending_finalizing_sidecar_writer_refuses_any_other_path(
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    monkeypatch.setattr(
        run.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must refuse first")),
    )

    with pytest.raises(run.HarnessError, match="sidecar path differs"):
        run._write_exfat_pending_finalizing_sidecar(
            Path("/var/tmp/dashcam-m10-private.123456789abc/recording"),
            relative,
            b"payload\n",
            dashcam_uid=42,
            storage_gid=84,
        )


@pytest.mark.parametrize(
    ("descriptor", "changed", "message"),
    (
        (10, {"uid": 43}, "recording root identity differs"),
        (10, {"mode": 0o40770}, "recording root identity differs"),
        (11, {"gid": 85}, "pending directory identity differs"),
        (11, {"device": 8}, "pending directory identity differs"),
        (12, {"mode": 0o100660}, "sidecar identity differs"),
        (12, {"nlink": 2}, "sidecar identity differs"),
        (12, {"device": 8}, "sidecar identity differs"),
        (12, {"size": 7}, "sidecar identity differs"),
    ),
)
def test_pending_finalizing_sidecar_writer_refuses_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    descriptor: int,
    changed: dict[str, int],
    message: str,
) -> None:
    payload = b"canonical\n"
    values = {
        10: {"mode": 0o40750, "uid": 42, "gid": 84, "nlink": 1, "device": 7},
        11: {"mode": 0o40750, "uid": 42, "gid": 84, "nlink": 1, "device": 7},
        12: {
            "mode": 0o100640,
            "uid": 42,
            "gid": 84,
            "nlink": 1,
            "device": 7,
            "size": len(payload),
        },
    }
    values[descriptor].update(changed)
    opened = 9

    def open_file(_path: object, _flags: int, *args: object, **kwargs: object) -> int:
        nonlocal opened
        opened += 1
        return opened

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda fd: _metadata(**values[fd]),
    )
    monkeypatch.setattr(run.os, "write", lambda _descriptor, view: len(view))
    monkeypatch.setattr(run.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match=message):
        run._write_exfat_pending_finalizing_sidecar(
            Path("/var/tmp/dashcam-m10-private.123456789abc/recording"),
            run.FIXTURE_FINALIZING_SIDECAR.as_posix(),
            payload,
            dashcam_uid=42,
            storage_gid=84,
        )


def test_fixture_storage_identity_is_resolved_inside_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run.sys, "platform", "linux")
    monkeypatch.setattr(run.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(
            getpwnam=lambda name: SimpleNamespace(pw_name=name, pw_uid=42),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "grp",
        SimpleNamespace(
            getgrnam=lambda name: SimpleNamespace(gr_name=name, gr_gid=84),
        ),
    )

    assert run._fixture_storage_identity() == (42, 84)


def test_seed_fixture_routes_only_generated_finalizing_sidecar_to_exfat_writer() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    seed = source[source.index("def _seed_fixture(") : source.index("def _catalog_counts(")]

    assert seed.count("_write_exfat_pending_finalizing_sidecar(") == 1
    assert "_write_exclusive(root / finalizing.sidecar_path" not in seed
    assert "dashcam_uid, storage_gid = _fixture_storage_identity()" in seed


def test_bundle_verifier_closes_both_exact_sources(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    digest = hashlib.sha256((root / "SHA256SUMS").read_bytes()).hexdigest()

    result = run.verify_bundle(root, digest, HARNESS_COMMIT)

    assert result["candidate"]["git_commit"] == run.EXPECTED_CANDIDATE
    assert result["rollback"]["git_commit"] == run.EXPECTED_ROLLBACK
    with pytest.raises(run.HarnessError, match="manifest"):
        run.verify_bundle(root, "0" * 64, HARNESS_COMMIT)
    with pytest.raises(run.HarnessError, match="identity"):
        run.verify_bundle(root, digest, HARNESS_COMMIT, "b" * 40)


def test_bundle_verifier_rejects_compression_extra_and_traversal(tmp_path: Path) -> None:
    compressed = _bundle(tmp_path / "compressed", compression=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256((compressed / "SHA256SUMS").read_bytes()).hexdigest()
    with pytest.raises(run.HarnessError, match="unsafe member"):
        run.verify_bundle(compressed, digest, HARNESS_COMMIT)

    extra = _bundle(tmp_path / "extra")
    (extra / "EXTRA").write_text("no\n", encoding="ascii")
    extra_digest = hashlib.sha256((extra / "SHA256SUMS").read_bytes()).hexdigest()
    with pytest.raises(run.HarnessError, match="directory member"):
        run.verify_bundle(extra, extra_digest, HARNESS_COMMIT)

    traversal = _archive({"../escape.py": b"bad\n"})
    assert traversal


def test_builder_source_archives_are_deterministic_stored_and_stripped() -> None:
    members = {
        "dashcam/__init__.py": b"a\n",
        "dashcam/recorder/runtime.py": b"b\n",
    }
    first = builder._zip_bytes(members)
    second = builder._zip_bytes(dict(reversed(tuple(members.items()))))

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == sorted(members)
        assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_exact_root_budget_and_threshold_boundaries() -> None:
    assert run.ROOT_REQUIRED_FREE_BYTES == 2_701_131_776
    assert run.root_budget_satisfied(run.ROOT_REQUIRED_FREE_BYTES)
    assert not run.root_budget_satisfied(run.ROOT_REQUIRED_FREE_BYTES - 1)
    assert not run.root_budget_satisfied(True)

    low, high, emergency = run.resolved_thresholds(run.EXFAT_IMAGE_BYTES)
    assert emergency == 16 * 1024**2
    assert emergency < 0.1 * 1024**3 < low < high < run.EXFAT_IMAGE_BYTES


def test_candidate_and_rollback_configs_are_deliberately_distinct() -> None:
    candidate = run._config(rollback=False).decode("ascii")
    rollback = run._config(rollback=True).decode("ascii")

    for value in (
        "width = 1920",
        "height = 1080",
        "fps = 30",
        "hardware_encoder_required = true",
        "enabled = false",
        "low_watermark_percent = 30",
        "high_watermark_percent = 35",
        "minimum_free_gib = 0.1",
        "emergency_free_mib = 16",
    ):
        assert value in candidate
        assert value in rollback
    assert "download_lease_timeout_s = 300" in candidate
    assert "download_lease_timeout_s" not in rollback
    for config in (candidate, rollback):
        assert 'device = "/dev/dashcam-gps-deliberately-absent"' in config
        assert "/run/dashcam/gps-deliberately-absent" not in config


def test_generated_configs_reach_exact_candidate_parser_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    from dashcam.config import ConfigError, parse_config_toml

    candidate = parse_config_toml(run._config(rollback=False).decode("ascii"))

    assert candidate.gps.device == "/dev/dashcam-gps-deliberately-absent"
    assert candidate.storage.download_lease_timeout_s == 300
    with pytest.raises(
        ConfigError,
        match=r"storage is missing required key: download_lease_timeout_s",
    ):
        parse_config_toml(run._config(rollback=True).decode("ascii"))


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    return work / "recording", work / "state", Path("/run/dashcam-m10-private.123456789abc")


def test_candidate_transient_unit_matches_private_production_contract(tmp_path: Path) -> None:
    recording, state, runtime = _paths(tmp_path)
    properties = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="candidate",
    )
    text = "\n".join(properties)

    assert "User=dashcam" in properties
    assert "Group=dashcam" in properties
    assert (
        "SupplementaryGroups=audio video render dialout dashcam-storage dashcam-api" in properties
    )
    assert "Type=notify" in properties
    assert "Restart=on-failure" in properties
    assert "RuntimeMaxSec=420s" in properties
    assert "PrivateDevices=no" in properties
    assert "RestrictAddressFamilies=AF_UNIX" in properties
    assert f"{recording.as_posix()}:/srv/dashcam" in text
    assert f"{state.as_posix()}:/var/lib/dashcam" in text
    assert f"{runtime.as_posix()}:/run/dashcam" in text
    assert sum(prop.startswith("BindPaths=") for prop in properties) == 1
    assert not any(prop.startswith("ReadWritePaths=") for prop in properties)
    adapter = next(prop for prop in properties if prop.startswith("BindReadOnlyPaths="))
    assert "/usr/bin/findmnt:/usr/libexec/dashcam-m10-findmnt-real" in adapter
    assert f"{recording.parent.as_posix()}/bundle/run.py:/usr/bin/findmnt" in adapter
    assert "StateDirectory=" not in text
    assert "RuntimeDirectory=" not in text


def test_rollback_recovery_is_camera_closed_and_rollback_recorder_is_minimal(
    tmp_path: Path,
) -> None:
    recording, state, runtime = _paths(tmp_path)
    recovery = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="rollback-recovery",
    )
    recorder = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="rollback-recorder",
    )
    recovery_text = "\n".join(recovery)
    recorder_text = "\n".join(recorder)

    assert "Type=oneshot" in recovery
    assert "Restart=no" in recovery
    assert "SupplementaryGroups=dashcam-storage" in recovery
    assert "PrivateDevices=no" in recovery
    assert "DevicePolicy=auto" in recovery
    for group in ("audio", "video", "render", "dialout", "dashcam-api"):
        assert group not in recovery_text
    assert "SupplementaryGroups=video render dialout dashcam-storage" in recorder
    assert "dashcam-api" not in recorder_text
    assert "audio" not in recorder_text


def test_bind_source_allowlist_refuses_production_or_foreign_paths() -> None:
    with pytest.raises(run.HarnessError, match="outside"):
        run.render_transient_properties(
            recording_source=Path("/srv/dashcam"),
            state_source=Path("/var/lib/dashcam"),
            runtime_source=Path("/run/dashcam"),
            role="candidate",
        )


def test_private_findmnt_adapter_selects_only_active_device_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = {"target", "source", "fstype", "label", "uuid", "options", "maj:min"}
    hidden = {key: None for key in keys}
    hidden.update(
        {"target": "/srv/dashcam", "source": "/dev/mmcblk0p3", "maj:min": "179:3"}
    )
    active = {key: None for key in keys}
    active.update({"target": "/srv/dashcam", "source": "/dev/loop7", "maj:min": "7:7"})
    observed = SimpleNamespace(
        returncode=0,
        stderr=b"",
        stdout=run.canonical_json({"filesystems": [hidden, active]}),
    )
    output = io.BytesIO()
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(run.os, "stat", lambda _path: SimpleNamespace(st_dev=77))
    monkeypatch.setattr(run.os, "major", lambda _device: 7, raising=False)
    monkeypatch.setattr(run.os, "minor", lambda _device: 7, raising=False)
    monkeypatch.setattr(run.sys, "stdout", SimpleNamespace(buffer=output))

    assert run._findmnt_adapter(
        (
            "--json",
            "--mountpoint",
            "/srv/dashcam",
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        )
    ) == 0
    assert output.getvalue() == run.canonical_json({"filesystems": [active]})
    assert run._findmnt_adapter(("--json", "--target", "/srv/dashcam")) == 2


@pytest.mark.parametrize(
    "document",
    (
        {"filesystems": []},
        {"filesystems": [{"target": "/srv/dashcam"}]},
        {
            "filesystems": [
                {
                    "target": "/srv/dashcam",
                    "source": "/dev/loop6",
                    "fstype": "exfat",
                    "label": "M10PRIVATE",
                    "uuid": "1111-2222",
                    "options": "rw",
                    "maj:min": "7:6",
                }
            ]
        },
        {
            "filesystems": [
                {
                    "target": "/srv/dashcam",
                    "source": "/dev/loop7",
                    "fstype": "exfat",
                    "label": "M10PRIVATE",
                    "uuid": "1111-2222",
                    "options": "rw",
                    "maj:min": "7:7",
                },
                {
                    "target": "/srv/dashcam",
                    "source": "/dev/loop8",
                    "fstype": "exfat",
                    "label": "M10PRIVATE",
                    "uuid": "3333-4444",
                    "options": "rw",
                    "maj:min": "7:7",
                },
            ]
        },
        {"filesystems": [], "unexpected": "private"},
    ),
)
def test_private_findmnt_adapter_refuses_nonexact_or_ambiguous_rows(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, object]
) -> None:
    observed = SimpleNamespace(
        returncode=0,
        stderr=b"",
        stdout=run.canonical_json(document),
    )
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(run.os, "stat", lambda _path: SimpleNamespace(st_dev=77))
    monkeypatch.setattr(run.os, "major", lambda _device: 7, raising=False)
    monkeypatch.setattr(run.os, "minor", lambda _device: 7, raising=False)

    assert run._findmnt_adapter(
        (
            "--json",
            "--mountpoint",
            "/srv/dashcam",
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        )
    ) == 2


def test_catalog_observer_requires_canonical_state_recording_siblings(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "recording"
    state = tmp_path / "state"
    recording.mkdir()
    state.mkdir()
    catalog = state / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    connection.executescript(
        """
        CREATE TABLE clips (
            clip_id TEXT, lifecycle TEXT, video_path TEXT, sidecar_path TEXT,
            protected INTEGER, lease_holder TEXT, start_monotonic_ns INTEGER,
            end_monotonic_ns INTEGER, retention_order INTEGER, size_bytes INTEGER,
            protection_reason TEXT, pair_reconciled INTEGER, managed INTEGER
        );
        CREATE TABLE operation_intents (
            intent_id TEXT, kind TEXT, status TEXT, clip_id TEXT,
            completed_monotonic_ns INTEGER, created_monotonic_ns INTEGER
        );
        CREATE TABLE protection_events (
            event_id TEXT, source TEXT, current_clip_id TEXT,
            requested_previous INTEGER, requested_next INTEGER,
            missing_previous INTEGER, remaining_next INTEGER,
            triggered_monotonic_ns INTEGER
        );
        CREATE TABLE protection_event_targets (
            event_id TEXT, clip_id TEXT, role TEXT, ordinal INTEGER
        );
        """
    )
    connection.close()

    assert run._query_catalog(catalog, recording) == {
        "clips": [],
        "intents": [],
        "events": [],
        "targets": [],
    }
    with pytest.raises(run.HarnessError, match="observer path"):
        run._query_catalog(tmp_path / "catalog.sqlite3", recording)
    with pytest.raises(run.HarnessError, match="observer path"):
        run._query_catalog(catalog, tmp_path / "media")


def test_clean_storage_safety_stop_requires_zero_restart_and_success() -> None:
    clean = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "NRestarts": "0",
    }
    run.validate_clean_safety_stop(clean)

    for key, value in (("Result", "exit-code"), ("ExecMainStatus", "1"), ("NRestarts", "1")):
        changed = {**clean, key: value}
        with pytest.raises(run.HarnessError, match="safety-stop"):
            run.validate_clean_safety_stop(changed)


def test_startup_budget_requires_exact_64_complete_one_pending_and_no_runtime() -> None:
    exact = {
        "delete_complete": 64,
        "delete_pending": 1,
        "camera_opened": False,
        "listener_present": False,
        "catalog_worker_count": 0,
    }
    run.validate_startup_delete_bound(exact)
    for key, value in (
        ("delete_complete", 65),
        ("delete_pending", 0),
        ("camera_opened", True),
        ("listener_present", True),
        ("catalog_worker_count", 1),
    ):
        with pytest.raises(run.HarnessError, match="budget"):
            run.validate_startup_delete_bound({**exact, key: value})


def test_exact_candidate_fixture_apis_create_a_b_and_65_delete_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    from dashcam.metadata import schema

    def bounded_member(root: Path, relative: str, size: int) -> None:
        target = root / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture\n" + bytes(min(size, 4096) - 8))

    monkeypatch.setattr(run, "_member", bounded_member)
    monkeypatch.setattr(run, "_fixture_storage_identity", lambda: (42, 84))
    monkeypatch.setattr(
        run,
        "_write_exfat_pending_finalizing_sidecar",
        lambda root, relative, payload, **_kwargs: (
            root / PurePosixPath(relative)
        ).write_bytes(payload),
    )
    monkeypatch.setattr(schema, "ZoneInfo", lambda _name: UTC)
    for phase in ("A", "B", "C"):
        phase_root = tmp_path / phase
        recording = phase_root / "recording"
        recording.mkdir(parents=True)
        for name in ("clips", "protected", "pending"):
            (recording / name).mkdir()
        result = run._seed_fixture(
            phase,
            recording,
            phase_root / "catalog.sqlite3",
            "11111111-1111-1111-1111-111111111111",
        )
        assert result["phase"] == phase
        counts = run._catalog_counts(phase_root / "catalog.sqlite3")
        if phase == "A":
            assert counts["finalizing"] == 1
            assert counts["leases"] == 1
        elif phase == "B":
            assert len(result["protected_ids"]) == 8
        else:
            assert counts["delete_pending"] == 65
            assert len(result["pending_delete_ids"]) == 65
            assert all(isinstance(row, dict) for row in result["pending_delete_ids"])

    rollback_root = tmp_path / "D" / "recording"
    rollback_root.mkdir(parents=True)
    for name in ("clips", "protected", "pending"):
        (rollback_root / name).mkdir()
    rollback = run._seed_fixture(
        "D",
        rollback_root,
        tmp_path / "D" / "catalog.sqlite3",
        "11111111-1111-1111-1111-111111111111",
    )
    assert rollback["schema5_latch_absent"] is True


def test_protected_emergency_fixture_is_reachable_within_filler_bound() -> None:
    emergency_target = 14 * 1024**2
    protected_bytes = 8 * 16 * 1024**2
    required = run.EXFAT_IMAGE_BYTES - protected_bytes - emergency_target
    assert required < run.MAX_FILLER_BYTES


@pytest.mark.parametrize("concurrent_drop", (8 * 1024**2, 16 * 1024**2 + 4096))
def test_filler_allows_only_one_chunk_of_concurrent_recording_decline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_drop: int,
) -> None:
    block = 4096
    target = 64 * 1024**2
    before = target + 20 * 1024**2
    observations = iter(
        (
            before,
            before,
            target + 3 * block,
            target + 2 * block,
            target - concurrent_drop,
        )
    )

    def statvfs(_path: Path) -> SimpleNamespace:
        free = next(observations)
        return SimpleNamespace(
            f_bavail=free // block,
            f_frsize=block,
            f_blocks=(before + 64 * 1024**2) // block,
        )

    monkeypatch.setattr(run.os, "statvfs", statvfs, raising=False)
    monkeypatch.setattr(
        run.os,
        "posix_fallocate",
        lambda _descriptor, _offset, _amount: None,
        raising=False,
    )

    if concurrent_drop <= run.FILLER_CHUNK_BYTES:
        path, allocated = run._allocate_filler(tmp_path, target)
        assert path.is_file()
        assert allocated > 0
    else:
        with pytest.raises(run.HarnessError, match="below its bounded target"):
            run._allocate_filler(tmp_path, target)


def test_filler_target_direction_refusals_are_distinct_reviewed_lines() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    messages = (
        "filler ended above its bounded target",
        "filler ended below its bounded target",
    )
    observed = [
        index + 1
        for index, line in enumerate(source.splitlines())
        if any(message in line for message in messages)
    ]

    assert len(observed) == 2
    assert len(set(observed)) == 2
    assert set(observed).issubset(run._reviewed_function_lines()["allocate_filler"])


@pytest.mark.parametrize("allow_concurrent_reclaim", (False, True))
def test_filler_upward_jump_requires_explicit_live_reclaim_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_concurrent_reclaim: bool,
) -> None:
    block = 4096
    target = 64 * 1024**2
    before = target + 20 * 1024**2
    observations = iter(
        (
            before,
            before,
            target + 3 * block,
            target + 2 * block,
            target + 8 * 1024**2,
        )
    )

    def statvfs(_path: Path) -> SimpleNamespace:
        free = next(observations)
        return SimpleNamespace(
            f_bavail=free // block,
            f_frsize=block,
            f_blocks=(before + 64 * 1024**2) // block,
        )

    monkeypatch.setattr(run.os, "statvfs", statvfs, raising=False)
    monkeypatch.setattr(
        run.os,
        "posix_fallocate",
        lambda _descriptor, _offset, _amount: None,
        raising=False,
    )

    if allow_concurrent_reclaim:
        _path, allocated = run._allocate_filler(
            tmp_path,
            target,
            allow_concurrent_reclaim=True,
        )
        assert allocated > 0
    else:
        with pytest.raises(run.HarnessError, match="above its bounded target"):
            run._allocate_filler(tmp_path, target)


def test_only_live_fillers_allow_catalog_proven_reclaim_increase() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert source.count("allow_concurrent_reclaim=True") == 3
    assert "filler, filler_bytes = _allocate_filler(root, emergency" in source
    assert "startup_filler, startup_filler_bytes = _allocate_filler(" in source
    assert "LIVE_FILLER_CHUNK_BYTES: Final = 256 * 1024" in source
    assert "if allow_concurrent_reclaim else FILLER_CHUNK_BYTES" in source
    assert "if allow_concurrent_reclaim:\n                time.sleep(0.05)" in source


def test_candidate_selection_waits_for_reclaimer_quiescence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    retention = {key: None for key in run.RETENTION_STATUS_KEYS}
    retention.update(
        {
            "mode": "NORMAL",
            "fault": None,
            "trigger": None,
            "stop_required": False,
        }
    )
    (runtime / "status.json").write_bytes(
        run.canonical_json({"runtime": {"storage_retention": retention}})
    )
    expected = {"clips": [{"clip_id": "fresh"}]}
    monkeypatch.setattr(run, "_catalog_counts", lambda _catalog: {"delete_pending": 0})
    monkeypatch.setattr(
        run,
        "_query_catalog",
        lambda _catalog, _root: expected,
    )

    assert run._wait_reclaim_quiescent(
        runtime,
        tmp_path / "catalog.sqlite3",
        tmp_path / "recording",
    ) is expected
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    quiescent = "_wait_reclaim_quiescent(runtime, catalog, root)"
    candidates = "candidates = _wait_listener_candidates(catalog, root)"
    assert quiescent in source
    assert candidates in source
    assert source.index(quiescent) < source.index(candidates)


def test_strict_idr_parser_rejects_keyframe_without_idr() -> None:
    assert run._contains_idr(b"\x00\x00\x00\x01\x65\x88")
    assert run._contains_idr((2).to_bytes(4, "big") + b"\x65\x88")
    assert not run._contains_idr(b"\x00\x00\x00\x01\x61\x88")


def _healthy_status() -> dict[str, object]:
    return {
        "runtime": {
            "pipeline_restart_count": 0,
            "video": {
                "width": 1920,
                "height": 1080,
                "frames_per_second": 30,
                "codec": "h264",
                "hardware_encoded": True,
                "effective_caps": {
                    "raw_format": "NV12",
                    "fps_numerator": 30,
                    "fps_denominator": 1,
                    "h264_profile": "high",
                    "h264_level": "4.1",
                },
                "encoder_identity": {
                    "factory_name": "v4l2h264enc",
                    "factory_class": "Codec/Encoder/Video/Hardware",
                    "device_path": "/dev/video11",
                },
            },
            "frames": {"raw": 91, "encoded": 90, "dropped": 0, "drop_source": "pts"},
            "overlay": {
                "last_error": None,
                "renderer": {
                    "last_error": None,
                    "update_rejections": 0,
                    "contract_mismatches": 0,
                    "transform_failures": 0,
                    "mapping_limit_rejections": 0,
                    "sync_failures": 0,
                },
            },
        }
    }


def test_runtime_health_requires_exact_hardware_and_known_zero_drops() -> None:
    assert run._runtime_health(_healthy_status())["encoder_factory"] == "v4l2h264enc"
    for mutation in (None, True, 1):
        status = _healthy_status()
        status["runtime"]["frames"]["dropped"] = mutation  # type: ignore[index]
        with pytest.raises(run.HarnessError, match="runtime"):
            run._runtime_health(status)
    status = _healthy_status()
    status["runtime"]["video"]["encoder_identity"]["factory_name"] = "x264enc"  # type: ignore[index]
    with pytest.raises(run.HarnessError, match="encoder"):
        run._runtime_health(status)


def test_exact_c_transition_binds_first_64_and_preserves_65th(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    (root / "clips").mkdir(parents=True)
    oracle: list[dict[str, object]] = []
    clips: list[dict[str, object]] = []
    intents: list[dict[str, object]] = []
    for index in range(65):
        video_path = f"clips/{index}.mp4"
        sidecar_path = f"clips/{index}.json"
        video_payload, sidecar_payload = b"video", b"sidecar"
        if index == 64:
            (root / video_path).write_bytes(video_payload)
            (root / sidecar_path).write_bytes(sidecar_payload)
        oracle.append(
            {
                "intent_id": f"intent-{index}",
                "clip_id": f"clip-{index}",
                "video_path": video_path,
                "sidecar_path": sidecar_path,
                "video_sha256": hashlib.sha256(video_payload).hexdigest(),
                "sidecar_sha256": hashlib.sha256(sidecar_payload).hexdigest(),
            }
        )
        clips.append(
            {
                "clip_id": f"clip-{index}",
                "lifecycle": "DELETING" if index == 64 else "DELETED",
            }
        )
        intents.append(
            {
                "intent_id": f"intent-{index}",
                "clip_id": f"clip-{index}",
                "status": "PENDING" if index == 64 else "COMPLETE",
            }
        )
    fixture = {"pending_delete_ids": oracle}
    snapshot = {"clips": clips, "intents": intents}
    run._validate_c_exact_transition(fixture, snapshot, root)
    intents[0]["status"], intents[64]["status"] = "PENDING", "COMPLETE"
    with pytest.raises(run.HarnessError, match="first 64"):
        run._validate_c_exact_transition(fixture, snapshot, root)


def test_active_assertion_is_scoped_to_same_writing_interval() -> None:
    before = {
        "clip_id": "a",
        "lifecycle": "WRITING",
        "video_present": True,
        "delete_intent": False,
    }
    run.validate_writing_interval(before, dict(before))

    # Once the former active UUID is FINALIZED the assertion intentionally no longer applies.
    with pytest.raises(run.HarnessError, match="WRITING"):
        run.validate_writing_interval(before, {**before, "lifecycle": "FINALIZED"})
    with pytest.raises(run.HarnessError, match="WRITING"):
        run.validate_writing_interval(before, {**before, "delete_intent": True})


def test_source_orders_a_rollback_b_c_and_fail_safe_single_start() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    qualify = source[source.index("def qualify(") : source.index("def _parser(")]

    positions = [
        qualify.index('phase_results["A"]'),
        qualify.index('phase_results["D"]'),
        qualify.index('phase_results["B"]'),
        qualify.index('phase_results["C"]'),
    ]
    assert positions == sorted(positions)
    assert "StartLimitBurst=1" in source
    assert "StartLimitIntervalSec=300s" in source
    assert "validate_clean_safety_stop(terminal)" in source


def test_source_runtime_excludes_and_restores_ordinary_recorder() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert '"mask", "--runtime", "dashcamd.service"' not in source
    assert "RefuseManualStart=yes" in source
    assert "ConditionPathExists=" in source
    assert "_remove_owned_runtime_exclusion(nonce, owned_exclusion)" in source
    assert "_require_excluded(" in source
    assert "NRestarts" in source
    assert "production host snapshot changed" in source
    assert "systemctl start dashcamd" not in source
    assert 'state["restore_authorized"] is True' in source
    assert "recover_owned_work" in source
    assert "QUALIFICATION_TIMEOUT_S: Final = 900" in source


def test_runtime_exclusion_is_owned_before_reload_and_matches_measured_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    sequence: list[str] = []
    nonce = "123456789abc"
    drop_in = "/run/systemd/system/dashcamd.service.d/99-m10-exclusion.conf"
    states = iter(
        (
            {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "UnitFileState": "enabled",
                "NRestarts": "0",
                "FragmentPath": "/etc/systemd/system/dashcamd.service",
                "DropInPaths": "",
                "RefuseManualStart": "no",
                "Conditions": "",
            },
            {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "UnitFileState": "enabled",
                "NRestarts": "0",
                "FragmentPath": "/etc/systemd/system/dashcamd.service",
                "DropInPaths": drop_in,
                "RefuseManualStart": "yes",
                "Conditions": "[unprintable]",
            },
        )
    )

    def service_properties(_unit: str) -> dict[str, str]:
        state = next(states)
        sequence.append(
            f"observe:{state['LoadState']}:{state['UnitFileState']}:{state['RefuseManualStart']}"
        )
        return state

    monkeypatch.setattr(run, "_service_properties", service_properties)

    def command(arguments: tuple[object, ...], **_kwargs: object) -> None:
        call = tuple(str(value) for value in arguments)
        calls.append(call)
        sequence.append(f"command:{Path(call[0]).name}:{call[1]}")

    monkeypatch.setattr(run, "_command", command)
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    owned = {"content": "owned"}
    monkeypatch.setattr(
        run,
        "_create_runtime_exclusion",
        lambda _nonce: sequence.append("create:owned-exclusion") or owned,
    )
    prior = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
        "NRestarts": "0",
        "FragmentPath": "/etc/systemd/system/dashcamd.service",
        "DropInPaths": "",
        "RefuseManualStart": "no",
        "Conditions": "",
    }
    monkeypatch.setattr(
        run,
        "_read_recovery_journal",
        lambda _work: {
            "phase": "PREPARED",
            "prior_unit": prior,
            "prior_exclusion_present": False,
        },
    )
    monkeypatch.setattr(
        run,
        "_transition_recovery_journal",
        lambda _work, expected, target, **_kwargs: sequence.append(
            f"transition:{expected}>{target}"
        ),
    )
    with (
        pytest.raises(RuntimeError, match="injected"),
        run._runtime_exclusion(Path(f"/var/tmp/dashcam-m10-private.{nonce}")),
    ):
        raise RuntimeError("injected")
    assert not any("mask" in call for call in calls)
    assert sequence == [
        "observe:loaded:enabled:no",
        "transition:PREPARED>EXCLUSION_INTENT",
        "create:owned-exclusion",
        "transition:EXCLUSION_INTENT>EXCLUSION_OWNED",
        "command:systemctl:daemon-reload",
        "observe:loaded:enabled:yes",
    ]


def test_runtime_exclusion_refuses_preexisting_operator_drop_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
        "NRestarts": "0",
        "FragmentPath": "/etc/systemd/system/dashcamd.service",
        "DropInPaths": "/run/systemd/system/dashcamd.service.d/operator.conf",
        "RefuseManualStart": "no",
        "Conditions": "",
    }
    transitions: list[tuple[str, str]] = []
    monkeypatch.setattr(run, "_service_properties", lambda _unit: dict(prior))
    monkeypatch.setattr(
        run,
        "_read_recovery_journal",
        lambda _work: {
            "phase": "PREPARED",
            "prior_unit": run._unit_restore_facts(prior),
            "prior_exclusion_present": False,
        },
    )
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: self == run.EXCLUSION_DIRECTORY,
    )
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(
        run,
        "_transition_recovery_journal",
        lambda _work, expected, target, **_kwargs: transitions.append((expected, target)),
    )

    with (
        pytest.raises(run.HarnessError, match="runtime exclusion override"),
        run._runtime_exclusion(Path("/var/tmp/dashcam-m10-private.123456789abc")),
    ):
        raise AssertionError("operator drop-in must refuse before entry")

    assert transitions == []


def test_runtime_exclusion_accepts_only_systemd257_unprintable_condition_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "123456789abc"
    before = {
        "LoadState": "loaded",
        "UnitFileState": "enabled",
    }
    values = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "UnitFileState": "enabled",
        "DropInPaths": "/run/systemd/system/dashcamd.service.d/99-m10-exclusion.conf",
        "RefuseManualStart": "yes",
        "Conditions": "[unprintable]",
    }
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)

    assert run._exclusion_loaded(values, nonce, before) is True
    for wrong in (
        "",
        "[unprintable] ",
        "unprintable",
        f"[type=ConditionPathExists parameter=/run/dashcam-m10-exclusion-{nonce}-absent]",
    ):
        assert (
            run._exclusion_loaded(
                {**values, "Conditions": wrong},
                nonce,
                before,
            )
            is False
        )
    assert (
        run._exclusion_loaded(
            {**values, "DropInPaths": "/run/systemd/system/dashcamd.service.d/wrong.conf"},
            nonce,
            before,
        )
        is False
    )


def test_service_property_parser_preserves_actual_unprintable_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        b"LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
        b"UnitFileState=enabled\nDropInPaths=/run/systemd/system/"
        b"dashcamd.service.d/99-m10-exclusion.conf\nRefuseManualStart=yes\n"
        b"Conditions=[unprintable]\n"
    )
    monkeypatch.setattr(
        run,
        "_command",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=payload),
    )

    values = run._service_properties("dashcamd.service")

    assert values["Conditions"] == "[unprintable]"
    assert values["DropInPaths"].endswith("/99-m10-exclusion.conf")


def _exact_exclusion_facts(nonce: str = "123456789abc") -> dict[str, object]:
    content = run._exclusion_content(nonce)
    return {
        "directory_device": 7,
        "directory_inode": 11,
        "directory_uid": 0,
        "directory_gid": 0,
        "directory_mode": 0o755,
        "directory_nlink": 2,
        "file_device": 7,
        "file_inode": 12,
        "file_uid": 0,
        "file_gid": 0,
        "file_mode": 0o644,
        "file_nlink": 1,
        "file_size": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content": content.decode("ascii"),
        "condition_path": f"/run/dashcam-m10-exclusion-{nonce}-absent",
    }


def test_recovery_journal_binds_exact_nonce_content_and_file_facts() -> None:
    exact = _exact_exclusion_facts()
    assert run._owned_exclusion_document_valid(exact, "123456789abc") is True
    assert run._owned_exclusion_document_valid({**exact, "file_inode": 13}, "123456789abc") is True
    assert (
        run._owned_exclusion_document_valid(
            {**exact, "content": "[Unit]\nRefuseManualStart=no\n"},
            "123456789abc",
        )
        is False
    )
    assert run._owned_exclusion_document_valid(exact, "fedcba987654") is False


@pytest.mark.parametrize("phase", ("PREPARED", "EXCLUSION_INTENT", "RESTORED"))
def test_recovery_without_exclusion_needs_no_removal(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    assert (
        run.validate_recovery_exclusion_authority(
            phase,
            nonce="123456789abc",
            exclusion_present=False,
            owned_exclusion=None,
        )
        is False
    )


def test_recovery_owned_exclusion_requires_exact_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"content": "exact"}
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_owned_exclusion_facts", lambda _nonce: expected)
    assert (
        run.validate_recovery_exclusion_authority(
            "EXCLUSION_OWNED",
            nonce="123456789abc",
            exclusion_present=True,
            owned_exclusion=expected,
        )
        is True
    )


def test_recovery_refuses_ambiguous_operator_drop_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    with pytest.raises(run.HarnessError, match="does not prove ownership"):
        run.validate_recovery_exclusion_authority(
            "EXCLUSION_INTENT",
            nonce="123456789abc",
            exclusion_present=True,
            owned_exclusion=None,
        )


def test_recovery_refuses_condition_marker_that_would_enable_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: "exclusion-" in self.name)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    with pytest.raises(run.HarnessError, match="impossible condition path exists"):
        run.validate_recovery_exclusion_authority(
            "EXCLUSION_OWNED",
            nonce="123456789abc",
            exclusion_present=True,
            owned_exclusion={"content": "exact"},
        )


def _metadata(
    *,
    mode: int,
    uid: int = 0,
    gid: int = 0,
    nlink: int = 1,
    device: int = 7,
    inode: int = 11,
    size: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=mode,
        st_uid=uid,
        st_gid=gid,
        st_nlink=nlink,
        st_dev=device,
        st_ino=inode,
        st_size=size,
    )


@pytest.mark.parametrize(
    "work_metadata",
    (
        _metadata(mode=0o120777, nlink=1),
        _metadata(mode=0o40750, nlink=2),
        _metadata(mode=0o40700, uid=1, nlink=2),
        _metadata(mode=0o40700, gid=1, nlink=2),
        _metadata(mode=0o40700, nlink=2, device=8),
    ),
)
def test_recovery_refuses_work_identity_drift(
    monkeypatch: pytest.MonkeyPatch, work_metadata: SimpleNamespace
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    parent = _metadata(mode=0o41777, nlink=2)

    def lstat(path: object) -> SimpleNamespace:
        value = Path(path)
        if value == Path("/var/tmp"):
            return parent
        if value == Path("/"):
            return _metadata(mode=0o40755, nlink=2)
        if value == work:
            return work_metadata
        raise AssertionError(value)

    monkeypatch.setattr(run.os, "lstat", lstat)
    with pytest.raises(run.HarnessError, match="work directory identity"):
        run._validate_work_identity(work)


def _recovery_document(work: Path) -> dict[str, object]:
    return {
        "schema_version": 2,
        "nonce": "123456789abc",
        "work": work.as_posix(),
        "ordinary_unit": "dashcamd.service",
        "phase": "PREPARED",
        "exclusion_owner": "11111111-1111-1111-1111-111111111111",
        "prior_exclusion_present": False,
        "owned_exclusion": None,
        "prior_unit": {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
            "NRestarts": "0",
            "FragmentPath": "/etc/systemd/system/dashcamd.service",
            "DropInPaths": "",
            "RefuseManualStart": "no",
            "Conditions": "",
        },
    }


@pytest.mark.parametrize(
    "journal_metadata",
    (
        _metadata(mode=0o120777, size=1),
        _metadata(mode=0o100640, size=1),
        _metadata(mode=0o100600, uid=1, size=1),
        _metadata(mode=0o100600, gid=1, size=1),
        _metadata(mode=0o100600, nlink=2, size=1),
        _metadata(mode=0o100600, device=8, size=1),
    ),
)
def test_recovery_journal_open_is_nofollow_and_refuses_identity_drift(
    monkeypatch: pytest.MonkeyPatch, journal_metadata: SimpleNamespace
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    payload = run.canonical_json(_recovery_document(work))
    journal_metadata.st_size = len(payload)
    work_metadata = _metadata(mode=0o40700, nlink=2, inode=31)
    opened: list[tuple[object, int, int | None]] = []
    reads = iter((payload, b""))

    monkeypatch.setattr(run, "_validate_work_identity", lambda _work: work_metadata)

    def open_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append((path, flags, kwargs.get("dir_fd")))
        return 10 if len(opened) == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda descriptor: work_metadata if descriptor == 10 else journal_metadata,
    )
    monkeypatch.setattr(run.os, "read", lambda _descriptor, _size: next(reads))
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match="journal content differs"):
        run._read_recovery_journal(work)

    assert opened[1][0] == "RECOVERY.json"
    assert opened[1][2] == 10
    if getattr(run.os, "O_NOFOLLOW", 0):
        assert opened[0][1] & run.os.O_NOFOLLOW
        assert opened[1][1] & run.os.O_NOFOLLOW


@pytest.mark.parametrize(
    "runtime_metadata",
    (
        _metadata(mode=0o120777),
        _metadata(mode=0o40700, nlink=2),
        _metadata(mode=0o40750, uid=1, gid=run.EXPECTED_API_GID, nlink=2),
        _metadata(mode=0o40750, uid=42, gid=1, nlink=2),
        _metadata(mode=0o40750, uid=42, gid=run.EXPECTED_API_GID, nlink=2, device=8),
    ),
)
def test_recovery_refuses_runtime_directory_identity_drift(
    monkeypatch: pytest.MonkeyPatch, runtime_metadata: SimpleNamespace
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")

    def lstat(path: object) -> SimpleNamespace:
        if Path(path) == Path("/run"):
            return _metadata(mode=0o40755, nlink=2)
        if Path(path) == runtime:
            return runtime_metadata
        raise AssertionError(path)

    monkeypatch.setattr(run.os, "lstat", lstat)
    with pytest.raises(run.HarnessError, match="runtime identity differs"):
        run._validate_runtime_recovery_identity(runtime, 42)


def test_intent_only_recovery_transitions_directly_to_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    transitions: list[tuple[str, str]] = []
    commands: list[tuple[str, ...]] = []
    prior = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
        "NRestarts": "0",
        "FragmentPath": "/etc/systemd/system/dashcamd.service",
        "DropInPaths": "",
        "RefuseManualStart": "no",
        "Conditions": "",
    }
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_service_properties", lambda _unit: dict(prior))
    monkeypatch.setattr(
        run,
        "_command",
        lambda arguments, **_kwargs: commands.append(tuple(str(value) for value in arguments)),
    )
    monkeypatch.setattr(
        run,
        "_transition_recovery_journal",
        lambda _work, expected, target, **_kwargs: transitions.append((expected, target)),
    )

    run._restore_recovery_exclusion(
        work,
        {"prior_unit": prior, "owned_exclusion": None},
        "EXCLUSION_INTENT",
        "123456789abc",
        False,
    )

    assert transitions == [("EXCLUSION_INTENT", "RESTORED")]
    assert commands == [("/usr/bin/systemctl", "daemon-reload")]


def test_owned_exclusion_swap_at_final_unlink_seam_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "directory_device": 7,
        "directory_inode": 11,
        "directory_uid": 0,
        "directory_gid": 0,
        "directory_mode": 0o755,
        "directory_nlink": 2,
        "file_inode": 12,
    }
    drifted = {**expected, "file_inode": 13}
    unlinked: list[str] = []
    descriptors = iter((9, 10))
    monkeypatch.setattr(run, "_open_exclusion_parent", lambda: next(descriptors))
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 11)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda descriptor: (
            _metadata(
                mode=0o40755,
                nlink=2,
                device=7,
                inode=11,
            )
            if descriptor == 11
            else _metadata(mode=0o40755, nlink=2, device=7, inode=1)
        ),
    )
    monkeypatch.setattr(run.os, "listdir", lambda _descriptor: [run.EXCLUSION_FILE_NAME])
    monkeypatch.setattr(run, "_exclusion_facts_at", lambda _descriptor, _nonce: drifted)
    monkeypatch.setattr(
        run.os,
        "unlink",
        lambda name, **_kwargs: unlinked.append(str(name)),
    )
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match="changed before removal"):
        run._remove_owned_runtime_exclusion("123456789abc", expected)

    assert unlinked == []


def test_runtime_cleanup_reopens_exact_directory_through_run_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    opened: list[tuple[object, int | None]] = []
    unlinked: list[tuple[str, int | None]] = []
    removed: list[tuple[str, int | None]] = []
    run_metadata = _metadata(mode=0o40755, nlink=2, inode=31)
    runtime_metadata = _metadata(
        mode=0o40750,
        uid=42,
        gid=run.EXPECTED_API_GID,
        nlink=2,
        inode=32,
    )
    member_metadata = _metadata(
        mode=0o100600,
        uid=42,
        gid=run.EXPECTED_API_GID,
        inode=33,
    )

    def open_file(path: object, _flags: int, **kwargs: object) -> int:
        opened.append((path, kwargs.get("dir_fd")))
        return 10 if len(opened) == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda descriptor: run_metadata if descriptor == 10 else runtime_metadata,
    )
    monkeypatch.setattr(run.os, "listdir", lambda descriptor: ["status.json"])
    monkeypatch.setattr(
        run.os,
        "stat",
        lambda _name, **_kwargs: member_metadata,
    )
    monkeypatch.setattr(
        run.os,
        "unlink",
        lambda name, **kwargs: unlinked.append((str(name), kwargs.get("dir_fd"))),
    )
    monkeypatch.setattr(
        run.os,
        "rmdir",
        lambda name, **kwargs: removed.append((str(name), kwargs.get("dir_fd"))),
    )
    monkeypatch.setattr(run.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    assert run._cleanup_runtime_recovery_directory(runtime, 42) is True
    assert opened == [(Path("/run"), None), (runtime.name, 10)]
    assert unlinked == [("status.json", 11)]
    assert removed == [(runtime.name, 10)]


def test_ambiguous_systemd_launch_reconciles_deterministic_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[str] = []
    monkeypatch.setattr(
        run,
        "_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(run.HarnessError("lost reply")),
    )
    monkeypatch.setattr(run, "_remove_unit", removed.append)
    unit = "dashcam-m10-private-123456789abc-b.service"

    with pytest.raises(run.HarnessError, match="lost reply"):
        run._systemd_run(unit, (), ("/bin/false",))

    assert removed == [unit]


@pytest.mark.parametrize(
    "timeout",
    (True, 0.0, -1.0, float("nan"), float("inf"), 50.001),
)
def test_systemd_run_refuses_invalid_or_unbounded_client_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
) -> None:
    monkeypatch.setattr(
        run,
        "_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must refuse first")),
    )

    with pytest.raises(run.HarnessError, match="client timeout differs"):
        run._systemd_run(
            "dashcam-m10-private-123456789abc-bind.service",
            (),
            ("/bin/true",),
            client_timeout_s=timeout,
        )


def test_notify_launch_admits_simulated_21_second_reply_while_bind_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []
    removed: list[str] = []

    def command(_arguments: object, *, timeout: float) -> None:
        observed.append(timeout)
        if timeout <= 21:
            raise run.HarnessError("simulated notify reply arrived at 21 seconds")

    monkeypatch.setattr(run, "_command", command)
    monkeypatch.setattr(run, "_remove_unit", removed.append)
    bind_unit = "dashcam-m10-private-123456789abc-bind.service"
    notify_unit = "dashcam-m10-private-123456789abc-a.service"

    with pytest.raises(run.HarnessError, match="arrived at 21 seconds"):
        run._systemd_run(bind_unit, (), ("/bin/true",))
    run._systemd_run(
        notify_unit,
        (),
        ("/bin/true",),
        client_timeout_s=run.SYSTEMD_RUN_NOTIFY_TIMEOUT_S,
    )

    assert observed == [20.0, 50.0]
    assert removed == [bind_unit]


def test_systemd_run_no_block_is_explicit_and_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        run,
        "_command",
        lambda arguments, **_kwargs: observed.append(tuple(str(value) for value in arguments)),
    )
    unit = "dashcam-m10-private-123456789abc-b.service"

    run._systemd_run(unit, (), ("/bin/true",), no_block=True)

    assert observed == [
        (
            "/usr/bin/systemd-run",
            "--no-block",
            "--unit",
            "dashcam-m10-private-123456789abc-b",
            "--",
            "/bin/true",
        )
    ]
    with pytest.raises(run.HarnessError, match="no-block mode differs"):
        run._systemd_run(unit, (), ("/bin/true",), no_block=1)  # type: ignore[arg-type]


def test_candidate_unit_maps_only_notify_roles_to_50_second_client_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, float]] = []

    def systemd_run(
        unit: str,
        _properties: object,
        _command: object,
        *,
        client_timeout_s: float,
        no_block: bool,
    ) -> None:
        assert no_block is False
        observed.append((unit, client_timeout_s))

    monkeypatch.setattr(run, "_systemd_run", systemd_run)
    paths = {
        "recording": Path("/var/tmp/dashcam-m10-private.123456789abc/recording"),
        "state": Path("/var/tmp/dashcam-m10-private.123456789abc/state"),
        "runtime": Path("/run/dashcam-m10-private.123456789abc"),
    }

    run._candidate_unit("123456789abc", "a", paths, role="candidate")
    run._candidate_unit(
        "123456789abc",
        "rollback3",
        paths,
        role="rollback-recorder",
    )

    assert observed == [
        ("dashcam-m10-private-123456789abc-a.service", 50.0),
        ("dashcam-m10-private-123456789abc-rollback3.service", 50.0),
    ]


def test_bind_and_rollback_recovery_use_role_appropriate_client_timeouts() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    bind = source[source.index("def _run_bind_probe(") : source.index("def _source_environment(")]
    rollback = source[
        source.index("def _rollback_phase(") : source.index("def _protected_emergency_phase(")
    ]

    assert bind.count("client_timeout_s=SYSTEMD_RUN_CLIENT_TIMEOUT_S") == 1
    assert rollback.count("client_timeout_s=SYSTEMD_RUN_NOTIFY_TIMEOUT_S") == 1


def test_source_has_owned_loop_cleanup_and_no_broad_destructive_actions() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    for token in (
        "_loop_backing(loop).resolve()",
        "refusing to detach a foreign loop",
        "refusing to unmount a foreign target",
        "root_budget_satisfied",
        "MAX_NON_IMAGE_ROOT_DELTA_BYTES",
        "post-cleanup root reserve",
        "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
        '"rw,nosuid,nodev,noexec,noatime,nodiscard"',
        "owned private mount remained busy after its retry bound",
        "UNMOUNT_RETRY_LIMIT: Final = 600",
        "_drain_private_units(nonce)",
        "_require_dense_image",
        "_require_owned_loop",
    ):
        assert token in source
    for forbidden in (
        "rm -rf",
        "mkfs.exfat /dev/mmc",
        "timedatectl",
        "apt-get",
        "systemctl enable",
    ):
        assert forbidden not in source


def test_fixture_cleanup_sweeps_all_nonce_units_before_unmount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    paths = {
        "recording": work / "recording",
        "state": work / "state",
        "recording_loop": Path("/dev/loop1"),
        "state_loop": Path("/dev/loop2"),
        "recording_image": work / "recording.exfat.img",
        "state_image": work / "state.ext4.img",
    }
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        run,
        "_drain_private_units",
        lambda nonce: observed.append(("drain", nonce)),
    )
    monkeypatch.setattr(run, "_require_mount", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run,
        "_unmount",
        lambda target, loop: observed.append(("unmount", (target, loop))),
    )
    monkeypatch.setattr(run, "_detach", lambda *args: None)

    run._cleanup_fixture(paths)

    assert observed[0] == ("drain", "123456789abc")
    assert [entry[0] for entry in observed] == ["drain", "unmount", "unmount"]


def test_fixture_mounts_are_made_private_with_exact_propagation_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state"
    target.mkdir()
    loop = Path("/dev/loop2")
    monkeypatch.setattr(
        run,
        "_findmnt",
        lambda _target: {
            "source": loop.as_posix(),
            "target": target.resolve().as_posix(),
        },
    )
    observed: list[tuple[str, ...]] = []

    def command(arguments: tuple[object, ...]) -> SimpleNamespace:
        observed.append(tuple(str(value) for value in arguments))
        if arguments[0] == "/usr/bin/mount":
            return SimpleNamespace(stdout=b"", stderr=b"")
        return SimpleNamespace(stdout=b"private\n", stderr=b"")

    monkeypatch.setattr(run, "_command", command)

    run._make_mount_private(target, loop)

    assert observed == [
        ("/usr/bin/mount", "--make-private", str(target)),
        (
            "/usr/bin/findmnt",
            "--noheadings",
            "--output",
            "PROPAGATION",
            "--mountpoint",
            str(target),
        ),
    ]


def test_parent_media_parser_imports_from_frozen_bundle_not_state_mount() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    phase_a = source[source.index("def _phase_a(") : source.index("def _rollback_phase(")]

    assert '_source_environment(root.parent / "bundle" / "candidate-source.zip")' in phase_a
    assert '_source_environment(state / "candidate-source.zip")' not in phase_a


def test_pre_ready_refusal_phases_launch_notify_units_nonblocking() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    protected = source[
        source.index("def _protected_emergency_phase(") : source.index("def _startup_bound_phase(")
    ]
    bounded = source[
        source.index("def _startup_bound_phase(") : source.index("def _fresh_phase(")
    ]
    candidate = source[source.index("def _candidate_unit(") : source.index("def _stop_clean(")]

    assert protected.count("no_block=True") == 1
    assert bounded.count("no_block=True") == 1
    assert "no_block=no_block" in candidate


def test_readme_states_private_scope_and_all_nonclaims() -> None:
    readme = (HARNESS / "README.md").read_text(encoding="utf-8")

    for text in (
        "production catalog or recording volume",
        "2,701,131,776",
        "runtime drop-in",
        "65-DELETE backlog",
        "exactly 64",
        "HTTP/download data plane",
        "physical GPS",
        "physical microphone",
        "camera-generated FINALIZING",
        "No media, coordinates, raw NMEA",
        "--recover-work",
        "atomically no-replace",
        "observes the production catalog/sentinel",
    ):
        assert text in readme


def test_result_claims_remain_explicit_and_false_for_unexercised_surfaces() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    for claim in (
        '"camera_generated_finalizing_overlap_tested": False',
        '"integrated_finalizing_startup_exclusion_tested": False',
        '"download_data_plane_tested": False',
        '"http_or_ui_tested": False',
        '"physical_gps_tested": False',
        '"physical_audio_tested": False',
        '"physical_power_loss_tested": False',
    ):
        assert claim in source
    assert '"conditions_property": "[unprintable]"' in source
    assert '"conditions_property_parsed": False' in source
    assert '"findmnt_active_row_adapter_used": True' in source


def test_phase_a_startup_order_refusals_remain_distinct_reviewed_lines() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    messages = (
        "startup reclaim produced no durable DELETE completion",
        "startup reclaim deleted an excluded clip",
        "startup FINALIZE intent was not observed",
        "startup FINALIZE intent did not complete",
        "startup intent completion timestamps differ",
        "startup DELETE did not follow FINALIZE convergence",
    )
    observed = [
        index + 1
        for index, line in enumerate(source.splitlines())
        if any(message in line for message in messages)
    ]

    assert len(observed) == len(messages)
    assert len(set(observed)) == len(messages)
    assert set(observed).issubset(run._reviewed_function_lines()["phase_a"])


def test_phase_a_records_both_startup_reclaim_pass_counts() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert '"startup_finalize_before_reclaim": True' in source
    assert '"startup_delete_before_finalize_count": 0' in source
    assert '"startup_delete_after_finalize_count": len(completed_deletes)' in source


def test_canonical_media_refusals_use_the_reviewed_function_lines() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    messages = (
        "canonical sidecar video summary differs",
        "canonical sidecar clip identity differs",
        "canonical sidecar start binding differs",
        "canonical sidecar end binding differs",
        "canonical sidecar protection binding differs",
        "canonical sidecar filename binding differs",
        "canonical sidecar video profile differs",
        "canonical sidecar frame counter differs",
        "canonical sidecar warnings shape differs",
        "canonical sidecar drop counter shape differs",
        "canonical sidecar drop sentinel contradicts its warning",
        "canonical sidecar recorded dropped frames",
        "finalized media catalog path differs",
        "finalized media member type differs",
        "finalized media device differs",
        "finalized media size differs",
        "finalized media lifecycle differs",
        "finalized media ownership differs",
        "finalized media reconciliation differs",
    )
    observed = [
        index + 1
        for index, line in enumerate(source.splitlines())
        if any(message in line for message in messages)
    ]

    assert len(observed) == len(messages)
    assert len(set(observed)) == len(messages)
    assert set(observed).issubset(run._reviewed_function_lines()["canonical_media_row"])


def test_canonical_media_accepts_production_sidecar_without_result_newline(
    tmp_path: Path,
) -> None:
    _clip, paths, sidecar = run._finalizing_fixture(22, 13)
    sidecar_path = tmp_path / PurePosixPath(paths.sidecar_target)
    video_path = tmp_path / PurePosixPath(paths.video_target)
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_bytes(sidecar)
    video_path.write_bytes(b"M10-PRIVATE\n")
    row = {
        "clip_id": str(run.UUID(int=23)),
        "lifecycle": "FINALIZED",
        "video_path": paths.video_target,
        "sidecar_path": paths.sidecar_target,
        "protected": False,
        "start_monotonic_ns": 22_000_000_000,
        "end_monotonic_ns": 23_000_000_000,
        "size_bytes": len(b"M10-PRIVATE\n"),
        "managed": True,
        "pair_reconciled": True,
    }

    observed = run._canonical_media_row(tmp_path, row)

    assert not sidecar.endswith(b"\n")
    assert observed["clip_id"] == row["clip_id"]
    assert observed["frames_written"] == 30
    assert observed["sidecar_drop_counter_available"] is True
    assert observed["sidecar_dropped_frames"] == 0


def test_media_evidence_never_turns_unavailable_sidecar_drop_count_into_zero() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert '"sidecar_drop_counter_available": not counter_warning' in source
    assert '0 if row.get("sidecar_drop_counter_available") is True else None' in source
    assert "MEDIA_DECODE_TIMEOUT_S: Final = 75" in source
    assert "timeout=MEDIA_DECODE_TIMEOUT_S" in source


def test_each_live_crossing_starts_in_a_fresh_writing_interval() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    phase = source[source.index("def _phase_a(") : source.index("def _rollback_phase(")]

    assert phase.count("_wait_fresh_writing(catalog, root)") == 3
    assert "metadata.st_size <= maximum_size_bytes" in source
    assert "metadata.st_dev == root_device" in source
    assert "maximum_size_bytes: int = 16 * 1024**2" in source
    assert "timeout: float = 95" in source
    assert 'if len(writing) > 2:' in source
    assert 'if len(early) > 1:' in source
    assert "_wait_listener_candidates(catalog, root)" in phase
    assert 'row["pair_reconciled"] is True' in source
    assert 'row["managed"] is True' in source


def test_fresh_writing_selection_accepts_one_small_successor_during_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    clips = root / "clips"
    clips.mkdir(parents=True)
    old_video = clips / "old.mp4"
    new_video = clips / "new.mp4"
    old_video.touch()
    with old_video.open("r+b") as stream:
        stream.truncate(17 * 1024**2)
    new_video.write_bytes(b"new")
    old = {
        "clip_id": "00000000-0000-4000-8000-000000000001",
        "lifecycle": "WRITING",
        "video_path": "clips/old.mp4",
        "video_present": True,
    }
    new = {
        "clip_id": "00000000-0000-4000-8000-000000000002",
        "lifecycle": "WRITING",
        "video_path": "clips/new.mp4",
        "video_present": True,
    }
    monkeypatch.setattr(run, "_query_catalog", lambda _catalog, _root: {"clips": [old, new]})

    assert run._wait_fresh_writing(tmp_path / "catalog.sqlite3", root) == new


def test_rollback_guard_report_schema_is_closed_to_the_companion_contract() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    rollback = source[
        source.index("def _rollback_phase(") : source.index("def _protected_emergency_phase(")
    ]

    for field in (
        '"actions_attempted"',
        '"high_free_bytes"',
        '"catalog_schema"',
        '"finalized_clips_examined"',
    ):
        assert field in rollback
    assert 'guard.get("catalog_schema") != 5' in rollback
    assert "<= MAX_FIXTURE_ROWS" in rollback


def test_runtime_health_refusals_are_distinct_reviewed_lines() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    messages = (
        "runtime aggregate drop counter shape differs",
        "runtime recorded aggregate dropped frames",
        "runtime raw frame progress differs",
        "runtime encoded frame progress differs",
        "runtime drop counter source differs",
        "runtime pipeline restart count differs",
        "runtime overlay reported an error",
        "runtime renderer reported an error",
        "runtime renderer update rejection count differs",
        "runtime renderer contract mismatch count differs",
        "runtime renderer transform failure count differs",
        "runtime renderer mapping rejection count differs",
        "runtime renderer synchronization failure count differs",
    )
    observed = [
        index + 1
        for index, line in enumerate(source.splitlines())
        if any(message in line for message in messages)
    ]

    assert len(observed) == len(messages)
    assert len(set(observed)) == len(messages)
    assert set(observed).issubset(run._reviewed_function_lines()["runtime_health"])


def test_runtime_health_wait_only_retries_unavailable_drop_shape() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    wait = source[source.index("def _wait_runtime_health(") : source.index("def _phase_a(")]

    assert "timeout: float = 10" in wait
    assert "isinstance(dropped, int) and not isinstance(dropped, bool)" in wait
    assert "return status, _runtime_health(status)" in wait
    assert "return last_status, _runtime_health(last_status)" in wait


def test_control_wrapper_is_not_a_reviewed_diagnostic_location() -> None:
    assert "raw_control" not in run.DIAGNOSTIC_FUNCTIONS
    assert "raw_control" not in run._reviewed_function_lines()


def test_download_release_retries_only_one_ambiguous_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def release(_runtime: Path, command: str, arguments: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert command == "release_download"
        assert arguments == {"clip_id": "clip", "lease_id": "lease"}
        if calls == 1:
            raise run.HarnessError("private response")
        return {"clip_id": "clip", "released": False}

    monkeypatch.setattr(run, "_raw_control", release)

    assert run._release_download_converged(
        Path("runtime"),
        clip_id="clip",
        lease_id="lease",
    ) == {"clip_id": "clip", "released": False}
    assert calls == 2


def test_download_release_requires_exact_convergence_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run,
        "_raw_control",
        lambda *_args, **_kwargs: {"clip_id": "other", "released": True},
    )

    with pytest.raises(run.HarnessError, match="convergence response"):
        run._release_download_converged(
            Path("runtime"),
            clip_id="clip",
            lease_id="lease",
        )


def test_rollback_output_is_opened_inside_private_namespace() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert 'p.add_argument("--output")' in source
    assert 'f"/run/dashcam/{output.name}"' in source
    assert "StandardOutput=file:" not in source


def test_publication_contract_is_exact_atomic_and_durable() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert 'RESULT_RE: Final = re.compile(r"m10-private-runtime-' in source
    publish = source[source.index("def _publish_result(") : source.index("def _space(")]
    assert "os.link(" in publish
    assert "follow_symlinks=False" in publish
    assert publish.count("os.fsync(directory)") >= 2
    assert "os.unlink(path.name" in publish


def test_second_publication_directory_fsync_failure_removes_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result.json"
    entries: set[str] = set()
    fsync_calls = 0

    def write(path: Path, _payload: bytes, _mode: int) -> None:
        entries.add(path.name)

    def link(source: str, target: str, **_kwargs: object) -> None:
        assert source in entries
        entries.add(target)

    def unlink(name: str, **_kwargs: object) -> None:
        if name not in entries:
            raise FileNotFoundError(name)
        entries.remove(name)

    def fsync(_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected second directory fsync failure")

    monkeypatch.setattr(run, "_validate_result_destination", lambda path: path)
    monkeypatch.setattr(run, "_write_exclusive", write)
    monkeypatch.setattr(run, "uuid4", lambda: SimpleNamespace(hex="1" * 32))
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "link", link)
    monkeypatch.setattr(run.os, "unlink", unlink)
    monkeypatch.setattr(run.os, "fsync", fsync)
    monkeypatch.setattr(
        run.os,
        "stat",
        lambda _name, **_kwargs: SimpleNamespace(st_dev=1, st_ino=9),
    )

    with pytest.raises(OSError, match="second directory"):
        run._publish_result(destination, b"complete\n")

    assert destination.name not in entries


def test_recovery_journal_phase_transition_is_durable_and_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "dashcam-m10-private.123456789abc"
    work.mkdir()
    document = {
        "schema_version": 2,
        "nonce": "123456789abc",
        "work": work.as_posix(),
        "ordinary_unit": "dashcamd.service",
        "phase": "PREPARED",
        "exclusion_owner": "11111111-1111-1111-1111-111111111111",
        "prior_exclusion_present": False,
        "owned_exclusion": None,
        "prior_unit": {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
            "NRestarts": "0",
            "FragmentPath": "/etc/systemd/system/dashcamd.service",
            "DropInPaths": "",
            "RefuseManualStart": "no",
            "Conditions": "",
        },
    }
    (work / "RECOVERY.json").write_bytes(run.canonical_json(document))
    monkeypatch.setattr(run, "_read_recovery_journal", lambda _work: dict(document))
    monkeypatch.setattr(
        run, "_write_exclusive", lambda path, payload, _mode: path.write_bytes(payload)
    )
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(run.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    run._transition_recovery_journal(work, "PREPARED", "EXCLUSION_INTENT")

    assert (
        run._strict_json((work / "RECOVERY.json").read_bytes(), "test")["phase"]
        == "EXCLUSION_INTENT"
    )


def test_failure_cleanup_stays_inside_exclusion_scope() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    qualify = source[source.index("def qualify(") : source.index("def _parser(")]

    assert qualify.index("with _runtime_exclusion(work) as exclusion_state") < qualify.index(
        "_cleanup_fixture(paths)"
    )
    assert qualify.index("_cleanup_fixture(paths)") < qualify.index(
        'exclusion_state["restore_authorized"] = True'
    )


def test_refusal_location_accepts_only_reviewed_function_and_executable_line() -> None:
    lines = run._reviewed_function_lines()
    valid_line = min(lines["host_snapshot"])
    accepted = f"REFUSED: H_OS_Fhost_snapshot_L{valid_line}\n".encode("ascii")

    assert run._validated_refusal_location(accepted) == accepted

    for refused in (
        accepted.removesuffix(b"\n"),
        accepted + b"private second line\n",
        f"REFUSED: H_OS_Fhost_snapshot_L{max(lines['host_snapshot']) + 1}\n".encode(
            "ascii"
        ),
        f"REFUSED: H_OS_Fnot_reviewed_L{valid_line}\n".encode("ascii"),
        f"REFUSED: H_PRIVATE_Fhost_snapshot_L{valid_line}\n".encode("ascii"),
        f"REFUSED: H_OS_Fhost_snapshot_L0{valid_line}\n".encode("ascii"),
        b"REFUSED: SSID MyHome PSK hunter2 /private/path\n",
    ):
        assert run._validated_refusal_location(refused) is None


def test_refusal_line_skips_command_frame_for_exact_reviewed_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = run.subprocess.CompletedProcess(
        ["/private/tool", "--token", "abcdef"],
        9,
        stdout=b"SSID MyHome coordinates 32.1,34.8",
        stderr=b"PSK hunter2 /private/path",
    )
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: completed)
    try:
        run._findmnt(Path("/"))
    except run.HarnessError as error:
        traceback = error.__traceback__
        frames: list[tuple[str, int]] = []
        while traceback is not None:
            frames.append((traceback.tb_frame.f_code.co_name, traceback.tb_lineno))
            traceback = traceback.tb_next
        caller_line = next(line for name, line in frames if name == "_findmnt")
        payload = run._refusal_line(error)
    else:
        pytest.fail("nonzero command unexpectedly succeeded")

    assert any(name == "_command" for name, _line in frames)
    assert "command" not in run._reviewed_function_lines()
    assert payload == f"REFUSED: H_HARNESS_Ffindmnt_L{caller_line}\n".encode("ascii")
    assert run._validated_refusal_location(payload) == payload
    for private in (
        b"MyHome",
        b"hunter2",
        b"32.1",
        b"34.8",
        b"/private",
        b"abcdef",
        b"returncode",
        b"stdout",
        b"stderr",
    ):
        assert private not in payload


def test_refusal_line_skips_command_and_systemd_wrappers_for_bind_probe_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = run.subprocess.CompletedProcess(
        ["/usr/bin/systemd-run", "--property", "Environment=PSK=hunter2"],
        7,
        stdout=b"SSID MyHome coordinates 32.1,34.8",
        stderr=b"token abcdef /private/systemd/path",
    )
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=42, pw_gid=43)),
    )
    monkeypatch.setattr(run, "_write_exclusive", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        run,
        "render_transient_properties",
        lambda **_kwargs: ("Environment=PRIVATE_TOKEN=abcdef",),
    )
    monkeypatch.setattr(run, "_remove_unit", lambda _unit: None)
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: completed)
    paths = {
        "state": Path("/private/state"),
        "runtime": Path("/private/runtime"),
        "recording": Path("/private/recording"),
    }

    try:
        run._run_bind_probe("123456789abc", paths)
    except run.HarnessError as error:
        traceback = error.__traceback__
        frames: list[tuple[str, int]] = []
        while traceback is not None:
            frames.append((traceback.tb_frame.f_code.co_name, traceback.tb_lineno))
            traceback = traceback.tb_next
        caller_line = next(line for name, line in frames if name == "_run_bind_probe")
        payload = run._refusal_line(error)
    else:
        pytest.fail("nonzero systemd-run unexpectedly succeeded")

    frame_names = {name for name, _line in frames}
    assert {"_run_bind_probe", "_systemd_run", "_command"} <= frame_names
    reviewed = run._reviewed_function_lines()
    assert "systemd_run" not in reviewed
    assert "command" not in reviewed
    assert payload == f"REFUSED: H_HARNESS_Frun_bind_probe_L{caller_line}\n".encode("ascii")
    assert run._validated_refusal_location(payload) == payload
    for private in (
        b"MyHome",
        b"hunter2",
        b"32.1",
        b"34.8",
        b"abcdef",
        b"/private",
        b"Environment",
        b"returncode",
        b"stdout",
        b"stderr",
    ):
        assert private not in payload


def _launch_status_payload(
    state: str,
    reason: str | None,
    *,
    runtime: object | None = None,
) -> bytes:
    return run.canonical_json(
        {
            "schema_version": 3,
            "lifecycle": {
                "state": state,
                "reason": reason,
                "detail": "SSID MyHome PSK hunter2 /private/path token abcdef",
                "sequence": 7,
                "config_schema_version": 1,
                "notification_failures": 0,
            },
            "runtime": (
                {
                    "secret": "coordinates 32.1,34.8",
                    "path": "/srv/dashcam/private.mp4",
                }
                if runtime is None
                else runtime
            ),
        }
    )


def _launch_status_refusal(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> bytes:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    status = runtime / "status.json"
    observations: list[tuple[Path, int]] = []
    monkeypatch.setattr(run.Path, "is_file", lambda self: self == status)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)

    def bounded(path: Path, maximum: int) -> bytes:
        observations.append((path, maximum))
        return payload

    monkeypatch.setattr(run, "_bounded_read", bounded)
    try:
        run._phase_a_launch_failure_status(runtime)
    except run.HarnessError as error:
        refusal = run._refusal_line(error)
    else:
        pytest.fail("launch-failure status callback unexpectedly returned")
    assert observations == [(status, run.RUNTIME_STATUS_BYTES)]
    assert refusal.startswith(b"REFUSED: H_HARNESS_Fphase_a_launch_failure_status_L")
    assert run._validated_refusal_location(refusal) == refusal
    for private in (
        b"MyHome",
        b"hunter2",
        b"/private",
        b"abcdef",
        b"32.1",
        b"34.8",
        b"/srv",
        b"detail",
        b"runtime",
        b"reason",
    ):
        assert private not in refusal
    return refusal


def test_phase_a_launch_failure_has_unique_safe_line_for_every_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run.RECORDER_REASONS == (
        "CONFIG_ERROR",
        "STARTUP_FAILED",
        "STARTUP_TIMEOUT",
        "RUNTIME_EXITED",
        "RUNTIME_FAILED",
        "PIPELINE_RECOVERING",
        "PIPELINE_RECOVERY_EXHAUSTED",
        "PIPELINE_NO_PROGRESS",
        "FINALIZATION_FAILED",
        "STORAGE_FAULT",
        "OPTIONAL_SUBSYSTEM",
        "SHUTDOWN_FAILED",
        "SHUTDOWN_TIMEOUT",
    )
    refusals = {
        _launch_status_refusal(monkeypatch, _launch_status_payload("FAULTED", reason))
        for reason in run.RECORDER_REASONS
    }
    assert len(refusals) == len(run.RECORDER_REASONS)


def test_phase_a_launch_failure_has_unique_safe_line_for_every_reasonless_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusals = {
        _launch_status_refusal(monkeypatch, _launch_status_payload(state, None))
        for state in run.RECORDER_STATES
    }
    assert len(refusals) == len(run.RECORDER_STATES)


def test_phase_a_launch_failure_missing_status_is_fixed_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    monkeypatch.setattr(run.Path, "is_file", lambda _self: False)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)

    try:
        run._phase_a_launch_failure_status(runtime)
    except run.HarnessError as error:
        refusal = run._refusal_line(error)
    else:
        pytest.fail("missing launch status unexpectedly returned")

    assert refusal.startswith(b"REFUSED: H_HARNESS_Fphase_a_launch_failure_status_L")
    assert run._validated_refusal_location(refusal) == refusal
    assert b"/run" not in refusal
    assert b"status.json" not in refusal


def test_phase_a_launch_failure_malformed_and_unknown_values_have_closed_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        run.canonical_json(
            {
                "schema_version": 2,
                "lifecycle": {"detail": "PSK hunter2 /private/path"},
                "runtime": {"secret": "abcdef"},
            }
        ),
        _launch_status_payload("PRIVATE_UNKNOWN_STATE", None),
        _launch_status_payload("FAULTED", "PRIVATE_UNKNOWN_REASON"),
    )
    refusals = {_launch_status_refusal(monkeypatch, payload) for payload in cases}
    assert len(refusals) == len(cases)
    for refusal in refusals:
        for private in (b"PRIVATE", b"hunter2", b"/private", b"abcdef"):
            assert private not in refusal


def _storage_preflight_status(*reasons: str) -> dict[str, object]:
    if not reasons:
        state = "READY"
        ready = True
    elif any(
        reason not in {"READ_ONLY", "CONFLICTING_MOUNT_OPTIONS", "RESERVE_EXHAUSTED"}
        for reason in reasons
    ):
        state = "FAULTED"
        ready = False
    elif any(reason in {"READ_ONLY", "CONFLICTING_MOUNT_OPTIONS"} for reason in reasons):
        state = "READ_ONLY"
        ready = False
    else:
        state = "EMERGENCY"
        ready = False
    return {
        "state": state,
        "reasons": list(reasons),
        "ready": ready,
        "mount": {
            "target": "/srv/dashcam/private-secret",
            "uuid_suffix": "secret-uuid",
            "device_id": "secret-device",
            "token": "abcdef",
        },
        "free_bytes": 32_134_800,
        "capacity_bytes": 987_654_321,
    }


def _storage_retention_status(
    *,
    mode: str | None = "NORMAL",
    fault: str | None = None,
    trigger: object = None,
    stop_required: bool = False,
) -> dict[str, object]:
    return {
        "sequence": 7,
        "mode": mode,
        "fault": fault,
        "trigger": trigger,
        "stale": False,
        "stop_required": stop_required,
        "reclaimer_enabled": True,
        "consecutive_observation_failures": 0,
        "sample_age_ns": 1,
        "volume_uuid_suffix": "secret-uuid",
        "device_id": "secret-device",
        "capacity_bytes": 987_654_321,
        "free_bytes": 32_134_800,
        "free_percent": 3.25,
        "thresholds": {"path": "/private/threshold", "token": "abcdef"},
        "directive": {"coordinates": "32.1,34.8", "psk": "hunter2"},
    }


def _storage_fault_status_payload(
    *,
    preflight: object = ...,
    retention: object = ...,
    include_preflight: bool = True,
    include_retention: bool = True,
    detail: str = "SSID MyHome PSK hunter2 /private/path token abcdef",
) -> bytes:
    runtime: dict[str, object] = {
        "private_path": "/private/runtime",
        "ssid": "MyHome",
        "psk": "hunter2",
        "token": "abcdef",
        "coordinates": "32.1,34.8",
    }
    if include_preflight:
        runtime["storage_preflight"] = (
            _storage_preflight_status() if preflight is ... else preflight
        )
    if include_retention:
        runtime["storage_retention"] = (
            _storage_retention_status() if retention is ... else retention
        )
    return run.canonical_json(
        {
            "schema_version": 3,
            "lifecycle": {
                "state": "FAULTED",
                "reason": "STORAGE_FAULT",
                "detail": detail,
                "sequence": 7,
                "config_schema_version": 1,
                "notification_failures": 0,
            },
            "runtime": runtime,
        }
    )


def test_storage_fault_diagnostic_has_unique_safe_line_for_every_preflight_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run.PREFLIGHT_REASONS == (
        "MALFORMED_FACTS",
        "WRONG_TARGET",
        "UNMOUNTED",
        "MISSING_MOUNT_IDENTITY",
        "ROOTFS_ALIAS",
        "WRONG_FILESYSTEM",
        "WRONG_LABEL",
        "WRONG_UUID",
        "WRONG_UUID_SUFFIX",
        "READ_ONLY",
        "CONFLICTING_MOUNT_OPTIONS",
        "MISSING_SENTINEL",
        "WRONG_SENTINEL_VERSION",
        "WRONG_SENTINEL_IDENTITY",
        "WRONG_SENTINEL_UUID",
        "INVALID_SENTINEL_GEOMETRY",
        "INVALID_SPACE",
        "INSUFFICIENT_CAPACITY",
        "RESERVE_EXHAUSTED",
        "WRITE_PROBE_FAILED",
    )
    refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(preflight=_storage_preflight_status(reason)),
        )
        for reason in run.PREFLIGHT_REASONS
    }
    assert len(refusals) == len(run.PREFLIGHT_REASONS)


def test_storage_fault_diagnostic_has_unique_safe_line_for_every_retention_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run.SPACE_OBSERVATION_FAULTS == (
        "OBSERVATION_FAILED",
        "INVALID_OBSERVATION",
        "OBSERVATION_STALE",
        "IDENTITY_DRIFT",
        "CAPACITY_DRIFT",
        "LATCH_LOAD_FAILED",
        "LATCH_BINDING_MISMATCH",
        "LATCH_STORE_FAILED",
        "NO_SPACE_WRITE",
    )
    refusals: set[bytes] = set()
    for fault in run.SPACE_OBSERVATION_FAULTS:
        no_space = fault == "NO_SPACE_WRITE"
        stop_required = fault not in {"OBSERVATION_FAILED", "INVALID_OBSERVATION"}
        refusals.add(
            _launch_status_refusal(
                monkeypatch,
                _storage_fault_status_payload(
                    retention=_storage_retention_status(
                        mode="EMERGENCY" if no_space else None,
                        fault=fault,
                        trigger="NO_SPACE_WRITE" if no_space else None,
                        stop_required=stop_required,
                    )
                ),
            )
        )
    assert len(refusals) == len(run.SPACE_OBSERVATION_FAULTS)


def test_storage_fault_diagnostic_accepts_normal_mode_with_production_fault_polarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transient = ("OBSERVATION_FAILED", "INVALID_OBSERVATION")
    latched = (
        "IDENTITY_DRIFT",
        "CAPACITY_DRIFT",
        "LATCH_LOAD_FAILED",
        "LATCH_BINDING_MISMATCH",
        "LATCH_STORE_FAILED",
    )
    transient_refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(
                retention=_storage_retention_status(
                    mode="NORMAL",
                    fault=fault,
                    stop_required=False,
                )
            ),
        )
        for fault in transient
    }
    latched_refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(
                retention=_storage_retention_status(
                    mode="NORMAL",
                    fault=fault,
                    stop_required=True,
                )
            ),
        )
        for fault in latched
    }

    assert len(transient_refusals) == len(transient)
    assert len(latched_refusals) == len(latched)
    assert transient_refusals.isdisjoint(latched_refusals)


def test_storage_fault_diagnostic_has_closed_no_fault_stop_and_mode_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run.RETENTION_MODES == ("NORMAL", "RECLAIMING", "EMERGENCY")
    shapes = [
        _storage_retention_status(mode=None),
        *(_storage_retention_status(mode=mode) for mode in run.RETENTION_MODES),
        _storage_retention_status(mode="EMERGENCY", stop_required=True),
    ]
    refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(retention=retention),
        )
        for retention in shapes
    }
    assert len(refusals) == len(shapes)


def test_storage_fault_diagnostic_missing_and_malformed_shapes_are_fixed_and_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_preflight = _storage_preflight_status()
    malformed_preflight["private_extra"] = "hunter2"
    malformed_retention = _storage_retention_status(trigger={"secret": "hunter2"})
    cases = (
        _launch_status_payload(
            "FAULTED",
            "STORAGE_FAULT",
            runtime=["hunter2", "/private/runtime", "32.1,34.8"],
        ),
        _storage_fault_status_payload(include_preflight=False),
        _storage_fault_status_payload(preflight=malformed_preflight),
        _storage_fault_status_payload(include_retention=False),
        _storage_fault_status_payload(retention=malformed_retention),
    )
    refusals = {_launch_status_refusal(monkeypatch, payload) for payload in cases}
    assert len(refusals) == len(cases)
    for refusal in refusals:
        for private in (
            b"MyHome",
            b"hunter2",
            b"/private",
            b"abcdef",
            b"32.1",
            b"34.8",
            b"secret-device",
            b"secret-uuid",
            b"32134800",
            b"987654321",
        ):
            assert private not in refusal


def test_storage_fault_diagnostic_splits_exact_preflight_execution_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        _storage_fault_status_payload(
            preflight=None,
            detail="storage preflight failed",
        ),
        _storage_fault_status_payload(
            preflight=None,
            detail="storage preflight exceeded deadline",
        ),
        _storage_fault_status_payload(
            preflight=None,
            detail="PRIVATE preflight hunter2 /private/path",
        ),
        _storage_fault_status_payload(
            preflight=["PRIVATE", "hunter2", "/private/path"],
            detail="storage preflight failed",
        ),
    )
    refusals = [_launch_status_refusal(monkeypatch, payload) for payload in cases]

    assert len(set(refusals[:3])) == 3
    assert refusals[2] == refusals[3]
    for refusal in set(refusals):
        assert run._validated_refusal_location(refusal) == refusal
        for private in (b"PRIVATE", b"hunter2", b"/private", b"failed", b"deadline"):
            assert private not in refusal


def test_preflight_execution_diagnostic_has_closed_unique_stage_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert run.UNIT_RE.fullmatch(
        "dashcam-m10-private-123456789abc-preflight.service"
    ) is not None
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    paths = {
        "recording": tmp_path / "recording",
        "state": tmp_path / "state",
        "runtime": runtime,
    }
    monkeypatch.setattr(run, "render_transient_properties", lambda **_kwargs: ())
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=1234)),
    )
    monkeypatch.setattr(
        run,
        "_validate_runtime_recovery_identity",
        lambda _runtime, _uid: runtime.stat(),
    )
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(
        run,
        "_consume_preflight_diagnostic_at",
        lambda *_args: run._strict_json(
            (runtime / "preflight-diagnostic.json").read_bytes(),
            "preflight diagnostic",
        ),
    )
    monkeypatch.setattr(
        run,
        "_wait_unit_terminal",
        lambda _unit, _timeout: {"Result": "success", "ExecMainStatus": "0"},
    )
    monkeypatch.setattr(run, "_remove_unit", lambda _unit: None)
    stages = (
        "IMPORT",
        "CONFIG",
        "IDENTITY",
        "POLICY",
        "COLLECT_ROOT",
        "FINDMNT_EXEC",
        "FINDMNT_BOUND",
        "FINDMNT_RETURN",
        "FINDMNT_JSON",
        "FINDMNT_ROOT",
        "FINDMNT_ROWS",
        "FINDMNT_ROW",
        "FINDMNT_DIFFERING",
        "COLLECT_TARGET",
        "COLLECT_SOURCE",
        "COLLECT_FIELDS",
        "COLLECT_DIRECTORY",
        "COLLECT_DEVICE",
        "COLLECT_RELATION",
        "COLLECT_SPACE",
        "COLLECT_SENTINEL",
        "PARSE",
        "FILESYSTEM",
        "RUN",
        "RESULT",
    )
    refusals: set[bytes] = set()

    def installer(output: Path, stage: str) -> Callable[..., None]:
        def systemd_run(*_args: object, **_kwargs: object) -> None:
            output.write_bytes(run.canonical_json({"schema_version": 1, "stage": stage}))

        return systemd_run

    for stage in stages:
        output = runtime / "preflight-diagnostic.json"
        monkeypatch.setattr(run, "_systemd_run", installer(output, stage))
        try:
            run._run_preflight_diagnostic("123456789abc", paths)
        except run.HarnessError as error:
            refusal = run._refusal_line(error)
        else:
            pytest.fail("preflight diagnostic unexpectedly returned")
        output.unlink()
        assert refusal.startswith(b"REFUSED: H_HARNESS_Frun_preflight_diagnostic_L")
        assert run._validated_refusal_location(refusal) == refusal
        assert stage.encode("ascii") not in refusal
        refusals.add(refusal)

    assert len(refusals) == len(stages)


def test_storage_fault_diagnostic_splits_every_preflight_malformed_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_keys = _storage_preflight_status()
    wrong_keys["private_extra"] = "hunter2"
    state_type = _storage_preflight_status()
    state_type["state"] = 7
    state_domain = _storage_preflight_status()
    state_domain["state"] = "PRIVATE_STATE"
    reasons_type = _storage_preflight_status()
    reasons_type["reasons"] = "PRIVATE_REASONS"
    reasons_length = _storage_preflight_status()
    reasons_length["reasons"] = ["WRONG_TARGET"] * (len(run.PREFLIGHT_REASONS) + 1)
    reason_type = _storage_preflight_status()
    reason_type["reasons"] = [7]
    reason_duplicate = _storage_preflight_status("READ_ONLY")
    reason_duplicate["reasons"] = ["READ_ONLY", "READ_ONLY"]
    reason_domain = _storage_preflight_status("PRIVATE_REASON")
    ready_type = _storage_preflight_status()
    ready_type["ready"] = "PRIVATE_READY"
    empty_relation = _storage_preflight_status()
    empty_relation["state"] = "CHECKING"
    nonempty_relation = _storage_preflight_status("WRONG_TARGET")
    nonempty_relation["state"] = "READY"
    cases: tuple[object, ...] = (
        ["PRIVATE_NOT_MAPPING", "/private/path", "hunter2"],
        wrong_keys,
        state_type,
        state_domain,
        reasons_type,
        reasons_length,
        reason_type,
        reason_duplicate,
        reason_domain,
        ready_type,
        empty_relation,
        nonempty_relation,
    )
    refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(preflight=preflight),
        )
        for preflight in cases
    }

    assert len(refusals) == len(cases)
    for refusal in refusals:
        assert run._validated_refusal_location(refusal) == refusal
        for private in (
            b"PRIVATE",
            b"storage_preflight",
            b"state",
            b"reasons",
            b"ready",
            b"hunter2",
            b"/private",
        ):
            assert private not in refusal


def test_storage_fault_diagnostic_refuses_unknown_closed_domain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_state = _storage_preflight_status()
    unknown_state["state"] = "PRIVATE_STATE"
    unknown_reason = _storage_preflight_status("PRIVATE_REASON")
    preflight_refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(preflight=preflight),
        )
        for preflight in (unknown_state, unknown_reason)
    }
    unknown_mode = _storage_retention_status(mode="PRIVATE_MODE")
    unknown_fault = _storage_retention_status(fault="PRIVATE_FAULT")
    unknown_trigger = _storage_retention_status(trigger="PRIVATE_TRIGGER")
    retention_refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(retention=retention),
        )
        for retention in (unknown_mode, unknown_fault, unknown_trigger)
    }

    assert len(preflight_refusals) == 2
    assert len(retention_refusals) == 1
    assert preflight_refusals.isdisjoint(retention_refusals)
    for refusal in preflight_refusals | retention_refusals:
        assert b"PRIVATE" not in refusal


def test_storage_fault_diagnostic_refuses_impossible_production_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impossible = [
        _storage_retention_status(mode="NORMAL", trigger="NO_SPACE_WRITE"),
        _storage_retention_status(mode="NORMAL", stop_required=True),
        *(
            _storage_retention_status(mode=mode, stop_required=True)
            for mode in (None, "NORMAL", "RECLAIMING")
        ),
        *(
            _storage_retention_status(mode=None, fault=fault, stop_required=True)
            for fault in ("OBSERVATION_FAILED", "INVALID_OBSERVATION")
        ),
        *(
            _storage_retention_status(mode=None, fault=fault, stop_required=False)
            for fault in (
                "OBSERVATION_STALE",
                "IDENTITY_DRIFT",
                "CAPACITY_DRIFT",
                "LATCH_LOAD_FAILED",
                "LATCH_BINDING_MISMATCH",
                "LATCH_STORE_FAILED",
            )
        ),
        _storage_retention_status(
            mode="RECLAIMING",
            fault="NO_SPACE_WRITE",
            trigger="NO_SPACE_WRITE",
            stop_required=True,
        ),
        _storage_retention_status(
            mode="EMERGENCY",
            fault="NO_SPACE_WRITE",
            trigger=None,
            stop_required=True,
        ),
        _storage_retention_status(
            mode="EMERGENCY",
            fault="NO_SPACE_WRITE",
            trigger="NO_SPACE_WRITE",
            stop_required=False,
        ),
        _storage_retention_status(mode=None, trigger="NO_SPACE_WRITE"),
    ]
    refusals = {
        _launch_status_refusal(
            monkeypatch,
            _storage_fault_status_payload(retention=retention),
        )
        for retention in impossible
    }

    assert len(refusals) == 1
    refusal = next(iter(refusals))
    assert run._validated_refusal_location(refusal) == refusal


def test_wait_recording_timeout_uses_closed_status_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    status = runtime / "status.json"
    payload = _launch_status_payload("FAULTED", "PIPELINE_NO_PROGRESS")
    monotonic = iter((0.0, 1.0))
    monkeypatch.setattr(run.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(run.Path, "is_file", lambda self: self == status)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_bounded_read", lambda _path, _maximum: payload)

    try:
        run._wait_recording(runtime, timeout=0.5)
    except run.HarnessError as error:
        refusal = run._refusal_line(error)
    else:
        pytest.fail("recording-status timeout unexpectedly returned")

    assert refusal.startswith(b"REFUSED: H_HARNESS_Fphase_a_launch_failure_status_L")
    assert run._validated_refusal_location(refusal) == refusal
    for private in (
        b"MyHome",
        b"hunter2",
        b"/private",
        b"abcdef",
        b"32.1",
        b"34.8",
        b"/srv",
        b"detail",
        b"runtime",
        b"reason",
    ):
        assert private not in refusal


def test_wait_recording_success_does_not_run_failure_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    status = runtime / "status.json"
    payload = _launch_status_payload("RECORDING", None)
    monotonic = iter((0.0, 0.1))
    classified: list[Path] = []
    monkeypatch.setattr(run.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(run.Path, "is_file", lambda self: self == status)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_bounded_read", lambda _path, _maximum: payload)
    monkeypatch.setattr(
        run,
        "_phase_a_launch_failure_status",
        lambda path: classified.append(path),
    )

    assert run._wait_recording(runtime, timeout=0.5)["lifecycle"] == {
        "state": "RECORDING",
        "reason": None,
        "detail": "SSID MyHome PSK hunter2 /private/path token abcdef",
        "sequence": 7,
        "config_schema_version": 1,
        "notification_failures": 0,
    }
    assert classified == []


def test_wait_recording_routes_preflight_failure_to_private_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    status = runtime / "status.json"
    payload = _storage_fault_status_payload(
        preflight=None,
        detail="storage preflight failed",
    )
    monotonic = iter((0.0, 1.0))
    called: list[bool] = []
    monkeypatch.setattr(run.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(run.Path, "is_file", lambda self: self == status)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_bounded_read", lambda _path, _maximum: payload)

    def diagnostic() -> None:
        called.append(True)
        raise run.HarnessError("fixed diagnostic refusal")

    with pytest.raises(run.HarnessError, match="fixed diagnostic refusal"):
        run._wait_recording(
            runtime,
            timeout=0.5,
            preflight_failure=diagnostic,
        )
    assert called == [True]


def test_phase_a_wires_launch_failure_status_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    status = runtime / "status.json"
    payload = _launch_status_payload("FAULTED", "STORAGE_FAULT")
    monkeypatch.setattr(run.Path, "is_file", lambda self: self == status)
    monkeypatch.setattr(run.Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_bounded_read", lambda _path, _maximum: payload)
    monkeypatch.setattr(run, "_source_environment", lambda _archive: None)
    monkeypatch.setattr(run, "_read_boot_id", lambda: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(run, "_fixture_subprocess", lambda *_args: {})
    monkeypatch.setattr(run, "resolved_thresholds", lambda _capacity: (100, 200, 50))
    monkeypatch.setattr(run, "_allocate_filler", lambda _root, _target: (Path("filler"), 1))

    def candidate(
        _nonce: str,
        _suffix: str,
        _paths: object,
        **options: object,
    ) -> str:
        callback = options.get("launch_failure")
        assert callable(callback)
        callback()
        pytest.fail("launch failure callback unexpectedly returned")

    monkeypatch.setattr(run, "_candidate_unit", candidate)
    paths = {
        "recording": Path("/fixture/recording"),
        "state": Path("/fixture/state"),
        "runtime": runtime,
    }
    try:
        run._phase_a("123456789abc", paths, {"capacity_bytes": 4096}, dashcam_uid=42)
    except run.HarnessError as error:
        refusal = run._refusal_line(error)
    else:
        pytest.fail("phase A launch failure unexpectedly returned")

    assert refusal.startswith(b"REFUSED: H_HARNESS_Fphase_a_launch_failure_status_L")
    assert run._validated_refusal_location(refusal) == refusal


def test_candidate_unit_runs_launch_failure_only_after_reconciliation_and_not_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    paths = {
        "recording": Path("/private/recording"),
        "state": Path("/private/state"),
        "runtime": Path("/private/runtime"),
    }
    monkeypatch.setattr(run, "render_transient_properties", lambda **_kwargs: ())
    monkeypatch.setattr(
        run,
        "_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(run.HarnessError("private")),
    )
    monkeypatch.setattr(run, "_remove_unit", lambda _unit: events.append("reconciled"))

    with pytest.raises(run.HarnessError):
        run._candidate_unit(
            "123456789abc",
            "a",
            paths,
            launch_failure=lambda: events.append("callback"),
        )
    assert events == ["reconciled", "callback"]

    events.clear()
    monkeypatch.setattr(run, "_systemd_run", lambda *_args, **_kwargs: None)
    run._candidate_unit(
        "123456789abc",
        "a",
        paths,
        launch_failure=lambda: events.append("callback"),
    )
    assert events == []


def test_fixture_subprocess_relays_only_exact_self_validated_child_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = min(run._reviewed_function_lines()["seed_fixture"])
    child = f"REFUSED: H_HARNESS_Fseed_fixture_L{line}\n".encode("ascii")
    completed = run.subprocess.CompletedProcess([], 2, stdout=b"", stderr=child)
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace())
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: completed)

    try:
        run._fixture_subprocess(
            "A", Path("/fixture"), Path("/state/catalog.sqlite3"), "boot", Path("/source.zip")
        )
    except run.FixtureChildRefusal as error:
        payload = run._refusal_line(error)
    else:
        pytest.fail("exact fixture child refusal was not relayed")

    expected = f"REFUSED: H_FIXTURE_CHILD child=H_HARNESS_Fseed_fixture_L{line}\n".encode(
        "ascii"
    )
    assert payload == expected
    assert run._validated_fixture_child_refusal(payload) == payload


def test_fixture_subprocess_rejects_every_nonexact_child_refusal_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = run._reviewed_function_lines()
    line = min(lines["seed_fixture"])
    child = f"REFUSED: H_HARNESS_Fseed_fixture_L{line}\n".encode("ascii")
    wrong_line = max(lines["seed_fixture"]) + 1
    cases = (
        (1, b"", child),
        (2, b"private stdout", child),
        (2, b"", child.removesuffix(b"\n")),
        (2, b"", child + b"private second line\n"),
        (2, b"", f"REFUSED: H_HARNESS_Fseed_fixture_L{wrong_line}\n".encode("ascii")),
        (2, b"", f"REFUSED: H_HARNESS_Fexternal_L{line}\n".encode("ascii")),
        (2, b"", b"SSID MyHome PSK hunter2 coordinates 32.1,34.8 /private/path\n"),
        (2, b"", b"x" * (run.MAX_COMMAND_OUTPUT + 1)),
    )
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace())

    for returncode, stdout, stderr in cases:
        completed = run.subprocess.CompletedProcess(
            ["/private/tool", "--token", "abcdef"],
            returncode,
            stdout=stdout,
            stderr=stderr,
        )
        monkeypatch.setattr(
            run.subprocess,
            "run",
            lambda *_args, _completed=completed, **_kwargs: _completed,
        )
        try:
            run._fixture_subprocess(
                "A",
                Path("/fixture"),
                Path("/state/catalog.sqlite3"),
                "boot",
                Path("/source.zip"),
            )
        except run.HarnessError as error:
            assert not isinstance(error, run.FixtureChildRefusal)
            payload = run._refusal_line(error)
        else:
            pytest.fail("nonexact fixture child refusal was admitted")

        assert payload.startswith(b"REFUSED: H_HARNESS_Ffixture_subprocess_L")
        assert run._validated_refusal_location(payload) == payload
        assert run._validated_fixture_child_refusal(payload) is None
        for private in (
            b"MyHome",
            b"hunter2",
            b"32.1",
            b"34.8",
            b"/private",
            b"abcdef",
            b"stdout",
            b"stderr",
        ):
            assert private not in payload


def test_fixture_child_refusal_is_opt_in_to_fixture_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = min(run._reviewed_function_lines()["seed_fixture"])
    child = f"REFUSED: H_HARNESS_Fseed_fixture_L{line}\n".encode("ascii")
    completed = run.subprocess.CompletedProcess([], 2, stdout=b"", stderr=child)
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(run.HarnessError) as captured:
        run._command(("/private/tool",))

    assert not isinstance(captured.value, run.FixtureChildRefusal)
    assert run._refusal_line(captured.value) == b"REFUSED: HarnessError\n"


def test_parent_top_level_emits_only_closed_fixture_child_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    line = min(run._reviewed_function_lines()["seed_fixture"])
    child = f"REFUSED: H_VALUE_Fseed_fixture_L{line}\n".encode("ascii")
    arguments = run.argparse.Namespace(
        recover_work=None,
        fixture=None,
        bundle="bundle",
        expected_manifest_sha256="0" * 64,
        expected_harness_commit="1" * 40,
        expected_candidate_commit=run.EXPECTED_CANDIDATE,
        rollback_commit=run.EXPECTED_ROLLBACK,
        expected_board_serial="00000000db28ffe4",
        output="/var/tmp/result.json",
        fixture_root=None,
        fixture_catalog=None,
        fixture_boot_id=None,
        fixture_source=None,
    )
    parser = SimpleNamespace(parse_args=lambda _argv: arguments)
    monkeypatch.setattr(run, "_parser", lambda: parser)

    def refuse(_arguments: object) -> dict[str, object]:
        raise run.FixtureChildRefusal(child)

    monkeypatch.setattr(run, "qualify", refuse)

    assert run.main([]) == 2
    captured = capsys.readouterr()
    expected = f"REFUSED: H_FIXTURE_CHILD child=H_VALUE_Fseed_fixture_L{line}\n"
    assert captured.out == ""
    assert captured.err == expected
    assert run._validated_fixture_child_refusal(captured.err.encode("ascii")) is not None


def test_nested_fixture_child_validator_rejects_malformed_or_forged_tokens() -> None:
    lines = run._reviewed_function_lines()
    line = min(lines["seed_fixture"])
    accepted = f"REFUSED: H_FIXTURE_CHILD child=H_OS_Fseed_fixture_L{line}\n".encode(
        "ascii"
    )
    assert run._validated_fixture_child_refusal(accepted) == accepted

    for refused in (
        accepted.removesuffix(b"\n"),
        accepted + b"private second line\n",
        accepted.replace(b"H_FIXTURE_CHILD", b"H_OTHER_CHILD"),
        f"REFUSED: H_FIXTURE_CHILD child=H_OS_Fseed_fixture_L"
        f"{max(lines['seed_fixture']) + 1}\n".encode("ascii"),
        f"REFUSED: H_FIXTURE_CHILD child=H_OS_Fexternal_L{line}\n".encode("ascii"),
        b"REFUSED: H_FIXTURE_CHILD child=SSID MyHome PSK hunter2\n",
    ):
        assert run._validated_fixture_child_refusal(refused) is None


@pytest.mark.parametrize(
    ("error", "plain"),
    [
        (run.HarnessError("token abcdef"), b"REFUSED: HarnessError\n"),
        (OSError("SSID MyHome"), b"REFUSED: OSError\n"),
        (ValueError("coordinates 32.1,34.8"), b"REFUSED: ValueError\n"),
        (RuntimeError("/private/catalog.sqlite3"), b"REFUSED: RuntimeError\n"),
        (Exception("secret bearer value"), b"REFUSED: Exception\n"),
    ],
)
def test_unlocated_refusal_uses_only_fixed_plain_type(
    error: Exception, plain: bytes
) -> None:
    payload = run._refusal_line(error)

    assert payload == plain
    assert run.REFUSAL_LOCATION_RE.fullmatch(payload) is None
    for private in (b"abcdef", b"MyHome", b"32.1", b"34.8", b"/private", b"bearer"):
        assert private not in payload


def test_external_only_exception_frame_is_not_reported() -> None:
    try:
        raise OSError("external /private/path token abcdef")
    except OSError as error:
        payload = run._refusal_line(error)

    assert payload == b"REFUSED: OSError\n"
    assert b"private" not in payload
    assert b"abcdef" not in payload


def test_reviewed_path_resolution_failure_falls_back_without_private_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RefusingPath:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def resolve(self, *, strict: bool) -> Path:
            assert strict is True
            raise OSError("private reviewed path token abcdef")

    monkeypatch.setattr(run, "Path", RefusingPath)
    payload = run._refusal_line(run.HarnessError("SSID MyHome PSK hunter2"))

    assert payload == b"REFUSED: HarnessError\n"
    assert b"abcdef" not in payload
    assert b"MyHome" not in payload


def test_forged_reviewed_name_with_external_code_filename_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_lines = run._reviewed_function_lines()["host_snapshot"]

    def external_host_snapshot() -> dict[str, object]:
        return {}

    monkeypatch.setattr(run, "_host_snapshot", external_host_snapshot)
    assert "host_snapshot" not in run._reviewed_function_lines()
    forged = (
        f"REFUSED: H_HARNESS_Fhost_snapshot_L{min(original_lines)}\n".encode("ascii")
    )
    assert run._validated_refusal_location(forged) is None


def test_main_refusal_is_one_safe_validated_line_without_exception_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = run.argparse.Namespace(
        recover_work=None,
        fixture=None,
        bundle="bundle",
        expected_manifest_sha256="0" * 64,
        expected_harness_commit="1" * 40,
        expected_candidate_commit=run.EXPECTED_CANDIDATE,
        rollback_commit=run.EXPECTED_ROLLBACK,
        expected_board_serial="00000000db28ffe4",
        output="/var/tmp/result.json",
        fixture_root=None,
        fixture_catalog=None,
        fixture_boot_id=None,
        fixture_source=None,
    )
    parser = SimpleNamespace(parse_args=lambda _argv: arguments)
    monkeypatch.setattr(run, "_parser", lambda: parser)

    def refuse(_arguments: object) -> dict[str, object]:
        raise run.HarnessError("SSID MyHome PSK hunter2 /private/path token abcdef")

    monkeypatch.setattr(run, "qualify", refuse)

    assert run.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = captured.err.encode("ascii")
    assert payload.startswith(b"REFUSED: H_HARNESS_Fmain_L")
    assert run._validated_refusal_location(payload) == payload
    for private in ("MyHome", "hunter2", "/private", "abcdef"):
        assert private not in captured.err
