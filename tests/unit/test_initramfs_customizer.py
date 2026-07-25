from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from dashcam.provisioning.initramfs_archive import (
    ZSTD_MAGIC,
    NewcArchive,
    NewcEntry,
    parse_newc_archive,
    serialize_newc_archive,
    split_initramfs,
)
from dashcam.provisioning.initramfs_customizer import (
    InitramfsClosureManifest,
    InitramfsCustomizationError,
    customize_initramfs,
    load_initramfs_closure_manifest,
)


def _entry(name: str, mode: int, data: bytes = b"", inode: int = 1) -> NewcEntry:
    return NewcEntry(name, inode, mode, 0, 0, 1, 10, 0, 0, 0, 0, data, 0, 0)


def _main(hook: bytes) -> bytes:
    entries = (
        _entry(".", stat.S_IFDIR | 0o755),
        _entry("scripts", stat.S_IFDIR | 0o755, inode=2),
        _entry("scripts/local-premount", stat.S_IFDIR | 0o755, inode=3),
        _entry("scripts/local-premount/resize_early", stat.S_IFREG | 0o755, b"stock", 4),
        _entry("etc", stat.S_IFDIR | 0o755, inode=5),
        _entry("usr", stat.S_IFDIR | 0o755, inode=6),
        _entry("usr/sbin", stat.S_IFDIR | 0o755, inode=7),
        _entry("usr/lib", stat.S_IFDIR | 0o755, inode=8),
        _entry("usr/lib/arm-linux-gnueabihf", stat.S_IFDIR | 0o755, inode=9),
        _entry(
            "usr/lib/arm-linux-gnueabihf/libc.so.6",
            stat.S_IFREG | 0o755,
            b"stock-libc",
            10,
        ),
        _entry(
            "usr/lib/ld-linux-armhf.so.3",
            stat.S_IFLNK | 0o777,
            b"arm-linux-gnueabihf/libc.so.6",
            11,
        ),
    )
    assert hook
    return serialize_newc_archive(NewcArchive(entries, 0, 0))


def _early() -> bytes:
    early = serialize_newc_archive(NewcArchive((_entry(".", stat.S_IFDIR | 0o755),), 0, 0))
    return early + b"\0" * (-len(early) % 512)


class FakeZstd:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.workdirs: list[Path] = []
        self.fail = False
        self.timeout = False
        self.oversize_diagnostics = False

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> int:
        self.argv.append(argv)
        self.workdirs.append(stdout_path.parent)
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        source = Path(argv[-1]).read_bytes()
        if "--decompress" in argv:
            stdout_path.write_bytes(source[len(ZSTD_MAGIC) :])
        else:
            stdout_path.write_bytes(ZSTD_MAGIC + source)
        stderr_path.write_bytes(b"x" * (65 * 1024) if self.oversize_diagnostics else b"")
        return 7 if self.fail else 0


def _fixture(tmp_path: Path) -> tuple[bytes, bytes, dict[str, Path], Path, dict[str, object]]:
    hook = b"#!/bin/sh\nexit 125\n"
    main = _main(hook)
    compressed = ZSTD_MAGIC + main
    image = _early() + compressed
    artifacts: dict[str, Path] = {}
    artifact_specs = []
    for index, (key, destination, mode) in enumerate(
        (
            ("resize2fs", "usr/sbin/resize2fs", stat.S_IFREG | 0o755),
            ("dumpe2fs", "usr/sbin/dumpe2fs", stat.S_IFREG | 0o755),
            ("sfdisk", "usr/sbin/sfdisk", stat.S_IFREG | 0o755),
            ("libfdisk", "usr/lib/arm-linux-gnueabihf/libfdisk.so.1", stat.S_IFREG | 0o644),
        )
    ):
        data = f"artifact-{key}".encode()
        path = tmp_path / key
        path.write_bytes(data)
        artifacts[key] = path
        artifact_specs.append(
            {
                "key": key,
                "source_path": f"/source/{key}",
                "destination": destination,
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": mode,
                "mtime": 100 + index,
            }
        )
    zstd = tmp_path / "zstd"
    zstd.write_bytes(b"fake")
    if os.name != "nt":
        zstd.chmod(0o755)
    contracts = []
    for name in ("config", "source", "target"):
        contract_text = f"{name}=v1\n"
        contracts.append(
            {
                "destination": f"etc/dashcam/{name}",
                "content": contract_text,
                "sha256": hashlib.sha256(contract_text.encode()).hexdigest(),
                "mode": stat.S_IFREG | 0o444,
                "mtime": 99,
            }
        )
    raw: dict[str, object] = {
        "schema_version": 1,
        "target": {
            "profile": "pi-zero-2-w-armv7l",
            "architecture": "armv7l",
            "kernel_filename": "kernel7.img",
            "initramfs_filename": "initramfs7",
            "auto_initramfs_required": True,
        },
        "input": {
            "size_bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "main_offset": len(_early()),
            "main_compressed_size_bytes": len(compressed),
            "main_compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "main_uncompressed_size_bytes": len(main),
            "main_uncompressed_sha256": hashlib.sha256(main).hexdigest(),
            "early_entry_count": 1,
            "main_entry_count": 11,
        },
        "stock_hook": {
            "path": "scripts/local-premount/resize_early",
            "required_count": 1,
            "replacement_sha256": hashlib.sha256(hook).hexdigest(),
        },
        "injection": {
            "directory": "etc/dashcam",
            "directory_mode": stat.S_IFDIR | 0o755,
            "mtime": 99,
        },
        "contracts": contracts,
        "artifacts": artifact_specs,
        "elf_closure": {
            "root_artifact_keys": ["resize2fs", "dumpe2fs", "sfdisk", "libfdisk"],
            "required_stock_entries": [
                {
                    "destination": "usr/lib/arm-linux-gnueabihf/libc.so.6",
                    "sha256": hashlib.sha256(b"stock-libc").hexdigest(),
                    "mode": stat.S_IFREG | 0o755,
                },
                {
                    "destination": "usr/lib/ld-linux-armhf.so.3",
                    "sha256": hashlib.sha256(b"arm-linux-gnueabihf/libc.so.6").hexdigest(),
                    "mode": stat.S_IFLNK | 0o777,
                },
            ],
        },
        "runtime_gate": {
            "destination": "etc/dashcam/firstboot-runtime-v1.enabled",
            "content": "EXACT_IMAGE_RUNTIME_VALIDATED=v1\n",
            "sha256": hashlib.sha256(b"EXACT_IMAGE_RUNTIME_VALIDATED=v1\n").hexdigest(),
            "mode": stat.S_IFREG | 0o400,
            "mtime": 99,
        },
        "zstd": {"compression_level": 19, "threads": 1, "checksum": True},
        "maximum_output_bytes": 1024 * 1024,
    }
    return image, hook, artifacts, zstd, raw


def _manifest(raw: dict[str, object]) -> InitramfsClosureManifest:
    return load_initramfs_closure_manifest(json.dumps(raw).encode())


def _replace_main_input(raw: dict[str, object], main: bytes, entry_count: int) -> bytes:
    changed = _early() + ZSTD_MAGIC + main
    input_raw = raw["input"]
    assert isinstance(input_raw, dict)
    input_raw.update(
        {
            "size_bytes": len(changed),
            "sha256": hashlib.sha256(changed).hexdigest(),
            "main_compressed_size_bytes": len(ZSTD_MAGIC + main),
            "main_compressed_sha256": hashlib.sha256(ZSTD_MAGIC + main).hexdigest(),
            "main_uncompressed_size_bytes": len(main),
            "main_uncompressed_sha256": hashlib.sha256(main).hexdigest(),
            "main_entry_count": entry_count,
        }
    )
    return changed


def test_customization_is_deterministic_and_verifies_injected_closure(tmp_path: Path) -> None:
    image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    runner = FakeZstd()

    first = customize_initramfs(
        image,
        manifest=_manifest(raw),
        zstd_executable=zstd,
        replacement_hook=hook,
        root_artifacts=artifacts,
        runner=runner,
    )
    second = customize_initramfs(
        image,
        manifest=_manifest(raw),
        zstd_executable=zstd,
        replacement_hook=hook,
        root_artifacts=artifacts,
        runner=FakeZstd(),
    )

    assert first == second
    assert split_initramfs(first).early_bytes == split_initramfs(image).early_bytes
    main = parse_newc_archive(split_initramfs(first).main_compressed[len(ZSTD_MAGIC) :])
    names = {entry.name for entry in main.entries}
    assert "etc/dashcam" in names
    assert "usr/sbin/resize2fs" in names
    assert "usr/lib/arm-linux-gnueabihf/libfdisk.so.1" in names
    assert "etc/dashcam/firstboot-runtime-v1.enabled" not in names
    compress_argv = next(argv for argv in runner.argv if "--compress" in argv)
    assert "--threads=1" in compress_argv
    assert "--check" in compress_argv
    assert "-19" in compress_argv
    assert all("sh" not in argv and "-c" not in argv for argv in runner.argv)


def test_authorized_customization_injects_only_the_exact_closed_gate(tmp_path: Path) -> None:
    image, hook, artifacts, zstd, raw = _fixture(tmp_path)

    enabled = customize_initramfs(
        image,
        manifest=_manifest(raw),
        zstd_executable=zstd,
        replacement_hook=hook,
        root_artifacts=artifacts,
        runtime_gate_enabled=True,
        runner=FakeZstd(),
    )

    main = parse_newc_archive(split_initramfs(enabled).main_compressed[len(ZSTD_MAGIC) :])
    gate = next(
        entry for entry in main.entries if entry.name == "etc/dashcam/firstboot-runtime-v1.enabled"
    )
    assert gate.data == b"EXACT_IMAGE_RUNTIME_VALIDATED=v1\n"
    assert gate.mode == stat.S_IFREG | 0o400


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size_bytes", 1, "size"),
        ("sha256", "0" * 64, "SHA-256"),
        ("main_offset", 1024, "offset"),
        ("early_entry_count", 2, "entry count"),
        ("main_entry_count", 8, "entry count"),
    ],
)
def test_input_manifest_mismatches_refuse(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    input_raw = raw["input"]
    assert isinstance(input_raw, dict)
    input_raw[field] = value
    with pytest.raises(InitramfsCustomizationError, match=message):
        customize_initramfs(
            image,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )


def test_hook_artifact_and_tool_validation_refuse_before_mutation(tmp_path: Path) -> None:
    image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    with pytest.raises(InitramfsCustomizationError, match="replacement hook SHA"):
        customize_initramfs(
            image,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=b"wrong",
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )
    artifacts["resize2fs"].write_bytes(b"wrong")
    with pytest.raises(InitramfsCustomizationError, match="artifact resize2fs SHA"):
        customize_initramfs(
            image,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )
    artifacts["resize2fs"].write_bytes(b"artifact-resize2fs")
    bad_tool = tmp_path / "not-zstd"
    bad_tool.write_bytes(b"x")
    with pytest.raises(InitramfsCustomizationError, match="zstd must be"):
        customize_initramfs(
            image,
            manifest=_manifest(raw),
            zstd_executable=bad_tool,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )


def test_runtime_gate_manifest_drift_is_refused(tmp_path: Path) -> None:
    _image, _hook, _artifacts, _zstd, raw = _fixture(tmp_path)
    runtime_gate = raw["runtime_gate"]
    assert isinstance(runtime_gate, dict)
    drifted = b"EXACT_IMAGE_RUNTIME_VALIDATED=v2\n"
    runtime_gate["content"] = drifted.decode()
    runtime_gate["sha256"] = hashlib.sha256(drifted).hexdigest()

    with pytest.raises(InitramfsCustomizationError, match="gate contract is not exact"):
        _manifest(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", "pi-zero-w-armv6l"),
        ("architecture", "aarch64"),
        ("kernel_filename", "kernel8.img"),
        ("initramfs_filename", "initramfs"),
        ("auto_initramfs_required", False),
    ],
)
def test_wrong_or_generic_boot_target_is_refused(tmp_path: Path, field: str, value: object) -> None:
    _image, _hook, _artifacts, _zstd, raw = _fixture(tmp_path)
    target = raw["target"]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(
        InitramfsCustomizationError,
        match="not the exact Pi Zero 2 W armv7l boot target",
    ):
        _manifest(raw)


def test_stock_hook_cardinality_and_forbidden_gate_refuse(tmp_path: Path) -> None:
    _image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    main = _main(hook)
    parsed = parse_newc_archive(main)
    without_hook = serialize_newc_archive(
        NewcArchive(
            tuple(
                entry
                for entry in parsed.entries
                if entry.name != "scripts/local-premount/resize_early"
            ),
            0,
            0,
        )
    )
    changed = _early() + ZSTD_MAGIC + without_hook
    input_raw = raw["input"]
    assert isinstance(input_raw, dict)
    input_raw.update(
        {
            "size_bytes": len(changed),
            "sha256": hashlib.sha256(changed).hexdigest(),
            "main_compressed_size_bytes": len(ZSTD_MAGIC + without_hook),
            "main_compressed_sha256": hashlib.sha256(ZSTD_MAGIC + without_hook).hexdigest(),
            "main_uncompressed_size_bytes": len(without_hook),
            "main_uncompressed_sha256": hashlib.sha256(without_hook).hexdigest(),
            "main_entry_count": 10,
        }
    )
    with pytest.raises(InitramfsCustomizationError, match="cardinality"):
        customize_initramfs(
            changed,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )


@pytest.mark.parametrize("mutation", ["missing", "content", "mode"])
def test_missing_or_mutated_required_stock_library_refuses_before_rebuild(
    tmp_path: Path, mutation: str
) -> None:
    _image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    parsed = parse_newc_archive(_main(hook))
    target = "usr/lib/arm-linux-gnueabihf/libc.so.6"
    entries: list[NewcEntry] = []
    for entry in parsed.entries:
        if entry.name != target:
            entries.append(entry)
        elif mutation == "content":
            entries.append(replace(entry, data=b"mutated-libc"))
        elif mutation == "mode":
            entries.append(replace(entry, mode=stat.S_IFREG | 0o644))
    changed_main = serialize_newc_archive(NewcArchive(tuple(entries), 0, 0))
    changed = _replace_main_input(raw, changed_main, 10 if mutation == "missing" else 11)

    with pytest.raises(
        InitramfsCustomizationError,
        match="required stock library is missing or changed",
    ):
        customize_initramfs(
            changed,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=FakeZstd(),
        )


@pytest.mark.parametrize("failure", ["status", "timeout", "diagnostics"])
def test_zstd_failures_are_bounded_and_temporary_files_are_cleaned(
    tmp_path: Path, failure: str
) -> None:
    image, hook, artifacts, zstd, raw = _fixture(tmp_path)
    runner = FakeZstd()
    runner.fail = failure == "status"
    runner.timeout = failure == "timeout"
    runner.oversize_diagnostics = failure == "diagnostics"
    with pytest.raises(InitramfsCustomizationError):
        customize_initramfs(
            image,
            manifest=_manifest(raw),
            zstd_executable=zstd,
            replacement_hook=hook,
            root_artifacts=artifacts,
            runner=runner,
        )
    assert runner.workdirs
    assert all(not path.exists() for path in runner.workdirs)


def test_manifest_records_exact_pinned_closure_and_closed_gate() -> None:
    path = Path(__file__).parents[2] / "deploy" / "image" / "initramfs-closure-v1.json"
    manifest = load_initramfs_closure_manifest(path.read_bytes())
    assert manifest.target.profile == "pi-zero-2-w-armv7l"
    assert manifest.target.architecture == "armv7l"
    assert manifest.target.kernel_filename == "kernel7.img"
    assert manifest.target.initramfs_filename == "initramfs7"
    assert manifest.target.auto_initramfs_required
    assert manifest.input_sha256 == (
        "3f5288ed963028accc5103f13f5a03a9d6d26ef3f58ad38a31736d3802d452b1"
    )
    assert manifest.main_offset == 3_924_992
    assert (manifest.early_entry_count, manifest.main_entry_count) == (328, 467)
    assert {item.key for item in manifest.artifacts} == {
        "resize2fs",
        "dumpe2fs",
        "sfdisk",
        "libfdisk",
    }
    assert manifest.elf_root_artifact_keys == (
        "resize2fs",
        "dumpe2fs",
        "sfdisk",
        "libfdisk",
    )
    assert len(manifest.required_stock_libraries) == 19
    assert {item.destination for item in manifest.required_stock_libraries} >= {
        "usr/lib/arm-linux-gnueabihf/libblkid.so.1",
        "usr/lib/arm-linux-gnueabihf/libblkid.so.1.1.0",
        "usr/lib/arm-linux-gnueabihf/libc.so.6",
        "usr/lib/ld-linux-armhf.so.3",
    }
    assert all(
        item.destination != "etc/dashcam/firstboot-runtime-v1.enabled"
        for item in manifest.contracts
    )
    assert manifest.runtime_gate.destination == "etc/dashcam/firstboot-runtime-v1.enabled"
    assert manifest.runtime_gate.content == b"EXACT_IMAGE_RUNTIME_VALIDATED=v1\n"
    assert manifest.runtime_gate.mode == stat.S_IFREG | 0o400
