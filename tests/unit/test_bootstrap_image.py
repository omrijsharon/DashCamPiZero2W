from __future__ import annotations

import hashlib
import json
import lzma
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashcam.provisioning import bootstrap_image as bootstrap_image_module
from dashcam.provisioning.bootstrap_image import (
    APT_PACKAGES,
    BOOTSTRAP_TOKEN,
    BUILT_RAW_SIZE,
    MINIMUM_PROJECTED_ROOT_FREE_BYTES,
    PACKAGE_INVENTORY,
    PINNED_SOURCE,
    READBACK_REQUIREMENTS,
    SECTOR_SIZE_BYTES,
    SOURCE_BOOT_SIZE_SECTORS,
    SOURCE_BOOT_START_SECTOR,
    SOURCE_MBR_DISK_ID,
    SOURCE_ROOT_START_SECTOR,
    ArtifactDigest,
    BootstrapImageRefusalCode,
    BootstrapImageRefused,
    BuildActionKind,
    BuildPaths,
    CommandResult,
    CompressionProof,
    EvidenceCheck,
    PinnedSource,
    SourceMetadata,
    VerificationEvidence,
    assert_root_free_projection,
    audit_cloudinit_no_root_expander,
    author_build_plan,
    cleanup_owned_work_files,
    decompress_pinned_source,
    default_command_runner,
    load_builder_requirements,
    make_imager_manifest,
    package_inventory_bytes,
    read_mbr_geometry,
    resolve_clean_app_commit,
    transform_cmdline,
    validate_build_paths,
    validate_new_manifest_output,
    validate_readback_result,
    verify_built_geometry,
    verify_ext4_size_offline,
    verify_only_p2_entry_changed,
    verify_zero_prefix,
)

ROOT = Path(__file__).parents[2]
LOCK = ROOT / "uv.lock"


def _metadata() -> SourceMetadata:
    return SourceMetadata(
        app_commit="1" * 40,
        package_lock_sha256=hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        app_wheel_sha256="2" * 64,
    )


def _source_verification(paths: BuildPaths) -> ArtifactDigest:
    return ArtifactDigest(
        path=str(paths.source_archive),
        size_bytes=PINNED_SOURCE.compressed_size_bytes,
        sha256=PINNED_SOURCE.compressed_sha256,
    )


def _paths(tmp_path: Path, *, source: Path | None = None) -> BuildPaths:
    actual_source = source or tmp_path / PINNED_SOURCE.filename
    if source is None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        actual_source.write_bytes(b"fixture")
    return BuildPaths(
        source_archive=actual_source,
        work_root=tmp_path / "work",
        raw_image=tmp_path / "work" / "dashcam-bootstrap-v1.img",
        compressed_image=tmp_path / "out" / "dashcam-bootstrap-v1.img.xz",
        imager_manifest=tmp_path / "out" / "dashcam-bootstrap-v1.rpi-imager-manifest",
    )


def _evidence(raw: ArtifactDigest) -> VerificationEvidence:
    return VerificationEvidence(
        schema_version=1,
        verifier="dashcam-bootstrap-v1-independent",
        passed=True,
        raw=raw,
        source_archive_sha256=PINNED_SOURCE.compressed_sha256,
        source_raw_sha256=PINNED_SOURCE.extracted_sha256,
        app_commit=_metadata().app_commit,
        package_lock_sha256=_metadata().package_lock_sha256,
        app_wheel_sha256=_metadata().app_wheel_sha256,
        projected_root_free_bytes=MINIMUM_PROJECTED_ROOT_FREE_BYTES,
        checks=tuple(
            EvidenceCheck(item.requirement_id, True, "checked")
            for item in READBACK_REQUIREMENTS
        ),
    )


def _write_mbr(
    path: Path,
    *,
    root_size: int,
    total_size: int,
    boot_start: int = SOURCE_BOOT_START_SECTOR,
    boot_size: int = SOURCE_BOOT_SIZE_SECTORS,
    root_start: int = SOURCE_ROOT_START_SECTOR,
) -> None:
    with path.open("xb") as stream:
        stream.truncate(total_size)
        sector = bytearray(SECTOR_SIZE_BYTES)
        sector[440:444] = int(SOURCE_MBR_DISK_ID[2:], 16).to_bytes(4, "little")
        for number, partition_type, start, size in (
            (1, 0x0C, boot_start, boot_size),
            (2, 0x83, root_start, root_size),
        ):
            offset = 446 + (number - 1) * 16
            sector[offset + 4] = partition_type
            sector[offset + 8 : offset + 12] = start.to_bytes(4, "little")
            sector[offset + 12 : offset + 16] = size.to_bytes(4, "little")
        sector[510:512] = b"\x55\xaa"
        stream.seek(0)
        stream.write(sector)


def test_cmdline_replaces_exactly_one_token_and_preserves_imager_tokens() -> None:
    original = (
        "console=serial0,115200 root=PARTUUID=aaaa-02 rootwait  resize  "
        "systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot "
        "systemd.unit=kernel-command-line.target\n"
    )
    transformed = transform_cmdline(original)

    assert transformed == original.replace("resize", BOOTSTRAP_TOKEN)
    assert "systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot" in transformed
    assert transformed.endswith("\n")


@pytest.mark.parametrize(
    "cmdline",
    [
        "rootwait",
        "resize rootwait resize",
        "rootwait autoresize",
        "resize dashcam.bootstrap=v1",
        "resize dashcam.bootstrap=v2",
        "resize\nsecond-line",
        "",
        "resize\x00",
    ],
)
def test_cmdline_rejects_missing_ambiguous_or_preexisting_layouts(cmdline: str) -> None:
    with pytest.raises(BootstrapImageRefused) as caught:
        transform_cmdline(cmdline)
    assert caught.value.code is BootstrapImageRefusalCode.CMDLINE_INVALID


def test_manifest_records_compressed_and_raw_hashes_sizes_deterministically() -> None:
    compressed = ArtifactDigest("/release/a.img.xz", 123, "a" * 64)
    raw = ArtifactDigest("/work/a.img", BUILT_RAW_SIZE, "b" * 64)
    proof = CompressionProof(compressed, raw)

    first = make_imager_manifest(
        proof=proof,
        evidence=_evidence(raw),
        artifact_url="https://example.invalid/a.img.xz",
        release_date="2026-07-25",
        metadata=_metadata(),
    )
    second = make_imager_manifest(
        proof=proof,
        evidence=_evidence(raw),
        artifact_url="https://example.invalid/a.img.xz",
        release_date="2026-07-25",
        metadata=_metadata(),
    )
    decoded = json.loads(first)
    image = decoded["os_list"][0]

    assert first == second
    assert first.endswith(b"\n")
    assert image["image_download_size"] == 123
    assert image["image_download_sha256"] == "a" * 64
    assert image["extract_size"] == BUILT_RAW_SIZE
    assert image["extract_sha256"] == "b" * 64
    assert decoded["dashcam_build"]["app_commit"] == "1" * 40


def test_manifest_rejects_unchecked_metadata_and_artifact_shapes() -> None:
    good = ArtifactDigest("/release/a.img.xz", 123, "a" * 64)
    raw = ArtifactDigest("/work/a.img", BUILT_RAW_SIZE, "b" * 64)

    with pytest.raises(BootstrapImageRefused):
        make_imager_manifest(
            proof=CompressionProof(replace(good, sha256="bad"), raw),
            evidence=_evidence(raw),
            artifact_url="https://example.invalid/a.img.xz",
            release_date="2026-07-25",
            metadata=_metadata(),
        )
    with pytest.raises(BootstrapImageRefused):
        make_imager_manifest(
            proof=CompressionProof(good, raw),
            evidence=_evidence(raw),
            artifact_url="/local/path",
            release_date="2026-07-25",
            metadata=_metadata(),
        )


def test_manifest_output_requires_existing_regular_parent_and_new_absolute_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "a.rpi-imager-manifest"
    validate_new_manifest_output(output)
    output.write_bytes(b"owner")
    with pytest.raises(BootstrapImageRefused) as caught:
        validate_new_manifest_output(output)
    assert caught.value.code is BootstrapImageRefusalCode.OUTPUT_EXISTS
    assert output.read_bytes() == b"owner"

    with pytest.raises(BootstrapImageRefused) as caught:
        validate_new_manifest_output(Path("relative.rpi-imager-manifest"))
    assert caught.value.code is BootstrapImageRefusalCode.PATH_UNSAFE


def test_paths_require_new_outputs_and_raw_inside_work_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    validate_build_paths(paths)

    paths.compressed_image.parent.mkdir()
    paths.compressed_image.write_bytes(b"owner")
    with pytest.raises(BootstrapImageRefused) as caught:
        validate_build_paths(paths)
    assert caught.value.code is BootstrapImageRefusalCode.OUTPUT_EXISTS
    assert paths.compressed_image.read_bytes() == b"owner"

    unsafe = replace(
        _paths(tmp_path / "other"),
        raw_image=(tmp_path / "outside.img"),
    )
    with pytest.raises(BootstrapImageRefused) as caught:
        validate_build_paths(unsafe)
    assert caught.value.code is BootstrapImageRefusalCode.PATH_UNSAFE


def test_paths_refuse_block_devices_without_touching_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    device = paths.raw_image
    original_lstat = Path.lstat

    def fake_lstat(path: Path) -> object:
        if path == device:
            return SimpleNamespace(st_mode=stat.S_IFBLK)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(BootstrapImageRefused) as caught:
        validate_build_paths(paths)
    assert caught.value.code is BootstrapImageRefusalCode.BLOCK_DEVICE

    windows_device = replace(paths, raw_image=Path(r"\\.\PhysicalDrive7"))
    with pytest.raises(BootstrapImageRefused) as caught:
        validate_build_paths(windows_device)
    assert caught.value.code is BootstrapImageRefusalCode.BLOCK_DEVICE


def test_plan_and_inventory_are_deterministic_and_have_no_legacy_actions(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = author_build_plan(paths, _metadata(), _source_verification(paths))
    second = author_build_plan(paths, _metadata(), _source_verification(paths))

    assert first.canonical_bytes() == second.canonical_bytes()
    assert tuple(sorted(set(APT_PACKAGES))) == APT_PACKAGES
    assert package_inventory_bytes() == PACKAGE_INVENTORY.canonical_bytes()
    assert first.raw_is_temporary is True
    assert first.block_device_targets_permitted is False
    assert first.modifies_initramfs is False
    assert BuildActionKind.CUSTOMIZE_CMDLINE in {action.kind for action in first.actions}
    serialized = first.canonical_bytes().decode()
    assert "rebuild_initramfs" not in serialized
    assert "partprobe" not in serialized
    assert "dashcam-bounded-provision" not in serialized
    assert "firstboot-initramfs" not in serialized


def test_cleanup_only_unlinks_registered_regular_files_after_verification(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    raw = work / "image.img"
    scratch = work / "scratch"
    owner = work / "owner.txt"
    raw.write_bytes(b"raw")
    scratch.write_bytes(b"scratch")
    owner.write_bytes(b"keep")

    with pytest.raises(BootstrapImageRefused) as caught:
        cleanup_owned_work_files(
            work_root=work,
            owned_files=(raw, scratch),
            compressed_verified=False,
        )
    assert caught.value.code is BootstrapImageRefusalCode.CLEANUP_NOT_VERIFIED
    assert raw.exists()

    removed = cleanup_owned_work_files(
        work_root=work,
        owned_files=(raw, scratch),
        compressed_verified=True,
    )
    assert removed == (raw, scratch)
    assert owner.read_bytes() == b"keep"

    outside = tmp_path / "outside"
    outside.write_bytes(b"owner")
    with pytest.raises(BootstrapImageRefused) as caught:
        cleanup_owned_work_files(
            work_root=work,
            owned_files=(outside,),
            compressed_verified=True,
        )
    assert caught.value.code is BootstrapImageRefusalCode.CLEANUP_NOT_OWNED
    assert outside.read_bytes() == b"owner"


def test_readback_contract_is_complete_and_closed() -> None:
    identifiers = {item.requirement_id for item in READBACK_REQUIREMENTS}
    assert len(identifiers) == len(READBACK_REQUIREMENTS)
    assert {
        "fat.cmdline.bootstrap",
        "fat.cmdline.imager-preserved",
        "fat.cloudinit-seed-preserved",
        "fat.initramfs-unchanged",
        "ext4.no-initramfs-hooks",
        "ext4.app",
        "ext4.environment",
        "ext4.package-inventory",
        "ext4.source-metadata",
        "ext4.storage-payload",
        "ext4.network-payload",
        "ext4.services",
        "ext4.cloudinit",
        "ext4.cloudinit-no-root-expander",
        "ext4.dependencies",
        "ext4.root-free-projection",
        "raw.mbr-geometry",
        "raw.future-p3-zero-prefix",
    } == identifiers

    passing = dict.fromkeys(identifiers, True)
    validate_readback_result(passing)
    with pytest.raises(BootstrapImageRefused):
        validate_readback_result(
            {key: value for key, value in passing.items() if key != "ext4.app"}
        )
    passing["ext4.app"] = False
    with pytest.raises(BootstrapImageRefused):
        validate_readback_result(passing)


def test_six_gib_projection_requires_at_least_two_gib_free() -> None:
    projected = assert_root_free_projection(
        current_filesystem_bytes=2 * 1024**3,
        current_free_bytes=100,
    )
    assert projected > MINIMUM_PROJECTED_ROOT_FREE_BYTES

    with pytest.raises(BootstrapImageRefused) as caught:
        assert_root_free_projection(
            current_filesystem_bytes=6 * 1024**3,
            current_free_bytes=MINIMUM_PROJECTED_ROOT_FREE_BYTES - 1,
        )
    assert caught.value.code is BootstrapImageRefusalCode.ARTIFACT_INVALID


def test_checked_recipe_references_owner_payloads_and_forbids_initramfs() -> None:
    recipe = (ROOT / "deploy/bootstrap/image/pi-gen-stage/01-run.sh").read_text()
    readme = (ROOT / "deploy/bootstrap/image/README.md").read_text()
    packages = (ROOT / "deploy/bootstrap/image/pi-gen-stage/00-packages").read_text().splitlines()

    assert "deploy/bootstrap/storage" in readme
    assert "deploy/bootstrap/network" in readme
    assert "${assets}/storage" in recipe
    assert "${assets}/network" in recipe
    assert packages == list(APT_PACKAGES)
    assert "update-initramfs" not in recipe
    assert "partprobe" not in recipe
    assert "dashcam-bounded-provision" in recipe  # refusal tripwire only
    assert "firstboot-initramfs" in recipe  # refusal tripwire only


def test_payload_installers_are_fail_closed_and_enable_only_bootstrap_services() -> None:
    storage = (ROOT / "deploy/bootstrap/storage/install.sh").read_text()
    network = (ROOT / "deploy/bootstrap/network/install.sh").read_text()
    recipe = (ROOT / "deploy/bootstrap/image/pi-gen-stage/01-run.sh").read_text()

    for installer in (storage, network):
        assert 'rootfs="$(realpath -e -- "${rootfs_input}")"' in installer
        assert '[ "${rootfs}" != "/" ]' in installer
        assert "symbolic destination refused" in installer
        assert "non-regular destination refused" in installer
        assert "multiply-linked destination refused" in installer
        assert "/dev/mmc" not in installer
        assert "partprobe" not in installer
        assert "update-initramfs" not in installer

    assert "bootstrap-v1-authorization.json" in storage
    assert "var/lib/dashcam/provisioning" in storage
    assert "var/lib/dashcam/network" in storage
    assert "srv/dashcam" in storage
    assert "dashcam-bootstrap-stage-a.service" in storage
    assert "dashcam-bootstrap-stage-b.service" in storage
    assert "enable_unit dashcamd.service" not in storage
    assert "enable_unit dashcam-storage-check.service" in storage
    assert "/opt/dashcam/venv/bin/python -m dashcam.provisioning.bootstrap" in storage
    assert "cloud-final.service" in storage

    assert "etc/NetworkManager/system-connections" in network
    assert "var/lib/dashcam/network" in network
    assert "dashcam-network-fallback.service" in network
    assert "dashcamd.service" not in network
    assert "/bin/bash" in recipe
    assert "test ! -e" in recipe
    assert "multi-user.target.wants/dashcamd.service" in recipe
    assert "multi-user.target.wants/dashcam-storage-check.service" in recipe
    assert "ConditionPathExists=/var/lib/dashcam/provisioning/layout-v1.complete.json" in recipe

    installed_config = tomllib.loads((ROOT / "config/default.toml").read_text())
    assert installed_config["gps"]["baud"] == 115_200


def test_recipe_preserves_cloudinit_rpi_customization_contract() -> None:
    recipe = (ROOT / "deploy/bootstrap/image/pi-gen-stage/01-run.sh").read_text()
    readme = (ROOT / "deploy/bootstrap/image/README.md").read_text()
    source = json.loads((ROOT / "deploy/bootstrap/image/source.json").read_text())
    network_unit = (
        ROOT / "deploy/bootstrap/network/dashcam-network-fallback.service"
    ).read_text()

    assert source["extracted_size_bytes"] == 2_675_965_952
    assert source["init_format"] == "cloudinit-rpi"
    assert (
        source["extracted_sha256"]
        == "235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2"
    )
    assert "cloud-final.service" in recipe
    assert "etc/cloud/cloud.cfg" in recipe
    assert "etc/cloud/cloud.cfg.d/99_raspberry-pi.cfg" in recipe
    assert "cloudinit/config/cc_raspberry_pi.py" in recipe
    assert "cloudinit/distros/raspberry_pi_os.py" in recipe
    assert "25.2-1~bpo13+1+rpt20" in recipe
    assert "1:20260612" in recipe
    assert "${BOOTFS_DIR}/meta-data" in recipe
    assert "${BOOTFS_DIR}/network-config" in recipe
    assert "${BOOTFS_DIR}/user-data" in recipe
    assert "cloud-init" in recipe
    assert "boot-before." in recipe
    assert "non-cmdline boot/seed file changed" in recipe
    assert "cloudinit-rpi" in readme
    assert "these source packages are preserved" in readme
    assert "FAT seed-file inventory/hashes" in readme
    assert "pre-customization extracted" in readme
    assert "Wants=cloud-final.service" in network_unit
    assert "After=NetworkManager.service cloud-final.service" in network_unit


def test_recipe_requires_offline_four_gib_root_expansion_before_customization() -> None:
    requirements = json.loads(
        (ROOT / "deploy/bootstrap/image/build-requirements.json").read_text()
    )
    readme = (ROOT / "deploy/bootstrap/image/README.md").read_text()

    assert requirements["schema_version"] == 2
    assert requirements["builder_container_digest"].startswith("REQUIRED_")
    assert requirements["binfmt_marker"] == "/proc/sys/fs/binfmt_misc/qemu-arm"
    assert {tool["name"] for tool in requirements["tools"]} == {
        "bash",
        "git",
        "guestfish",
        "guestmount",
        "guestunmount",
        "qemu-arm",
    }
    assert "offline pre-customization expansion" in readme
    assert "only about 298 MiB unreserved ext4 free" in readme
    assert "bind the 8,388,608-sector p2" in readme
    assert "6,991,904,768 bytes" in readme
    assert "partprobe" in readme  # explicit prohibition

    prepare = (ROOT / "deploy/bootstrap/image/prepare-stage.sh").read_text()
    stage = (ROOT / "deploy/bootstrap/image/pi-gen-stage/01-run.sh").read_text()
    assert "build-metadata/build-requirements.json" in prepare
    assert "build-metadata/official-source.json" in prepare
    assert "build-metadata/build-requirements.json" in stage
    assert "build-metadata/official-source.json" in stage


def test_pinned_source_decompression_proves_both_identities_and_cleans_failure(
    tmp_path: Path,
) -> None:
    raw_payload = b"official raw fixture" * 100
    archive_payload = lzma.compress(raw_payload, format=lzma.FORMAT_XZ)
    archive = tmp_path / "source.img.xz"
    archive.write_bytes(archive_payload)
    pin = PinnedSource(
        filename=archive.name,
        url="https://example.invalid/source.img.xz",
        compressed_size_bytes=len(archive_payload),
        compressed_sha256=hashlib.sha256(archive_payload).hexdigest(),
        extracted_size_bytes=len(raw_payload),
        extracted_sha256=hashlib.sha256(raw_payload).hexdigest(),
    )
    output = tmp_path / "source.img"

    result = decompress_pinned_source(archive, output, source=pin)
    assert result.sha256 == pin.extracted_sha256
    assert output.read_bytes() == raw_payload

    mismatch = replace(pin, extracted_sha256="0" * 64)
    failed_output = tmp_path / "failed.img"
    with pytest.raises(BootstrapImageRefused) as caught:
        decompress_pinned_source(archive, failed_output, source=mismatch)
    assert caught.value.code is BootstrapImageRefusalCode.EXTRACTED_HASH_MISMATCH
    assert not failed_output.exists()


def test_built_mbr_and_full_future_p3_zero_prefix_are_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production geometry is nearly 7 GB. Exercise the exact same verifier
    # logic with compact injected geometry so a unit test cannot exhaust the
    # Windows system drive.
    boot_start = 8
    boot_size = 16
    root_start = 32
    root_size = 64
    future_p3_start = 128
    zero_prefix_bytes = 4096
    raw_size = future_p3_start * SECTOR_SIZE_BYTES + zero_prefix_bytes
    monkeypatch.setattr(bootstrap_image_module, "SOURCE_BOOT_START_SECTOR", boot_start)
    monkeypatch.setattr(bootstrap_image_module, "SOURCE_BOOT_SIZE_SECTORS", boot_size)
    monkeypatch.setattr(bootstrap_image_module, "SOURCE_ROOT_START_SECTOR", root_start)
    monkeypatch.setattr(bootstrap_image_module, "BUILD_ROOT_SIZE_SECTORS", root_size)
    monkeypatch.setattr(bootstrap_image_module, "FUTURE_P3_START_SECTOR", future_p3_start)
    monkeypatch.setattr(bootstrap_image_module, "ZERO_PREFIX_BYTES", zero_prefix_bytes)
    monkeypatch.setattr(bootstrap_image_module, "BUILT_RAW_SIZE", raw_size)

    raw = tmp_path / "built.img"
    _write_mbr(
        raw,
        root_size=root_size,
        total_size=raw_size,
        boot_start=boot_start,
        boot_size=boot_size,
        root_start=root_start,
    )

    geometry = verify_built_geometry(raw)
    verify_zero_prefix(raw)

    assert geometry.disk_id == SOURCE_MBR_DISK_ID
    assert geometry.partitions[0].start_sector == boot_start
    assert geometry.partitions[1].size_sectors == root_size
    assert raw.stat().st_size == raw_size

    with raw.open("r+b") as stream:
        stream.seek(future_p3_start * SECTOR_SIZE_BYTES + zero_prefix_bytes - 1)
        stream.write(b"\x01")
    with pytest.raises(BootstrapImageRefused) as caught:
        verify_zero_prefix(raw)
    assert caught.value.code is BootstrapImageRefusalCode.ZERO_PREFIX_MISMATCH


def test_mbr_parser_rejects_missing_signature(tmp_path: Path) -> None:
    raw = tmp_path / "bad.img"
    raw.write_bytes(b"\x00" * 512)
    with pytest.raises(BootstrapImageRefused) as caught:
        read_mbr_geometry(raw)
    assert caught.value.code is BootstrapImageRefusalCode.GEOMETRY_MISMATCH


def test_offline_growth_may_change_only_the_second_mbr_entry() -> None:
    source = bytearray(SECTOR_SIZE_BYTES)
    source[510:512] = b"\x55\xaa"
    built = bytearray(source)
    p2_start = 446 + 16
    built[p2_start : p2_start + 16] = bytes(range(16))

    verify_only_p2_entry_changed(bytes(source), bytes(built))

    built[440] = 1
    with pytest.raises(BootstrapImageRefused) as caught:
        verify_only_p2_entry_changed(bytes(source), bytes(built))
    assert caught.value.code is BootstrapImageRefusalCode.GEOMETRY_MISMATCH


def test_offline_ext4_size_is_reopened_and_checked_before_customization(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "image.img"
    tool = tmp_path / "guestfish"
    raw.write_bytes(b"raw")
    tool.write_bytes(b"tool")
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def runner(
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del env
        calls.append((tuple(argv), input_text))
        return CommandResult(0, f"ext4\n{4 * 1024**3}\n", "")

    verify_ext4_size_offline(raw, guestfish=tool, runner=runner)

    assert calls == [
        (
            (str(tool), "--ro", "--format=raw", "-a", str(raw)),
            "run\nmount-ro /dev/sda2 /\nvfs-type /dev/sda2\nvfs-size /\n",
        )
    ]


def test_checked_builder_requirements_refuse_placeholders() -> None:
    payload = (ROOT / "deploy/bootstrap/image/build-requirements.json").read_bytes()
    with pytest.raises(BootstrapImageRefused) as caught:
        load_builder_requirements(payload)
    assert caught.value.code is BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED


def test_default_command_runner_rejects_output_beyond_its_memory_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_image_module, "MAX_COMMAND_OUTPUT_BYTES", 16)

    result = default_command_runner((sys.executable, "-c", "print('x' * 32)"))

    assert result.returncode == 125
    assert result.stdout == ""
    assert result.stderr == "command output exceeded the closed size limit"


def test_unborn_repository_refuses_before_dirty_tree_probes(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del input_text, env
        values = tuple(argv)
        calls.append(values)
        return CommandResult(128, "", "fatal: ambiguous argument HEAD")

    with pytest.raises(BootstrapImageRefused) as caught:
        resolve_clean_app_commit(repository, runner=runner)
    assert caught.value.code is BootstrapImageRefusalCode.REPOSITORY_UNCOMMITTED
    assert calls == [("git", "-C", str(repository), "rev-parse", "--verify", "HEAD")]


def test_cloudinit_audit_keeps_imager_support_without_root_expanders(tmp_path: Path) -> None:
    root = tmp_path / "root"
    cloud = root / "etc/cloud"
    module = root / "usr/lib/python3/dist-packages/cloudinit/config/cc_raspberry_pi.py"
    (cloud / "cloud.cfg.d").mkdir(parents=True)
    module.parent.mkdir(parents=True)
    module.write_text("# raspberry_pi module\n")
    (cloud / "cloud.cfg").write_text(
        "cloud_final_modules:\n  - scripts-user\n  - final-message\n"
    )
    pi_cfg = cloud / "cloud.cfg.d/99_raspberry-pi.cfg"
    pi_cfg.write_text("datasource:\n  NoCloud:\n    seedfrom: file:///boot/firmware/\n")

    assert "growpart/resizefs absent" in audit_cloudinit_no_root_expander(root)

    (cloud / "cloud.cfg").write_text("cloud_init_modules:\n  - growpart\n")
    with pytest.raises(ValueError, match="growpart"):
        audit_cloudinit_no_root_expander(root)


def test_stage_inventory_excludes_its_own_checksum_and_is_verified_before_use() -> None:
    prepare = (ROOT / "deploy/bootstrap/image/prepare-stage.sh").read_text()
    stage = (ROOT / "deploy/bootstrap/image/pi-gen-stage/01-run.sh").read_text()

    assert "! -path './build-metadata/stage-files.sha256'" in prepare
    assert "sha256sum --check --strict build-metadata/stage-files.sha256" in stage
    assert stage.index("sha256sum --check --strict") < stage.index("cp -a --no-dereference")
    assert "build-metadata/app-wheel.sha256" in prepare
    assert "import-smoke.txt" in stage


def test_plan_only_script_cannot_bypass_the_executor_verifier_manifest_path() -> None:
    planner = (ROOT / "scripts/build_bootstrap_image.py").read_text()

    assert "--write-manifest" not in planner
    assert "make_imager_manifest" not in planner
    assert "execute_bootstrap_image.py" in planner
