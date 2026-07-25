from __future__ import annotations

import hashlib
import json
import stat
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashcam.provisioning.image_builder import (
    GIB,
    ImageBuildActionKind,
    ImageBuildRefusalCode,
    ImageBuildRefused,
    SourceImageManifest,
    author_image_build_plan,
    calculate_target_layout,
    load_source_manifest,
    validate_output_path,
    verify_raw_image,
    verify_source_archive,
)

ROOT = Path(__file__).parents[2]
OFFICIAL_MANIFEST = ROOT / "deploy" / "image" / "source-manifest-v1.json"
PAYLOAD = ROOT / "deploy" / "image" / "payload"


def _official() -> SourceImageManifest:
    return load_source_manifest(OFFICIAL_MANIFEST.read_bytes())


def _archive(tmp_path: Path, payload: bytes = b"bounded archive fixture\n") -> Path:
    path = tmp_path / "source.img.xz"
    path.write_bytes(payload)
    return path


def _fixture_manifest(source: Path) -> SourceImageManifest:
    official = _official()
    payload = source.read_bytes()
    archive = replace(
        official.archive,
        filename=source.name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    return replace(official, archive=archive)


def _raw_fixture(tmp_path: Path, manifest: SourceImageManifest) -> Path:
    image = manifest.image
    path = tmp_path / "source.img"
    with path.open("wb") as stream:
        stream.truncate(image.size_bytes)
        sector = bytearray(image.sector_size_bytes)
        sector[440:444] = int(image.mbr_disk_id[2:], 16).to_bytes(4, "little")
        for partition in image.partitions:
            offset = 446 + (partition.number - 1) * 16
            sector[offset] = 0x80 if partition.bootable else 0
            sector[offset + 4] = int(partition.partition_type[2:], 16)
            sector[offset + 8 : offset + 12] = partition.start_sector.to_bytes(4, "little")
            sector[offset + 12 : offset + 16] = partition.size_sectors.to_bytes(4, "little")
        sector[510:512] = b"\x55\xaa"
        stream.seek(0)
        stream.write(sector)
        boot, root = image.partitions
        fat_serial = int(boot.filesystem_uuid.replace("-", ""), 16)
        stream.seek(boot.start_sector * image.sector_size_bytes + 67)
        stream.write(fat_serial.to_bytes(4, "little"))
        stream.seek(root.start_sector * image.sector_size_bytes + 1024 + 104)
        stream.write(uuid.UUID(root.filesystem_uuid).bytes)
    return path


def _small_raw_manifest(source: Path) -> SourceImageManifest:
    manifest = _fixture_manifest(source)
    boot = replace(
        manifest.image.partitions[0],
        start_sector=1,
        size_sectors=4,
        filesystem_uuid="1234-ABCD",
    )
    root = replace(
        manifest.image.partitions[1],
        start_sector=5,
        size_sectors=27,
        filesystem_uuid="12345678-1234-5678-9abc-def012345678",
    )
    image = replace(
        manifest.image,
        size_bytes=32 * 512,
        total_sectors=32,
        mbr_disk_id="0x12345678",
        partitions=(boot, root),
    )
    return replace(manifest, image=image)


def test_checked_in_manifest_pins_exact_official_archive_and_geometry() -> None:
    manifest = _official()

    assert manifest.archive.url.endswith(
        "/raspios_lite_armhf-2026-06-19/2026-06-18-raspios-trixie-armhf-lite.img.xz"
    )
    assert manifest.archive.size_bytes == 549_086_704
    assert (
        manifest.archive.sha256
        == "ea4e84c501d6dd4f4b1d04eb84df133a03f90a05ee2e8ab849185c17c2b0707b"
    )
    assert manifest.image.size_bytes == 2_675_965_952
    assert manifest.image.total_sectors == 5_226_496
    assert manifest.image.mbr_disk_id == "0x4f2c9ea0"
    assert [
        (item.number, item.start_sector, item.end_sector, item.partition_type)
        for item in manifest.image.partitions
    ] == [
        (1, 16_384, 1_064_959, "0x0c"),
        (2, 1_064_960, 5_226_495, "0x83"),
    ]
    assert manifest.target.root_size_bytes == 6 * GIB


def test_manifest_parser_is_closed_and_bounded() -> None:
    decoded = json.loads(OFFICIAL_MANIFEST.read_bytes())
    decoded["surprise"] = True
    with pytest.raises(ImageBuildRefused) as caught:
        load_source_manifest(json.dumps(decoded).encode())
    assert caught.value.code is ImageBuildRefusalCode.INVALID_MANIFEST

    with pytest.raises(ImageBuildRefused) as caught:
        load_source_manifest(b"x" * (64 * 1024 + 1))
    assert caught.value.code is ImageBuildRefusalCode.INVALID_MANIFEST


def test_source_size_and_hash_mismatch_are_distinct_refusals(tmp_path: Path) -> None:
    source = _archive(tmp_path)
    manifest = _fixture_manifest(source)

    with pytest.raises(ImageBuildRefused) as caught:
        verify_source_archive(
            source,
            replace(manifest, archive=replace(manifest.archive, size_bytes=1)),
        )
    assert caught.value.code is ImageBuildRefusalCode.SOURCE_SIZE_MISMATCH

    wrong_hash = replace(manifest, archive=replace(manifest.archive, sha256="0" * 64))
    with pytest.raises(ImageBuildRefused) as caught:
        verify_source_archive(source, wrong_hash)
    assert caught.value.code is ImageBuildRefusalCode.SOURCE_HASH_MISMATCH


def test_raw_size_geometry_and_filesystem_identity_are_verified(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    manifest = _small_raw_manifest(archive)
    raw = _raw_fixture(tmp_path, manifest)

    assert verify_raw_image(raw, manifest).mbr_disk_id == "0x12345678"

    with pytest.raises(ImageBuildRefused) as caught:
        verify_raw_image(raw, replace(manifest, image=replace(manifest.image, size_bytes=1)))
    assert caught.value.code is ImageBuildRefusalCode.RAW_SIZE_MISMATCH

    with raw.open("r+b") as stream:
        stream.seek(446 + 8)
        stream.write((2).to_bytes(4, "little"))
    with pytest.raises(ImageBuildRefused) as caught:
        verify_raw_image(raw, manifest)
    assert caught.value.code is ImageBuildRefusalCode.RAW_GEOMETRY_MISMATCH


def test_output_requires_absolute_new_regular_image_and_never_input_in_place(
    tmp_path: Path,
) -> None:
    source = _archive(tmp_path)

    with pytest.raises(ImageBuildRefused) as caught:
        validate_output_path(source, Path("relative.img"))
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE

    with pytest.raises(ImageBuildRefused) as caught:
        validate_output_path(source, source)
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_EXISTS

    existing = tmp_path / "existing.img"
    existing.write_bytes(b"owner data")
    with pytest.raises(ImageBuildRefused) as caught:
        validate_output_path(source, existing)
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_EXISTS
    assert existing.read_bytes() == b"owner data"


def test_output_symlink_and_device_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _archive(tmp_path)
    target = tmp_path / "target.img"
    target.write_bytes(b"do not touch")
    link = tmp_path / "link.img"
    try:
        link.symlink_to(target)
    except OSError:
        original_lstat = Path.lstat

        def symlink_lstat(path: Path) -> object:
            if path == link:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", symlink_lstat)
    with pytest.raises(ImageBuildRefused) as caught:
        validate_output_path(source, link)
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE
    assert target.read_bytes() == b"do not touch"

    monkeypatch.undo()
    device = tmp_path / "device.img"
    original_lstat = Path.lstat

    def device_lstat(path: Path) -> object:
        if path == device:
            return SimpleNamespace(st_mode=stat.S_IFBLK)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", device_lstat)
    with pytest.raises(ImageBuildRefused) as caught:
        validate_output_path(source, device)
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE


def test_32gb_and_64gb_target_calculations_are_exact_and_aligned() -> None:
    manifest = _official()
    card_32 = calculate_target_layout(manifest, 31_457_280_000)
    card_64 = calculate_target_layout(manifest, 64_000_000_000)

    assert (card_32.root_start_sector, card_32.root_end_sector) == (1_064_960, 13_647_871)
    assert (card_32.data_start_sector, card_32.data_end_sector) == (
        13_647_872,
        61_437_951,
    )
    assert (card_64.root_start_sector, card_64.root_end_sector) == (1_064_960, 13_647_871)
    assert (card_64.data_start_sector, card_64.data_end_sector) == (
        13_647_872,
        124_997_631,
    )
    for layout in (card_32, card_64):
        assert layout.root_size_sectors * 512 == 6 * GIB
        assert layout.data_start_sector % 2_048 == 0
        assert (layout.data_end_sector + 1) % 2_048 == 0
        assert layout.total_sectors - layout.data_end_sector - 1 >= 2_048


def test_plan_is_deterministic_argv_only_and_does_not_create_or_mutate_files(
    tmp_path: Path,
) -> None:
    source = _archive(tmp_path)
    original = source.read_bytes()
    manifest = _fixture_manifest(source)
    output = tmp_path / "release.img"
    calls: list[tuple[str, ...]] = []

    first = author_image_build_plan(
        manifest=manifest,
        manifest_path=OFFICIAL_MANIFEST,
        source_archive=source,
        output_image=output,
        payload_root=PAYLOAD,
        executor=calls.append,
    )
    second = author_image_build_plan(
        manifest=manifest,
        manifest_path=OFFICIAL_MANIFEST,
        source_archive=source,
        output_image=output,
        payload_root=PAYLOAD,
        executor=calls.append,
    )

    assert first.to_dict() == second.to_dict()
    assert first.schema_version == 2
    assert calls == []
    assert not output.exists()
    assert source.read_bytes() == original
    assert not first.execution_supported
    assert not first.block_device_executor_included
    assert first.target_profile == "pi-zero-2-w-armv7l"
    closure_path = ROOT / "deploy" / "image" / "initramfs-closure-v1.json"
    assert first.initramfs_closure.path == str(closure_path.resolve())
    assert first.initramfs_closure.size_bytes == closure_path.stat().st_size
    assert first.initramfs_closure.sha256 == hashlib.sha256(closure_path.read_bytes()).hexdigest()
    assert first.to_dict()["target_profile"] == "pi-zero-2-w-armv7l"
    assert first.to_dict()["initramfs_closure"] == {
        "path": str(closure_path.resolve()),
        "size_bytes": closure_path.stat().st_size,
        "sha256": hashlib.sha256(closure_path.read_bytes()).hexdigest(),
    }
    assert all(isinstance(action.argv, tuple) for action in first.actions)
    assert all(
        action.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"} for action in first.actions
    )
    assert all(not isinstance(action.argv, str) for action in first.actions)


def test_plan_removes_only_stock_trigger_preserves_firstrun_and_installs_inert_runtime(
    tmp_path: Path,
) -> None:
    source = _archive(tmp_path)
    manifest = _fixture_manifest(source)
    plan = author_image_build_plan(
        manifest=manifest,
        manifest_path=OFFICIAL_MANIFEST,
        source_archive=source,
        output_image=tmp_path / "release.img",
        payload_root=PAYLOAD,
    )

    transform = next(
        action for action in plan.actions if action.kind is ImageBuildActionKind.TRANSFORM_CMDLINE
    )
    assert transform.argv[transform.argv.index("--remove-exact-token") + 1] == "resize"
    assert (
        transform.argv[transform.argv.index("--add-exact-token") + 1]
        == "dashcam.bounded_provision=v1"
    )
    assert "--preserve-all-other-tokens" in transform.argv
    rebuild = next(
        action for action in plan.actions if action.kind is ImageBuildActionKind.REBUILD_INITRAMFS
    )
    assert "--target-profile" in rebuild.argv
    assert "pi-zero-2-w-armv7l" in rebuild.argv
    assert "::initramfs7" in rebuild.argv
    assert "update-initramfs" not in rebuild.argv
    assert "all" not in rebuild.argv
    contract = json.loads((PAYLOAD / "firstboot-contract-v1.json").read_bytes())
    assert contract["status"] == "gated_candidate_pending_exact_image_validation"
    assert contract["placeholder_exit_code"] == 125
    serialized = json.dumps(contract).lower()
    assert serialized.index("backup before any table write") < serialized.index(
        "write only the bounded p2 end"
    )
    assert "shrinking is forbidden" in serialized
    assert "signature" in serialized
    assert "no reboot loop" in serialized
    for template in (PAYLOAD / "templates").glob("*.inert"):
        text = template.read_text(encoding="utf-8")
        assert "exit 125" in text
        assert "sfdisk" not in text
        assert "mkfs" not in text
    install = next(
        action
        for action in plan.actions
        if action.kind is ImageBuildActionKind.INSTALL_GATED_PAYLOAD
    )
    assert "--require-runtime-gate-absent" in install.argv


def test_execute_is_refused_without_calling_fake_executor_or_creating_output(
    tmp_path: Path,
) -> None:
    source = _archive(tmp_path)
    manifest = _fixture_manifest(source)
    output = tmp_path / "release.img"
    calls: list[tuple[str, ...]] = []

    with pytest.raises(ImageBuildRefused) as caught:
        author_image_build_plan(
            manifest=manifest,
            manifest_path=OFFICIAL_MANIFEST,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            dry_run=False,
            executor=calls.append,
        )

    assert caught.value.code is ImageBuildRefusalCode.EXECUTION_DISABLED
    assert calls == []
    assert not output.exists()
