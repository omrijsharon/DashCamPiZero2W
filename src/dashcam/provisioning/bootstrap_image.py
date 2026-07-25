"""Reproducible, regular-file-only build support for DashCam Bootstrap v1.

The target is always a new regular image file, never removable media or a block
device. Linux-only execution exposes filesystems through libguestfs/FUSE,
without loop devices or kernel partition-table rereads. The module also authors
the closed plan, performs the sole command-line transformation, verifies
readback, and constrains cleanup to registered tool-created files.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, Protocol, cast

HASH_CHUNK_BYTES: Final = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 4 * 1024 * 1024
MAX_COMMAND_INPUT_BYTES: Final = 1024 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 3600
BOOTSTRAP_TOKEN: Final = "dashcam.bootstrap=v1"
ROOT_TARGET_BYTES: Final = 6 * 1024**3
MINIMUM_PROJECTED_ROOT_FREE_BYTES: Final = 2 * 1024**3
SOURCE_ARCHIVE_NAME: Final = "2026-06-18-raspios-trixie-armhf-lite.img.xz"
SOURCE_ARCHIVE_URL: Final = (
    "https://downloads.raspberrypi.com/raspios_lite_armhf/images/"
    "raspios_lite_armhf-2026-06-19/"
    "2026-06-18-raspios-trixie-armhf-lite.img.xz"
)
SOURCE_ARCHIVE_SIZE: Final = 549_086_704
SOURCE_ARCHIVE_SHA256: Final = (
    "ea4e84c501d6dd4f4b1d04eb84df133a03f90a05ee2e8ab849185c17c2b0707b"
)
SOURCE_RAW_SIZE: Final = 2_675_965_952
SOURCE_RAW_SHA256: Final = (
    "235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2"
)
SECTOR_SIZE_BYTES: Final = 512
SOURCE_MBR_DISK_ID: Final = "0x4f2c9ea0"
SOURCE_BOOT_START_SECTOR: Final = 16_384
SOURCE_BOOT_SIZE_SECTORS: Final = 1_048_576
SOURCE_ROOT_START_SECTOR: Final = 1_064_960
SOURCE_ROOT_SIZE_SECTORS: Final = 4_161_536
BUILD_ROOT_SIZE_BYTES: Final = 4 * 1024**3
BUILD_ROOT_SIZE_SECTORS: Final = BUILD_ROOT_SIZE_BYTES // SECTOR_SIZE_BYTES
BUILD_ROOT_END_SECTOR: Final = SOURCE_ROOT_START_SECTOR + BUILD_ROOT_SIZE_SECTORS - 1
RUNTIME_ROOT_SIZE_SECTORS: Final = ROOT_TARGET_BYTES // SECTOR_SIZE_BYTES
FUTURE_P3_START_SECTOR: Final = SOURCE_ROOT_START_SECTOR + RUNTIME_ROOT_SIZE_SECTORS
ZERO_PREFIX_BYTES: Final = 4 * 1024**2
BUILT_RAW_SIZE: Final = FUTURE_P3_START_SECTOR * SECTOR_SIZE_BYTES + ZERO_PREFIX_BYTES
APP_INSTALL_ROOT: Final = "/opt/dashcam"
APP_VENV: Final = "/opt/dashcam/venv"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_BOOTSTRAP_TOKEN_RE = re.compile(r"dashcam\.bootstrap=\S+")
_RESIZE_TOKEN_RE = re.compile(r"(?<!\S)resize(?!\S)")
_WINDOWS_DEVICE_RE = re.compile(
    r"^(?:\\\\[.?]\\)?(?:physicaldrive|harddisk|cdrom|tape)\d+(?:\\.*)?$",
    re.IGNORECASE,
)


class BootstrapImageRefusalCode(StrEnum):
    """Stable refusal codes for build automation and tests."""

    CMDLINE_INVALID = "cmdline_invalid"
    SOURCE_INVALID = "source_invalid"
    SOURCE_SIZE_MISMATCH = "source_size_mismatch"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    PATH_UNSAFE = "path_unsafe"
    OUTPUT_EXISTS = "output_exists"
    BLOCK_DEVICE = "block_device"
    METADATA_INVALID = "metadata_invalid"
    ARTIFACT_INVALID = "artifact_invalid"
    CLEANUP_NOT_VERIFIED = "cleanup_not_verified"
    CLEANUP_NOT_OWNED = "cleanup_not_owned"
    EXTRACTED_SIZE_MISMATCH = "extracted_size_mismatch"
    EXTRACTED_HASH_MISMATCH = "extracted_hash_mismatch"
    GEOMETRY_MISMATCH = "geometry_mismatch"
    ZERO_PREFIX_MISMATCH = "zero_prefix_mismatch"
    TOOLCHAIN_UNRESOLVED = "toolchain_unresolved"
    TOOLCHAIN_MISMATCH = "toolchain_mismatch"
    COMMAND_FAILED = "command_failed"
    REPOSITORY_UNCOMMITTED = "repository_uncommitted"
    EVIDENCE_INVALID = "evidence_invalid"
    PLATFORM_UNSUPPORTED = "platform_unsupported"


class BootstrapImageRefused(ValueError):
    """A fail-closed Bootstrap image contract violation."""

    def __init__(self, code: BootstrapImageRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PinnedSource:
    """Exact official Raspberry Pi OS Lite 32-bit source archive."""

    filename: str = SOURCE_ARCHIVE_NAME
    url: str = SOURCE_ARCHIVE_URL
    compressed_size_bytes: int = SOURCE_ARCHIVE_SIZE
    compressed_sha256: str = SOURCE_ARCHIVE_SHA256
    extracted_size_bytes: int = SOURCE_RAW_SIZE
    extracted_sha256: str = SOURCE_RAW_SHA256
    architecture: str = "armhf"
    os_release: str = "Raspberry Pi OS Lite 32-bit (Debian Trixie), 2026-06-18"
    init_format: str = "cloudinit-rpi"


PINNED_SOURCE: Final = PinnedSource()


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    """Size and SHA-256 identity of one regular-file artifact."""

    path: str
    size_bytes: int
    sha256: str

    @classmethod
    def read(cls, path: Path) -> ArtifactDigest:
        _require_existing_regular_file(path, "artifact")
        return cls(path=str(path), size_bytes=path.stat().st_size, sha256=_sha256_file(path))


@dataclass(frozen=True, slots=True)
class BuildPaths:
    """Paths in a build plan; raw and work products are always temporary."""

    source_archive: Path
    work_root: Path
    raw_image: Path
    compressed_image: Path
    imager_manifest: Path


class BuildActionKind(StrEnum):
    """Closed set of Bootstrap v1 build phases."""

    VERIFY_PINNED_SOURCE = "verify_pinned_source"
    PREPARE_PI_GEN_STAGE = "prepare_pi_gen_stage"
    BUILD_TEMPORARY_RAW_IMAGE = "build_temporary_raw_image"
    VERIFY_EXTRACTED_SOURCE = "verify_extracted_source"
    GROW_ROOT_OFFLINE = "grow_root_offline"
    AUTHOR_ZERO_PREFIX = "author_zero_prefix"
    CUSTOMIZE_CMDLINE = "customize_cmdline"
    INSTALL_ROOTFS_PAYLOAD = "install_rootfs_payload"
    VERIFY_RAW_READBACK = "verify_raw_readback"
    COMPRESS_RAW_IMAGE = "compress_raw_image"
    VERIFY_COMPRESSED_ARTIFACT = "verify_compressed_artifact"
    WRITE_IMAGER_MANIFEST = "write_imager_manifest"
    CLEAN_TOOL_WORK_FILES = "clean_tool_work_files"


@dataclass(frozen=True, slots=True)
class BuildAction:
    sequence: int
    kind: BuildActionKind
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    mutates_source: bool
    note: str


@dataclass(frozen=True, slots=True)
class PackageInventory:
    """Deterministic declared target package and application inventory."""

    apt_packages: tuple[str, ...]
    app_install_root: str
    virtual_environment: str
    python_lock_path: str
    storage_payload_source: str
    network_payload_source: str
    bootstrap_image_stage: str

    def canonical_bytes(self) -> bytes:
        return (json.dumps(asdict(self), indent=2, sort_keys=True) + "\n").encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


APT_PACKAGES: Final = tuple(
    sorted(
        {
            "ca-certificates",
            "dosfstools",
            "e2fsprogs",
            "exfatprogs",
            "fdisk",
            "ffmpeg",
            "gstreamer1.0-alsa",
            "gstreamer1.0-libcamera",
            "gstreamer1.0-plugins-bad",
            "gstreamer1.0-plugins-base",
            "gstreamer1.0-plugins-good",
            "gstreamer1.0-plugins-ugly",
            "gstreamer1.0-tools",
            "iproute2",
            "iw",
            "libcamera-tools",
            "network-manager",
            "python3",
            "python3-pip",
            "python3-venv",
            "rfkill",
            "rpicam-apps-lite",
            "systemd",
            "tzdata",
            "util-linux",
        }
    )
)

PACKAGE_INVENTORY: Final = PackageInventory(
    apt_packages=APT_PACKAGES,
    app_install_root=APP_INSTALL_ROOT,
    virtual_environment=APP_VENV,
    python_lock_path=f"{APP_INSTALL_ROOT}/build-metadata/uv.lock",
    storage_payload_source="deploy/bootstrap/storage",
    network_payload_source="deploy/bootstrap/network",
    bootstrap_image_stage="deploy/bootstrap/image/pi-gen-stage",
)


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Metadata embedded in and read back from the built root filesystem."""

    app_commit: str
    package_lock_sha256: str
    app_wheel_sha256: str
    source_archive_sha256: str = SOURCE_ARCHIVE_SHA256
    source_archive_url: str = SOURCE_ARCHIVE_URL
    source_raw_sha256: str = SOURCE_RAW_SHA256
    init_format: str = "cloudinit-rpi"
    build_root_size_bytes: int = BUILD_ROOT_SIZE_BYTES
    built_raw_size_bytes: int = BUILT_RAW_SIZE
    package_inventory_sha256: str = PACKAGE_INVENTORY.sha256

    def validate(self) -> None:
        if _COMMIT_RE.fullmatch(self.app_commit) is None:
            _refuse(
                BootstrapImageRefusalCode.METADATA_INVALID,
                "app commit must be a full lowercase 40-character Git commit",
            )
        for label, value in (
            ("package lock", self.package_lock_sha256),
            ("application wheel", self.app_wheel_sha256),
            ("source archive", self.source_archive_sha256),
            ("source raw", self.source_raw_sha256),
            ("package inventory", self.package_inventory_sha256),
        ):
            if _SHA256_RE.fullmatch(value) is None:
                _refuse(
                    BootstrapImageRefusalCode.METADATA_INVALID,
                    f"{label} SHA-256 must be 64 lowercase hexadecimal characters",
                )
        if self.source_archive_sha256 != SOURCE_ARCHIVE_SHA256:
            _refuse(
                BootstrapImageRefusalCode.METADATA_INVALID,
                "source metadata does not identify the pinned archive",
            )
        if (
            self.source_raw_sha256 != SOURCE_RAW_SHA256
            or self.init_format != PINNED_SOURCE.init_format
            or self.build_root_size_bytes != BUILD_ROOT_SIZE_BYTES
            or self.built_raw_size_bytes != BUILT_RAW_SIZE
        ):
            _refuse(
                BootstrapImageRefusalCode.METADATA_INVALID,
                "source metadata does not identify the extracted/build geometry contract",
            )
        if self.package_inventory_sha256 != PACKAGE_INVENTORY.sha256:
            _refuse(
                BootstrapImageRefusalCode.METADATA_INVALID,
                "source metadata does not identify the declared package inventory",
            )

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_json(asdict(self))


@dataclass(frozen=True, slots=True)
class ReadbackRequirement:
    """One independent post-build inspection requirement."""

    requirement_id: str
    filesystem: str
    path: str
    assertion: str


READBACK_REQUIREMENTS: Final = (
    ReadbackRequirement(
        "fat.cmdline.bootstrap",
        "fat",
        "/cmdline.txt",
        "contains exactly one dashcam.bootstrap=v1 token and no standalone resize token",
    ),
    ReadbackRequirement(
        "fat.cmdline.imager-preserved",
        "fat",
        "/cmdline.txt",
        "all unrelated source and defensive firstrun/systemd.run tokens are preserved",
    ),
    ReadbackRequirement(
        "fat.cloudinit-seed-preserved",
        "fat",
        "/",
        "cloudinit-rpi seed files and every non-cmdline FAT file match the official source",
    ),
    ReadbackRequirement(
        "fat.initramfs-unchanged",
        "fat",
        "/",
        (
            "every source initramfs/initrd file has the same path, size, and SHA-256 "
            "as the source image"
        ),
    ),
    ReadbackRequirement(
        "ext4.no-initramfs-hooks",
        "ext4",
        "/etc/initramfs-tools",
        "contains no DashCam hook, script, module, or legacy first-boot trigger",
    ),
    ReadbackRequirement(
        "ext4.app",
        "ext4",
        APP_INSTALL_ROOT,
        "contains the current app payload identified by embedded full Git commit",
    ),
    ReadbackRequirement(
        "ext4.environment",
        "ext4",
        APP_VENV,
        "contains the installed locked environment and recorded successful target import smoke",
    ),
    ReadbackRequirement(
        "ext4.package-inventory",
        "ext4",
        f"{APP_INSTALL_ROOT}/build-metadata/package-inventory.json",
        "matches the deterministic declared package inventory and installed dpkg inventory",
    ),
    ReadbackRequirement(
        "ext4.source-metadata",
        "ext4",
        f"{APP_INSTALL_ROOT}/build-metadata/source.json",
        "matches source archive hash/URL, app commit, package-lock hash, and inventory hash",
    ),
    ReadbackRequirement(
        "ext4.storage-payload",
        "ext4",
        f"{APP_INSTALL_ROOT}/bootstrap/storage",
        "matches the deploy/bootstrap/storage payload inventory",
    ),
    ReadbackRequirement(
        "ext4.network-payload",
        "ext4",
        f"{APP_INSTALL_ROOT}/bootstrap/network",
        "matches the deploy/bootstrap/network payload inventory",
    ),
    ReadbackRequirement(
        "ext4.services",
        "ext4",
        "/etc/systemd/system",
        (
            "contains enabled Bootstrap Stage A/Stage B/network units ordered after cloud-final "
            "while recorder writes remain disabled"
        ),
    ),
    ReadbackRequirement(
        "ext4.cloudinit",
        "ext4",
        "/usr/lib/systemd/system/cloud-final.service",
        "cloud-init is installed and the image retains cloudinit-rpi service/module contracts",
    ),
    ReadbackRequirement(
        "ext4.cloudinit-no-root-expander",
        "ext4",
        "/etc/cloud",
        (
            "growpart/resizefs modules remain absent while raspberry_pi and NoCloud "
            "file:///boot/firmware support remain"
        ),
    ),
    ReadbackRequirement(
        "ext4.dependencies",
        "ext4",
        "/var/lib/dpkg/status",
        "all declared target packages are installed at captured exact versions",
    ),
    ReadbackRequirement(
        "ext4.root-free-projection",
        "ext4",
        "/",
        "projected free bytes after online growth to 6 GiB are at least 2 GiB",
    ),
    ReadbackRequirement(
        "raw.mbr-geometry",
        "raw",
        "MBR",
        "preserves source disk ID/p1 and has exact 4 GiB p2 with empty p3/p4 entries",
    ),
    ReadbackRequirement(
        "raw.future-p3-zero-prefix",
        "raw",
        str(FUTURE_P3_START_SECTOR),
        "the full 4 MiB extent at the future Stage A p3 start is all zero",
    ),
)


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Serializable plan for a Linux image builder."""

    schema_version: int
    generation: str
    source: PinnedSource
    paths: BuildPaths
    metadata: SourceMetadata
    package_inventory: PackageInventory
    readback_requirements: tuple[ReadbackRequirement, ...]
    actions: tuple[BuildAction, ...]
    raw_is_temporary: bool
    block_device_targets_permitted: bool
    modifies_initramfs: bool
    minimum_projected_root_free_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "source": asdict(self.source),
            "paths": {key: str(value) for key, value in asdict(self.paths).items()},
            "metadata": asdict(self.metadata),
            "package_inventory": asdict(self.package_inventory),
            "readback_requirements": [
                asdict(requirement) for requirement in self.readback_requirements
            ],
            "actions": [
                {
                    **asdict(action),
                    "kind": action.kind.value,
                    "inputs": list(action.inputs),
                    "outputs": list(action.outputs),
                }
                for action in self.actions
            ],
            "raw_is_temporary": self.raw_is_temporary,
            "block_device_targets_permitted": self.block_device_targets_permitted,
            "modifies_initramfs": self.modifies_initramfs,
            "minimum_projected_root_free_bytes": self.minimum_projected_root_free_bytes,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())


def transform_cmdline(cmdline: str) -> str:
    """Replace exactly one standalone stock ``resize`` token.

    Whitespace, line ending, token order, and every other token are preserved.
    In particular this leaves Raspberry Pi Imager ``firstrun`` and
    ``systemd.run*`` arguments byte-for-byte unchanged.
    """

    if not cmdline or "\x00" in cmdline:
        _refuse(BootstrapImageRefusalCode.CMDLINE_INVALID, "cmdline is empty or contains NUL")
    content = cmdline[:-1] if cmdline.endswith("\n") else cmdline
    if "\n" in content or "\r" in content:
        _refuse(
            BootstrapImageRefusalCode.CMDLINE_INVALID,
            "cmdline must contain exactly one physical line",
        )
    tokens = content.split()
    resize_count = tokens.count("resize")
    if resize_count != 1:
        _refuse(
            BootstrapImageRefusalCode.CMDLINE_INVALID,
            f"cmdline must contain exactly one standalone resize token; found {resize_count}",
        )
    bootstrap_tokens = [token for token in tokens if _BOOTSTRAP_TOKEN_RE.fullmatch(token)]
    if bootstrap_tokens:
        _refuse(
            BootstrapImageRefusalCode.CMDLINE_INVALID,
            "cmdline already contains a dashcam.bootstrap token",
        )
    transformed, replacements = _RESIZE_TOKEN_RE.subn(BOOTSTRAP_TOKEN, cmdline)
    if replacements != 1:
        _refuse(
            BootstrapImageRefusalCode.CMDLINE_INVALID,
            "cmdline replacement count disagrees with token validation",
        )
    return transformed


def verify_pinned_source(path: Path, source: PinnedSource = PINNED_SOURCE) -> ArtifactDigest:
    """Verify exact official archive size and SHA-256 before planning a build."""

    _require_existing_regular_file(path, "source archive")
    if path.name != source.filename:
        _refuse(
            BootstrapImageRefusalCode.SOURCE_INVALID,
            f"source archive must be named {source.filename}",
        )
    actual_size = path.stat().st_size
    if actual_size != source.compressed_size_bytes:
        _refuse(
            BootstrapImageRefusalCode.SOURCE_SIZE_MISMATCH,
            f"source archive size {actual_size} != {source.compressed_size_bytes}",
        )
    actual_hash = _sha256_file(path)
    if actual_hash != source.compressed_sha256:
        _refuse(
            BootstrapImageRefusalCode.SOURCE_HASH_MISMATCH,
            "source archive SHA-256 does not match the pinned official archive",
        )
    return ArtifactDigest(str(path), actual_size, actual_hash)


def validate_build_paths(paths: BuildPaths, *, require_source: bool = True) -> None:
    """Refuse block/device/symlink paths and constrain temporary build ownership."""

    candidates = (
        paths.source_archive,
        paths.work_root,
        paths.raw_image,
        paths.compressed_image,
        paths.imager_manifest,
    )
    for candidate in candidates:
        _refuse_device_path(candidate)
        _refuse_symlink_ancestors(candidate)
    if any(not candidate.is_absolute() for candidate in candidates):
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "all build paths must be absolute")
    if require_source:
        _require_existing_regular_file(paths.source_archive, "source archive")
    work_root = _absolute_without_resolution(paths.work_root)
    raw_image = _absolute_without_resolution(paths.raw_image)
    compressed_image = _absolute_without_resolution(paths.compressed_image)
    manifest = _absolute_without_resolution(paths.imager_manifest)
    if raw_image.parent != work_root:
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "temporary raw image must be an immediate child of the tool work root",
        )
    if _is_relative_to(compressed_image, work_root) or _is_relative_to(manifest, work_root):
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "retained compressed image and manifest must be outside the temporary work root",
        )
    if paths.raw_image.suffix != ".img" or not paths.compressed_image.name.endswith(".img.xz"):
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "raw output must end in .img and retained output must end in .img.xz",
        )
    if paths.imager_manifest.suffix != ".rpi-imager-manifest":
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "manifest must end in .rpi-imager-manifest",
        )
    normalized = {
        _absolute_without_resolution(paths.source_archive),
        work_root,
        raw_image,
        compressed_image,
        manifest,
    }
    if len(normalized) != len(candidates):
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "build paths must be distinct")
    for output in (paths.work_root, paths.raw_image, paths.compressed_image, paths.imager_manifest):
        if output.exists() or output.is_symlink():
            _refuse(
                BootstrapImageRefusalCode.OUTPUT_EXISTS,
                f"build output already exists: {output}",
            )


def author_build_plan(
    paths: BuildPaths,
    metadata: SourceMetadata,
    source_verification: ArtifactDigest,
) -> BuildPlan:
    """Create the deterministic Bootstrap v1 build and readback plan."""

    validate_build_paths(paths)
    metadata.validate()
    if (
        source_verification.path != str(paths.source_archive)
        or source_verification.size_bytes != PINNED_SOURCE.compressed_size_bytes
        or source_verification.sha256 != PINNED_SOURCE.compressed_sha256
    ):
        _refuse(
            BootstrapImageRefusalCode.SOURCE_INVALID,
            "build plan requires verification of the exact pinned source path",
        )
    work = str(paths.work_root)
    raw = str(paths.raw_image)
    compressed = str(paths.compressed_image)
    manifest = str(paths.imager_manifest)
    stage = "deploy/bootstrap/image/pi-gen-stage"
    actions = (
        BuildAction(
            1,
            BuildActionKind.VERIFY_PINNED_SOURCE,
            (str(paths.source_archive),),
            (),
            False,
            "Verify the exact official Lite armhf archive before any work output exists.",
        ),
        BuildAction(
            2,
            BuildActionKind.PREPARE_PI_GEN_STAGE,
            (
                stage,
                "deploy/bootstrap/storage",
                "deploy/bootstrap/network",
                "uv.lock",
            ),
            (work,),
            False,
            "Materialize the custom-stage assets and build metadata in a new tool-owned root.",
        ),
        BuildAction(
            3,
            BuildActionKind.BUILD_TEMPORARY_RAW_IMAGE,
            (str(paths.source_archive), stage),
            (raw,),
            False,
            "Use only a new regular .img work file; never accept a block-device target.",
        ),
        BuildAction(
            4,
            BuildActionKind.VERIFY_EXTRACTED_SOURCE,
            (raw,),
            (),
            False,
            "Prove official extracted byte size and SHA-256 before mutation.",
        ),
        BuildAction(
            5,
            BuildActionKind.GROW_ROOT_OFFLINE,
            (raw,),
            (raw,),
            False,
            "Grow p2/ext4 offline from official geometry to exact 4 GiB.",
        ),
        BuildAction(
            6,
            BuildActionKind.AUTHOR_ZERO_PREFIX,
            (raw,),
            (raw,),
            False,
            "Extend through future p3 start plus 4 MiB and read back the all-zero extent.",
        ),
        BuildAction(
            7,
            BuildActionKind.CUSTOMIZE_CMDLINE,
            (f"{raw}:fat:/cmdline.txt",),
            (f"{raw}:fat:/cmdline.txt",),
            False,
            "Replace exactly one standalone resize token and preserve every other token.",
        ),
        BuildAction(
            8,
            BuildActionKind.INSTALL_ROOTFS_PAYLOAD,
            (
                "repository source",
                "uv.lock",
                "deploy/bootstrap/storage",
                "deploy/bootstrap/network",
            ),
            (f"{raw}:ext4:{APP_INSTALL_ROOT}",),
            False,
            "Install app, locked environment, metadata, dependencies, and post-root services.",
        ),
        BuildAction(
            9,
            BuildActionKind.VERIFY_RAW_READBACK,
            (raw,),
            (),
            False,
            "Independently re-read FAT/ext4 and satisfy every closed readback requirement.",
        ),
        BuildAction(
            10,
            BuildActionKind.COMPRESS_RAW_IMAGE,
            (raw,),
            (compressed,),
            False,
            "Create deterministic xz output without replacing or retaining the raw image.",
        ),
        BuildAction(
            11,
            BuildActionKind.VERIFY_COMPRESSED_ARTIFACT,
            (compressed,),
            (),
            False,
            "Re-hash compressed output and verify decompressed size/hash against raw readback.",
        ),
        BuildAction(
            12,
            BuildActionKind.WRITE_IMAGER_MANIFEST,
            (compressed,),
            (manifest,),
            False,
            "Record compressed download and extracted raw sizes and SHA-256 values.",
        ),
        BuildAction(
            13,
            BuildActionKind.CLEAN_TOOL_WORK_FILES,
            (work, raw, compressed),
            (),
            False,
            "Only after compressed verification, remove registered tool-created work files.",
        ),
    )
    return BuildPlan(
        schema_version=1,
        generation="DashCam Bootstrap v1",
        source=PINNED_SOURCE,
        paths=paths,
        metadata=metadata,
        package_inventory=PACKAGE_INVENTORY,
        readback_requirements=READBACK_REQUIREMENTS,
        actions=actions,
        raw_is_temporary=True,
        block_device_targets_permitted=False,
        modifies_initramfs=False,
        minimum_projected_root_free_bytes=MINIMUM_PROJECTED_ROOT_FREE_BYTES,
    )


def project_root_free_bytes(
    *,
    current_filesystem_bytes: int,
    current_free_bytes: int,
    target_filesystem_bytes: int = ROOT_TARGET_BYTES,
) -> int:
    """Project free space after online ext4 growth without assuming sparse payload use."""

    if (
        current_filesystem_bytes <= 0
        or current_free_bytes < 0
        or current_free_bytes > current_filesystem_bytes
        or target_filesystem_bytes < current_filesystem_bytes
    ):
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            "filesystem sizes are inconsistent",
        )
    return current_free_bytes + target_filesystem_bytes - current_filesystem_bytes


def assert_root_free_projection(
    *,
    current_filesystem_bytes: int,
    current_free_bytes: int,
) -> int:
    """Require the specification's >=2 GiB free-space gate at the 6 GiB target."""

    projected = project_root_free_bytes(
        current_filesystem_bytes=current_filesystem_bytes,
        current_free_bytes=current_free_bytes,
    )
    if projected < MINIMUM_PROJECTED_ROOT_FREE_BYTES:
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            f"projected root free space {projected} is below "
            f"{MINIMUM_PROJECTED_ROOT_FREE_BYTES}",
        )
    return projected


def make_imager_manifest(
    *,
    proof: CompressionProof,
    evidence: VerificationEvidence,
    artifact_url: str,
    release_date: str,
    metadata: SourceMetadata,
    name: str = "DashCam Bootstrap v1",
) -> bytes:
    """Generate a deterministic Raspberry Pi Imager os-list manifest."""

    metadata.validate()
    validate_verification_evidence(evidence)
    compressed = proof.compressed
    raw = proof.extracted
    _validate_artifact_digest(compressed, expected_suffix=".img.xz")
    _validate_artifact_digest(raw, expected_suffix=".img")
    if (
        evidence.raw.size_bytes != raw.size_bytes
        or evidence.raw.sha256 != raw.sha256
        or evidence.app_commit != metadata.app_commit
        or evidence.package_lock_sha256 != metadata.package_lock_sha256
        or evidence.app_wheel_sha256 != metadata.app_wheel_sha256
    ):
        _refuse(
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
            "manifest identities do not match the passing independent evidence",
        )
    if not artifact_url or not (
        artifact_url.startswith("https://") or artifact_url.startswith("file:///")
    ):
        _refuse(
            BootstrapImageRefusalCode.METADATA_INVALID,
            "artifact URL must use https:// or an explicit file:/// URL",
        )
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date) is None:
        _refuse(
            BootstrapImageRefusalCode.METADATA_INVALID,
            "release date must use YYYY-MM-DD",
        )
    payload = {
        "dashcam_build": {
            "generation": "v1",
            "source_archive_sha256": metadata.source_archive_sha256,
            "source_archive_url": metadata.source_archive_url,
            "source_raw_sha256": metadata.source_raw_sha256,
            "build_root_size_bytes": metadata.build_root_size_bytes,
            "built_raw_size_bytes": metadata.built_raw_size_bytes,
            "app_commit": metadata.app_commit,
            "package_lock_sha256": metadata.package_lock_sha256,
            "package_inventory_sha256": metadata.package_inventory_sha256,
            "verification_evidence_sha256": hashlib.sha256(
                evidence.canonical_bytes()
            ).hexdigest(),
            "readback_contract_sha256": hashlib.sha256(
                _canonical_json([asdict(item) for item in READBACK_REQUIREMENTS])
            ).hexdigest(),
        },
        "imager": {
            "devices": [
                {
                    "capabilities": [],
                    "default": True,
                    "description": "Raspberry Pi Zero 2 W",
                    "matching_type": "inclusive",
                    "name": "Raspberry Pi Zero 2 W",
                    "tags": ["pi3-32bit"],
                }
            ]
        },
        "os_list": [
            {
                "capabilities": [],
                "description": (
                    "Raspberry Pi OS Lite 32-bit Trixie with DashCam Bootstrap v1 "
                    "post-root provisioning"
                ),
                "devices": ["pi3-32bit"],
                "extract_sha256": raw.sha256,
                "extract_size": raw.size_bytes,
                "image_download_sha256": compressed.sha256,
                "image_download_size": compressed.size_bytes,
                "init_format": PINNED_SOURCE.init_format,
                "name": name,
                "release_date": release_date,
                "url": artifact_url,
            }
        ],
    }
    return _canonical_json(payload)


def cleanup_owned_work_files(
    *,
    work_root: Path,
    owned_files: Iterable[Path],
    compressed_verified: bool,
    unlink: Callable[[Path], None] | None = None,
) -> tuple[Path, ...]:
    """Remove only registered regular work files after compressed verification.

    The caller must register paths at creation time.  This function never
    discovers files recursively and never removes directories, symlinks,
    devices, or anything outside the exact work root.
    """

    if not compressed_verified:
        _refuse(
            BootstrapImageRefusalCode.CLEANUP_NOT_VERIFIED,
            "work cleanup requires a verified compressed artifact",
        )
    if not work_root.is_absolute():
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "work root must be absolute")
    _refuse_device_path(work_root)
    _refuse_symlink_ancestors(work_root)
    root = _absolute_without_resolution(work_root)
    unique = tuple(dict.fromkeys(owned_files))
    remover = unlink if unlink is not None else Path.unlink
    removed: list[Path] = []
    for path in unique:
        if not path.is_absolute() or not _is_relative_to(_absolute_without_resolution(path), root):
            _refuse(
                BootstrapImageRefusalCode.CLEANUP_NOT_OWNED,
                f"cleanup path is outside the work root: {path}",
            )
        _refuse_device_path(path)
        _refuse_symlink_ancestors(path)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            _refuse(
                BootstrapImageRefusalCode.CLEANUP_NOT_OWNED,
                f"cleanup path is not a regular tool-created file: {path}",
            )
        remover(path)
        removed.append(path)
    return tuple(removed)


def validate_readback_result(
    result: Mapping[str, bool],
    requirements: Sequence[ReadbackRequirement] = READBACK_REQUIREMENTS,
) -> None:
    """Require one explicit successful result for every readback requirement."""

    expected = {requirement.requirement_id for requirement in requirements}
    actual = set(result)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            f"readback result keys are incomplete: missing={missing}, unexpected={unexpected}",
        )
    failed = sorted(key for key, passed in result.items() if passed is not True)
    if failed:
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            f"readback requirements failed: {failed}",
        )


def validate_new_manifest_output(path: Path) -> None:
    """Require a new absolute regular-file destination for manifest creation."""

    _refuse_device_path(path)
    _refuse_symlink_ancestors(path)
    if not path.is_absolute() or path.suffix != ".rpi-imager-manifest":
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "manifest output must be an absolute .rpi-imager-manifest path",
        )
    if path.exists() or path.is_symlink():
        _refuse(
            BootstrapImageRefusalCode.OUTPUT_EXISTS,
            f"manifest output already exists: {path}",
        )
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            f"manifest parent must be an existing directory: {parent}",
        )


def validate_new_retained_output(path: Path, *, suffix: str) -> None:
    """Public early gate for compressed/evidence/manifest retained outputs."""

    _validate_new_regular_output(path, suffix)


def package_inventory_bytes() -> bytes:
    """Return canonical bytes installed into and read back from the image."""

    return PACKAGE_INVENTORY.canonical_bytes()


@dataclass(frozen=True, slots=True)
class PartitionEntry:
    """Relevant DOS partition-table fields read directly from sector zero."""

    number: int
    status: int
    partition_type: int
    start_sector: int
    size_sectors: int

    @property
    def end_sector(self) -> int:
        return self.start_sector + self.size_sectors - 1


@dataclass(frozen=True, slots=True)
class MbrGeometry:
    disk_id: str
    partitions: tuple[PartitionEntry, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Injectable argv-only process runner."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class ToolPin:
    name: str
    executable: str
    version_argv: tuple[str, ...]
    version_output_sha256: str


@dataclass(frozen=True, slots=True)
class BuilderRequirements:
    """Immutable external builder and apt-snapshot identities."""

    schema_version: int
    builder_container_digest: str
    debian_snapshot: str
    raspberrypi_snapshot: str
    binfmt_marker: str
    tools: tuple[ToolPin, ...]

    def tool(self, name: str) -> ToolPin:
        matches = [tool for tool in self.tools if tool.name == name]
        if len(matches) != 1:
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
                f"builder requirements contain {len(matches)} {name!r} tools",
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class CompressionProof:
    compressed: ArtifactDigest
    extracted: ArtifactDigest


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    check_id: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Closed independent readback result required before manifest creation."""

    schema_version: int
    verifier: str
    passed: bool
    raw: ArtifactDigest
    source_archive_sha256: str
    source_raw_sha256: str
    app_commit: str
    package_lock_sha256: str
    app_wheel_sha256: str
    projected_root_free_bytes: int
    checks: tuple[EvidenceCheck, ...]

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "verifier": self.verifier,
                "passed": self.passed,
                "raw": asdict(self.raw),
                "source_archive_sha256": self.source_archive_sha256,
                "source_raw_sha256": self.source_raw_sha256,
                "app_commit": self.app_commit,
                "package_lock_sha256": self.package_lock_sha256,
                "app_wheel_sha256": self.app_wheel_sha256,
                "projected_root_free_bytes": self.projected_root_free_bytes,
                "checks": [asdict(check) for check in self.checks],
            }
        )


def load_builder_requirements(payload: bytes) -> BuilderRequirements:
    """Parse a closed builder identity file and reject unresolved placeholders."""

    if len(payload) > 64 * 1024:
        _refuse(BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED, "builder requirements too large")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapImageRefused(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "builder requirements must be valid UTF-8 JSON",
        ) from exc
    root = _closed_mapping(
        decoded,
        {
            "schema_version",
            "builder_container_digest",
            "debian_snapshot",
            "raspberrypi_snapshot",
            "binfmt_marker",
            "tools",
        },
        "builder requirements",
        BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
    )
    if root["schema_version"] != 2:
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "builder requirements schema_version must be 2",
        )
    tools_raw = root["tools"]
    if not isinstance(tools_raw, list) or not tools_raw:
        _refuse(BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED, "tools must be a nonempty list")
    tools_list = cast(list[object], tools_raw)
    tools: list[ToolPin] = []
    for index, item in enumerate(tools_list):
        tool = _closed_mapping(
            item,
            {"name", "executable", "version_argv", "version_output_sha256"},
            f"tools[{index}]",
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
        )
        argv = tool["version_argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
                f"tools[{index}].version_argv is invalid",
            )
        argv_strings = cast(list[str], argv)
        tools.append(
            ToolPin(
                name=_required_string(tool["name"], f"tools[{index}].name"),
                executable=_required_string(tool["executable"], f"tools[{index}].executable"),
                version_argv=tuple(argv_strings),
                version_output_sha256=_required_sha256(
                    tool["version_output_sha256"],
                    f"tools[{index}].version_output_sha256",
                    BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
                ),
            )
        )
    requirements = BuilderRequirements(
        schema_version=2,
        builder_container_digest=_required_string(
            root["builder_container_digest"], "builder_container_digest"
        ),
        debian_snapshot=_required_string(root["debian_snapshot"], "debian_snapshot"),
        raspberrypi_snapshot=_required_string(
            root["raspberrypi_snapshot"], "raspberrypi_snapshot"
        ),
        binfmt_marker=_required_string(root["binfmt_marker"], "binfmt_marker"),
        tools=tuple(tools),
    )
    serialized = json.dumps(asdict(requirements)).lower()
    if any(marker in serialized for marker in ("required", "unresolved", "placeholder", "todo")):
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "builder requirements contain unresolved placeholders",
        )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", requirements.builder_container_digest) is None:
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "builder container digest must be an exact sha256 identity",
        )
    if not requirements.debian_snapshot.startswith("https://snapshot.debian.org/"):
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "Debian packages must use an immutable snapshot.debian.org URL",
        )
    if not requirements.raspberrypi_snapshot.startswith("https://"):
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            "Raspberry Pi packages must use a pinned HTTPS snapshot",
        )
    for label, url in (
        ("Debian", requirements.debian_snapshot),
        ("Raspberry Pi", requirements.raspberrypi_snapshot),
    ):
        if re.search(r"/20\d{6}T\d{6}Z(?:/|$)", url) is None:
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
                f"{label} snapshot URL lacks an immutable UTC timestamp",
            )
    required_tools = {
        "guestfish",
        "guestmount",
        "guestunmount",
        "qemu-arm",
        "bash",
        "git",
    }
    if {tool.name for tool in requirements.tools} != required_tools:
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_UNRESOLVED,
            f"tool set must be exactly {sorted(required_tools)}",
        )
    return requirements


def verify_builder_host(
    requirements: BuilderRequirements,
    *,
    runner: CommandRunner,
    platform_name: str,
    effective_uid: int,
    actual_container_digest: str,
) -> dict[str, str]:
    """Verify Linux/root/binfmt and exact external-tool version output."""

    if platform_name != "linux" or effective_uid != 0:
        _refuse(
            BootstrapImageRefusalCode.PLATFORM_UNSUPPORTED,
            "builder requires a reviewed rootful Linux host/container",
        )
    if actual_container_digest != requirements.builder_container_digest:
        _refuse(
            BootstrapImageRefusalCode.TOOLCHAIN_MISMATCH,
            "active builder container digest does not match the reviewed requirement",
        )
    marker = Path(requirements.binfmt_marker)
    _require_existing_regular_file(marker, "armhf binfmt marker")
    identities: dict[str, str] = {}
    for tool in requirements.tools:
        executable = Path(tool.executable)
        _require_existing_regular_file(executable, f"{tool.name} executable")
        if not os.access(executable, os.X_OK):
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_MISMATCH,
                f"{tool.name} executable is not executable",
            )
        argv = (str(executable), *tool.version_argv)
        result = runner(argv)
        if result.returncode != 0:
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_MISMATCH,
                f"{tool.name} version probe failed",
            )
        identity = hashlib.sha256((result.stdout + result.stderr).encode()).hexdigest()
        if identity != tool.version_output_sha256:
            _refuse(
                BootstrapImageRefusalCode.TOOLCHAIN_MISMATCH,
                f"{tool.name} version identity changed",
            )
        identities[tool.name] = identity
    return identities


def resolve_clean_app_commit(repository: Path, *, runner: CommandRunner) -> str:
    """Resolve a full Git commit and refuse dirty, untracked, or unborn trees."""

    if not repository.is_absolute() or not (repository / ".git").is_dir():
        _refuse(
            BootstrapImageRefusalCode.REPOSITORY_UNCOMMITTED,
            "repository must be an absolute Git worktree",
        )
    head = runner(("git", "-C", str(repository), "rev-parse", "--verify", "HEAD"))
    commit = head.stdout.strip()
    if head.returncode != 0 or _COMMIT_RE.fullmatch(commit) is None:
        _refuse(
            BootstrapImageRefusalCode.REPOSITORY_UNCOMMITTED,
            "repository has no full committed HEAD",
        )
    probes = (
        ("diff", "--quiet"),
        ("diff", "--cached", "--quiet"),
    )
    for args in probes:
        result = runner(("git", "-C", str(repository), *args))
        if result.returncode != 0:
            _refuse(
                BootstrapImageRefusalCode.REPOSITORY_UNCOMMITTED,
                "repository has tracked changes or no usable Git state",
            )
    untracked = runner(
        ("git", "-C", str(repository), "ls-files", "--others", "--exclude-standard")
    )
    if untracked.returncode != 0 or untracked.stdout.strip():
        _refuse(
            BootstrapImageRefusalCode.REPOSITORY_UNCOMMITTED,
            "repository contains untracked files",
        )
    return commit


def decompress_pinned_source(
    source_archive: Path,
    raw_output: Path,
    *,
    source: PinnedSource = PINNED_SOURCE,
) -> ArtifactDigest:
    """Stream the verified source into a new regular raw file and prove identity."""

    verification = verify_pinned_source(source_archive, source)
    del verification
    _validate_new_regular_output(raw_output, ".img")
    digest = hashlib.sha256()
    size = 0
    created = False
    try:
        with raw_output.open("xb") as destination:
            created = True
            with lzma.open(source_archive, "rb") as compressed:
                for chunk in iter(lambda: compressed.read(HASH_CHUNK_BYTES), b""):
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        actual_hash = digest.hexdigest()
        if size != source.extracted_size_bytes:
            _refuse(
                BootstrapImageRefusalCode.EXTRACTED_SIZE_MISMATCH,
                f"extracted source size {size} != {source.extracted_size_bytes}",
            )
        if actual_hash != source.extracted_sha256:
            _refuse(
                BootstrapImageRefusalCode.EXTRACTED_HASH_MISMATCH,
                "extracted source SHA-256 does not match the official raw image",
            )
        return ArtifactDigest(str(raw_output), size, actual_hash)
    except Exception:
        if created:
            with suppress(FileNotFoundError):
                raw_output.unlink()
        raise


def read_mbr_geometry(raw_image: Path) -> MbrGeometry:
    """Read DOS geometry directly without trusting a mounted/filesystem view."""

    _require_existing_regular_file(raw_image, "raw image")
    with raw_image.open("rb") as stream:
        sector = stream.read(SECTOR_SIZE_BYTES)
    if len(sector) != SECTOR_SIZE_BYTES or sector[510:512] != b"\x55\xaa":
        _refuse(BootstrapImageRefusalCode.GEOMETRY_MISMATCH, "invalid DOS MBR signature")
    disk_id = f"0x{int.from_bytes(sector[440:444], 'little'):08x}"
    partitions: list[PartitionEntry] = []
    for index in range(4):
        entry = sector[446 + 16 * index : 446 + 16 * (index + 1)]
        partitions.append(
            PartitionEntry(
                number=index + 1,
                status=entry[0],
                partition_type=entry[4],
                start_sector=int.from_bytes(entry[8:12], "little"),
                size_sectors=int.from_bytes(entry[12:16], "little"),
            )
        )
    return MbrGeometry(disk_id=disk_id, partitions=tuple(partitions))


def verify_source_geometry(raw_image: Path) -> MbrGeometry:
    geometry = read_mbr_geometry(raw_image)
    expected = (
        (1, 0, 0x0C, SOURCE_BOOT_START_SECTOR, SOURCE_BOOT_SIZE_SECTORS),
        (2, 0, 0x83, SOURCE_ROOT_START_SECTOR, SOURCE_ROOT_SIZE_SECTORS),
        (3, 0, 0, 0, 0),
        (4, 0, 0, 0, 0),
    )
    actual = tuple(
        (item.number, item.status, item.partition_type, item.start_sector, item.size_sectors)
        for item in geometry.partitions
    )
    if geometry.disk_id != SOURCE_MBR_DISK_ID or actual != expected:
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            "official source MBR geometry does not match the closed contract",
        )
    return geometry


def verify_built_geometry(raw_image: Path) -> MbrGeometry:
    geometry = read_mbr_geometry(raw_image)
    expected = (
        (1, 0, 0x0C, SOURCE_BOOT_START_SECTOR, SOURCE_BOOT_SIZE_SECTORS),
        (2, 0, 0x83, SOURCE_ROOT_START_SECTOR, BUILD_ROOT_SIZE_SECTORS),
        (3, 0, 0, 0, 0),
        (4, 0, 0, 0, 0),
    )
    actual = tuple(
        (item.number, item.status, item.partition_type, item.start_sector, item.size_sectors)
        for item in geometry.partitions
    )
    if geometry.disk_id != SOURCE_MBR_DISK_ID or actual != expected:
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            "built MBR must preserve disk ID/p1 and contain exact 4 GiB p2 only",
        )
    if raw_image.stat().st_size != BUILT_RAW_SIZE:
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            f"built raw size must be exactly {BUILT_RAW_SIZE}",
        )
    return geometry


def verify_only_p2_entry_changed(source_mbr: bytes, built_mbr: bytes) -> None:
    """Prove the offline grow preserved every MBR byte outside entry 2."""

    if len(source_mbr) != SECTOR_SIZE_BYTES or len(built_mbr) != SECTOR_SIZE_BYTES:
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            "source and built MBR reads must each be exactly one sector",
        )
    p2_start = 446 + 16
    p2_end = p2_start + 16
    if (
        source_mbr[:p2_start] != built_mbr[:p2_start]
        or source_mbr[p2_end:] != built_mbr[p2_end:]
    ):
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            "offline grow changed MBR bytes outside the authorized p2 entry",
        )


def verify_zero_prefix(raw_image: Path) -> None:
    """Read and prove every byte of the future p3 4 MiB prefix is zero."""

    _require_existing_regular_file(raw_image, "raw image")
    offset = FUTURE_P3_START_SECTOR * SECTOR_SIZE_BYTES
    remaining = ZERO_PREFIX_BYTES
    with raw_image.open("rb") as stream:
        stream.seek(offset)
        while remaining:
            chunk = stream.read(min(HASH_CHUNK_BYTES, remaining))
            if not chunk or any(chunk):
                _refuse(
                    BootstrapImageRefusalCode.ZERO_PREFIX_MISMATCH,
                    "future p3 prefix is short or contains a nonzero byte",
                )
            remaining -= len(chunk)


def verify_ext4_size_offline(
    raw_image: Path,
    *,
    guestfish: Path,
    runner: CommandRunner,
) -> None:
    """Reopen p2 read-only and prove ext4 is exactly the build-time size."""

    _require_existing_regular_file(raw_image, "raw image")
    _require_existing_regular_file(guestfish, "guestfish executable")
    script = "run\nmount-ro /dev/sda2 /\nvfs-type /dev/sda2\nvfs-size /\n"
    result = runner(
        (str(guestfish), "--ro", "--format=raw", "-a", str(raw_image)),
        input_text=script,
    )
    values = result.stdout.splitlines()
    if (
        result.returncode != 0
        or values != ["ext4", str(BUILD_ROOT_SIZE_BYTES)]
    ):
        _refuse(
            BootstrapImageRefusalCode.GEOMETRY_MISMATCH,
            "offline-grown p2 did not reopen as exact 4 GiB ext4",
        )


def grow_root_offline(
    raw_image: Path,
    *,
    guestfish: Path,
    runner: CommandRunner,
) -> ArtifactDigest:
    """Grow p2/ext4 offline to 4 GiB through libguestfs on a regular image."""

    _require_existing_regular_file(raw_image, "raw image")
    _require_existing_regular_file(guestfish, "guestfish executable")
    source_digest = ArtifactDigest.read(raw_image)
    if (
        source_digest.size_bytes != SOURCE_RAW_SIZE
        or source_digest.sha256 != SOURCE_RAW_SHA256
    ):
        _refuse(
            BootstrapImageRefusalCode.EXTRACTED_HASH_MISMATCH,
            "offline grow requires the exact verified official raw image",
        )
    verify_source_geometry(raw_image)
    with raw_image.open("rb") as source_stream:
        source_mbr = source_stream.read(SECTOR_SIZE_BYTES)
    with raw_image.open("r+b") as stream:
        stream.truncate(BUILT_RAW_SIZE)
        stream.flush()
        os.fsync(stream.fileno())
    verify_zero_prefix(raw_image)
    script = "\n".join(
        (
            "run",
            f"part-resize /dev/sda 2 {BUILD_ROOT_END_SECTOR}",
            "e2fsck-f /dev/sda2",
            "resize2fs /dev/sda2",
            "e2fsck-f /dev/sda2",
            "",
        )
    )
    result = runner(
        (str(guestfish), "--rw", "--format=raw", "-a", str(raw_image)),
        input_text=script,
    )
    if result.returncode != 0:
        _refuse(
            BootstrapImageRefusalCode.COMMAND_FAILED,
            f"offline guestfish grow failed: {result.stderr.strip()}",
        )
    with raw_image.open("rb") as built_stream:
        built_mbr = built_stream.read(SECTOR_SIZE_BYTES)
    verify_only_p2_entry_changed(source_mbr, built_mbr)
    verify_built_geometry(raw_image)
    verify_zero_prefix(raw_image)
    verify_ext4_size_offline(raw_image, guestfish=guestfish, runner=runner)
    return ArtifactDigest.read(raw_image)


def compress_verified_raw(raw_image: Path, compressed_output: Path) -> CompressionProof:
    """Deterministically compress a verified raw image and stream-prove extraction."""

    verify_built_geometry(raw_image)
    verify_zero_prefix(raw_image)
    raw = ArtifactDigest.read(raw_image)
    _validate_new_regular_output(compressed_output, ".img.xz")
    created = False
    try:
        with compressed_output.open("xb") as destination:
            created = True
            compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=9)
            with raw_image.open("rb") as source_stream:
                for chunk in iter(lambda: source_stream.read(HASH_CHUNK_BYTES), b""):
                    destination.write(compressor.compress(chunk))
            destination.write(compressor.flush())
            destination.flush()
            os.fsync(destination.fileno())
        compressed = ArtifactDigest.read(compressed_output)
        extracted = verify_compressed_stream(compressed_output, expected=raw)
        return CompressionProof(compressed=compressed, extracted=extracted)
    except Exception:
        if created:
            with suppress(FileNotFoundError):
                compressed_output.unlink()
        raise


def verify_compressed_stream(
    compressed_image: Path,
    *,
    expected: ArtifactDigest,
) -> ArtifactDigest:
    """Hash the complete decompressed stream and compare it to verified raw identity."""

    _require_existing_regular_file(compressed_image, "compressed image")
    digest = hashlib.sha256()
    size = 0
    with lzma.open(compressed_image, "rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
            size += len(chunk)
    actual = ArtifactDigest(path=expected.path, size_bytes=size, sha256=digest.hexdigest())
    if actual.size_bytes != expected.size_bytes or actual.sha256 != expected.sha256:
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            "compressed artifact does not extract to the independently verified raw identity",
        )
    return actual


def load_verification_evidence(payload: bytes) -> VerificationEvidence:
    """Parse closed evidence produced by the independent verifier process."""

    if len(payload) > 256 * 1024:
        _refuse(BootstrapImageRefusalCode.EVIDENCE_INVALID, "verification evidence is too large")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapImageRefused(
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
            "verification evidence must be valid UTF-8 JSON",
        ) from exc
    root = _closed_mapping(
        decoded,
        {
            "schema_version",
            "verifier",
            "passed",
            "raw",
            "source_archive_sha256",
            "source_raw_sha256",
            "app_commit",
            "package_lock_sha256",
            "app_wheel_sha256",
            "projected_root_free_bytes",
            "checks",
        },
        "verification evidence",
    )
    raw_map = _closed_mapping(root["raw"], {"path", "size_bytes", "sha256"}, "raw")
    checks_raw = root["checks"]
    if not isinstance(checks_raw, list):
        _refuse(BootstrapImageRefusalCode.EVIDENCE_INVALID, "checks must be a list")
    checks_list = cast(list[object], checks_raw)
    checks: list[EvidenceCheck] = []
    for index, item in enumerate(checks_list):
        check = _closed_mapping(item, {"check_id", "passed", "detail"}, f"checks[{index}]")
        passed = check["passed"]
        if not isinstance(passed, bool):
            _refuse(
                BootstrapImageRefusalCode.EVIDENCE_INVALID,
                f"checks[{index}].passed must be boolean",
            )
        checks.append(
            EvidenceCheck(
                check_id=_required_string(check["check_id"], f"checks[{index}].check_id"),
                passed=passed,
                detail=_required_string(check["detail"], f"checks[{index}].detail"),
            )
        )
    evidence = VerificationEvidence(
        schema_version=root["schema_version"] if isinstance(root["schema_version"], int) else -1,
        verifier=_required_string(root["verifier"], "verifier"),
        passed=root["passed"] if isinstance(root["passed"], bool) else False,
        raw=ArtifactDigest(
            path=_required_string(raw_map["path"], "raw.path"),
            size_bytes=raw_map["size_bytes"] if isinstance(raw_map["size_bytes"], int) else -1,
            sha256=_required_sha256(
                raw_map["sha256"], "raw.sha256", BootstrapImageRefusalCode.EVIDENCE_INVALID
            ),
        ),
        source_archive_sha256=_required_sha256(
            root["source_archive_sha256"],
            "source_archive_sha256",
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
        ),
        source_raw_sha256=_required_sha256(
            root["source_raw_sha256"],
            "source_raw_sha256",
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
        ),
        app_commit=_required_string(root["app_commit"], "app_commit"),
        package_lock_sha256=_required_sha256(
            root["package_lock_sha256"],
            "package_lock_sha256",
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
        ),
        app_wheel_sha256=_required_sha256(
            root["app_wheel_sha256"],
            "app_wheel_sha256",
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
        ),
        projected_root_free_bytes=(
            root["projected_root_free_bytes"]
            if isinstance(root["projected_root_free_bytes"], int)
            else -1
        ),
        checks=tuple(checks),
    )
    validate_verification_evidence(evidence)
    return evidence


def verify_mounted_readback(
    *,
    raw_image: Path,
    source_boot: Path,
    built_boot: Path,
    built_root: Path,
    metadata: SourceMetadata,
    builder_requirements: BuilderRequirements,
    filesystem_size_bytes: int | None = None,
    filesystem_free_bytes: int | None = None,
) -> VerificationEvidence:
    """Independently inspect read-only mounted FAT/ext4 trees and raw geometry."""

    metadata.validate()
    checks: list[EvidenceCheck] = []

    def check(check_id: str, operation: Callable[[], str]) -> None:
        try:
            detail = operation()
        except (BootstrapImageRefused, OSError, ValueError, UnicodeError) as exc:
            checks.append(EvidenceCheck(check_id, False, str(exc)))
        else:
            checks.append(EvidenceCheck(check_id, True, detail))

    source_cmdline = source_boot / "cmdline.txt"
    built_cmdline = built_boot / "cmdline.txt"

    def cmdline_bootstrap() -> str:
        actual = built_cmdline.read_text(encoding="utf-8")
        tokens = actual.split()
        if tokens.count(BOOTSTRAP_TOKEN) != 1 or "resize" in tokens:
            raise ValueError("built cmdline Bootstrap/resize token count is invalid")
        return "one Bootstrap token; no standalone resize"

    def cmdline_preserved() -> str:
        source_text = source_cmdline.read_text(encoding="utf-8")
        built_text = built_cmdline.read_text(encoding="utf-8")
        if transform_cmdline(source_text) != built_text:
            raise ValueError("built cmdline differs beyond the single permitted token transform")
        return "source cmdline exact transform matches"

    source_boot_inventory = _tree_file_inventory(source_boot, exclude={"cmdline.txt"})
    built_boot_inventory = _tree_file_inventory(built_boot, exclude={"cmdline.txt"})

    def seed_preserved() -> str:
        if source_boot_inventory != built_boot_inventory:
            raise ValueError("non-cmdline FAT file inventory differs from official source")
        for seed in ("meta-data", "network-config", "user-data"):
            if seed not in built_boot_inventory:
                raise ValueError(f"cloudinit-rpi seed file missing: {seed}")
        return f"{len(built_boot_inventory)} non-cmdline FAT files match"

    def initramfs_preserved() -> str:
        source_items = {
            key: value
            for key, value in source_boot_inventory.items()
            if "initramfs" in key.lower() or Path(key).name.startswith("initrd")
        }
        built_items = {
            key: value
            for key, value in built_boot_inventory.items()
            if "initramfs" in key.lower() or Path(key).name.startswith("initrd")
        }
        if not source_items or source_items != built_items:
            raise ValueError("initramfs/initrd inventory is absent or changed")
        return f"{len(source_items)} initramfs/initrd files unchanged"

    def no_initramfs_hooks() -> str:
        roots = (
            built_root / "etc/initramfs-tools",
            built_root / "usr/share/initramfs-tools",
        )
        forbidden = (b"dashcam", b"dashcam-bounded-provision", b"firstboot-initramfs")
        inspected = 0
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"symbolic initramfs path refused: {path}")
                if not path.is_file():
                    continue
                inspected += 1
                data = path.read_bytes()
                if any(marker in data.lower() for marker in forbidden):
                    raise ValueError(f"DashCam initramfs content found: {path}")
        return f"{inspected} initramfs-tool files contain no DashCam hook"

    source_metadata_path = built_root / APP_INSTALL_ROOT.lstrip("/") / "build-metadata/source.json"
    lock_path = built_root / APP_INSTALL_ROOT.lstrip("/") / "build-metadata/uv.lock"
    wheel_identity_path = (
        built_root / APP_INSTALL_ROOT.lstrip("/") / "build-metadata/app-wheel.sha256"
    )

    def app_check() -> str:
        app_root = built_root / APP_INSTALL_ROOT.lstrip("/") / "app"
        if not (app_root / "src/dashcam").is_dir():
            raise ValueError("application source payload is missing")
        distributions = tuple(
            (built_root / APP_VENV.lstrip("/") / "lib").glob(
                "python*/site-packages/dashcam_pizero2w-*.dist-info"
            )
        )
        if len(distributions) != 1:
            raise ValueError("installed DashCam wheel identity is missing or ambiguous")
        return f"app source and one installed distribution found for {metadata.app_commit}"

    def environment_check() -> str:
        python = built_root / APP_VENV.lstrip("/") / "bin/python"
        smoke = (
            built_root / APP_INSTALL_ROOT.lstrip("/") / "build-metadata/import-smoke.txt"
        )
        if not python.exists() or not smoke.is_file():
            raise ValueError("target venv or import-smoke record missing")
        if "/opt/dashcam/venv/" not in smoke.read_text(encoding="utf-8"):
            raise ValueError("target import smoke did not import from the target venv")
        wheel_line = wheel_identity_path.read_text(encoding="utf-8").strip()
        if not wheel_line.startswith(metadata.app_wheel_sha256 + "  wheelhouse/"):
            raise ValueError("application wheel hash record does not match metadata")
        return "target venv, wheel hash, and import smoke match"

    def package_inventory_check() -> str:
        actual = (
            built_root
            / APP_INSTALL_ROOT.lstrip("/")
            / "build-metadata/package-inventory.json"
        ).read_bytes()
        if actual != package_inventory_bytes():
            raise ValueError("installed package inventory contract differs")
        return f"package inventory {PACKAGE_INVENTORY.sha256} matches"

    def source_metadata_check() -> str:
        if source_metadata_path.read_bytes() != metadata.canonical_bytes():
            raise ValueError("installed source metadata differs")
        if _sha256_file(lock_path) != metadata.package_lock_sha256:
            raise ValueError("installed uv.lock hash differs")
        installed_requirements = load_builder_requirements(
            (
                built_root
                / APP_INSTALL_ROOT.lstrip("/")
                / "build-metadata/build-requirements.json"
            ).read_bytes()
        )
        if installed_requirements != builder_requirements:
            raise ValueError("installed builder tool/snapshot requirements differ")
        return "source/app/lock/wheel/tool/snapshot metadata matches"

    def owner_payload_check(name: str) -> str:
        payload = built_root / APP_INSTALL_ROOT.lstrip("/") / "bootstrap" / name
        if not (payload / "install.sh").is_file():
            raise ValueError(f"{name} payload installer is missing")
        return f"{name} payload present"

    def service_check() -> str:
        systemd = built_root / "etc/systemd/system"
        required = (
            "dashcam-bootstrap-stage-a.service",
            "dashcam-bootstrap-stage-b.service",
            "dashcam-network-fallback.service",
            "dashcam-storage-check.service",
        )
        wants = systemd / "multi-user.target.wants"
        for name in required:
            unit = systemd / name
            link = wants / name
            if not unit.is_file() or not link.is_symlink() or os.readlink(link) != f"../{name}":
                raise ValueError(f"service missing or not enabled exactly: {name}")
            text = unit.read_text(encoding="utf-8")
            if name != "dashcam-storage-check.service" and "cloud-final.service" not in text:
                raise ValueError(f"service lacks cloud-final ordering: {name}")
        dashcamd_link = wants / "dashcamd.service"
        if dashcamd_link.exists() or dashcamd_link.is_symlink():
            raise ValueError("write-capable recorder service enabled prematurely")
        storage_check = systemd / "dashcam-storage-check.service"
        guard = (
            "ConditionPathExists=/var/lib/dashcam/provisioning/"
            "layout-v1.complete.json"
        )
        if guard not in storage_check.read_text(encoding="utf-8"):
            raise ValueError("enabled storage preflight lacks Stage B completion guard")
        group_lines = (
            built_root / "etc/group"
        ).read_text(encoding="utf-8").splitlines()
        storage_groups = [
            line.split(":")
            for line in group_lines
            if line.split(":", 1)[0] == "dashcam-storage"
        ]
        if (
            len(storage_groups) != 1
            or len(storage_groups[0]) != 4
            or not storage_groups[0][2].isdigit()
            or "dashcam" not in storage_groups[0][3].split(",")
        ):
            raise ValueError("dashcam-storage group identity/membership is not exact")
        passwd_lines = (
            built_root / "etc/passwd"
        ).read_text(encoding="utf-8").splitlines()
        dashcam_users = [
            line.split(":")
            for line in passwd_lines
            if line.split(":", 1)[0] == "dashcam"
        ]
        if (
            len(dashcam_users) != 1
            or len(dashcam_users[0]) != 7
            or not dashcam_users[0][2].isdigit()
        ):
            raise ValueError("dashcam service user identity is not exact")
        return (
            "Bootstrap/preflight services and service identities are exact; "
            "recorder remains disabled"
        )

    def cloudinit_check() -> str:
        required = (
            "usr/lib/systemd/system/cloud-final.service",
            "etc/cloud/cloud.cfg",
            "etc/cloud/cloud.cfg.d/99_raspberry-pi.cfg",
            "usr/lib/python3/dist-packages/cloudinit/config/cc_raspberry_pi.py",
            "usr/lib/python3/dist-packages/cloudinit/distros/raspberry_pi_os.py",
        )
        missing = [relative for relative in required if not (built_root / relative).is_file()]
        if missing:
            raise ValueError(f"cloudinit-rpi contracts missing: {missing}")
        return "cloud-final, raspberry_pi module, and Raspberry Pi OS distro support present"

    def no_cloud_root_expander() -> str:
        return audit_cloudinit_no_root_expander(built_root)

    def dependency_check() -> str:
        versions_path = (
            built_root / APP_INSTALL_ROOT.lstrip("/") / "build-metadata/dpkg-versions.tsv"
        )
        installed = {
            line.split("\t", 1)[0].split(":", 1)[0]
            for line in versions_path.read_text(encoding="utf-8").splitlines()
            if "\t" in line
        }
        missing = sorted(set(APT_PACKAGES) - installed)
        if missing:
            raise ValueError(f"declared packages missing: {missing}")
        return f"{len(APT_PACKAGES)} declared packages installed"

    measured_size: int
    measured_free: int
    if filesystem_size_bytes is None or filesystem_free_bytes is None:
        statvfs = getattr(os, "statvfs", None)
        if statvfs is None:
            raise ValueError("statvfs is unavailable on the verifier host")
        fs = statvfs(built_root)
        measured_size = fs.f_blocks * fs.f_frsize
        measured_free = fs.f_bavail * fs.f_frsize
    else:
        measured_size = filesystem_size_bytes
        measured_free = filesystem_free_bytes
    projected = -1

    def root_projection_check() -> str:
        nonlocal projected
        if measured_size != BUILD_ROOT_SIZE_BYTES:
            raise ValueError(f"built ext4 size {measured_size} is not exact 4 GiB")
        projected = assert_root_free_projection(
            current_filesystem_bytes=measured_size,
            current_free_bytes=measured_free,
        )
        return f"projected 6 GiB free bytes: {projected}"

    check("fat.cmdline.bootstrap", cmdline_bootstrap)
    check("fat.cmdline.imager-preserved", cmdline_preserved)
    check("fat.cloudinit-seed-preserved", seed_preserved)
    check("fat.initramfs-unchanged", initramfs_preserved)
    check("ext4.no-initramfs-hooks", no_initramfs_hooks)
    check("ext4.app", app_check)
    check("ext4.environment", environment_check)
    check("ext4.package-inventory", package_inventory_check)
    check("ext4.source-metadata", source_metadata_check)
    check("ext4.storage-payload", lambda: owner_payload_check("storage"))
    check("ext4.network-payload", lambda: owner_payload_check("network"))
    check("ext4.services", service_check)
    check("ext4.cloudinit", cloudinit_check)
    check("ext4.cloudinit-no-root-expander", no_cloud_root_expander)
    check("ext4.dependencies", dependency_check)
    check("ext4.root-free-projection", root_projection_check)
    check("raw.mbr-geometry", lambda: _geometry_detail(raw_image))
    check("raw.future-p3-zero-prefix", lambda: _zero_prefix_detail(raw_image))
    passed = len(checks) == len(READBACK_REQUIREMENTS) and all(item.passed for item in checks)
    return VerificationEvidence(
        schema_version=1,
        verifier="dashcam-bootstrap-v1-independent",
        passed=passed,
        raw=ArtifactDigest.read(raw_image),
        source_archive_sha256=SOURCE_ARCHIVE_SHA256,
        source_raw_sha256=SOURCE_RAW_SHA256,
        app_commit=metadata.app_commit,
        package_lock_sha256=metadata.package_lock_sha256,
        app_wheel_sha256=metadata.app_wheel_sha256,
        projected_root_free_bytes=projected,
        checks=tuple(checks),
    )


def audit_cloudinit_no_root_expander(rootfs: Path) -> str:
    """Prove cloud-init cannot race Bootstrap by growing root while Imager still works."""

    cloud_cfg = rootfs / "etc/cloud"
    if not cloud_cfg.is_dir() or cloud_cfg.is_symlink():
        raise ValueError("cloud configuration directory is missing or unsafe")
    configs = tuple(
        path
        for path in sorted(cloud_cfg.rglob("*.cfg"))
        if path.is_file() and not path.is_symlink()
    )
    text = "\n".join(path.read_text(encoding="utf-8", errors="strict") for path in configs)
    if re.search(r"(?m)^\s*-\s*(?:growpart|resizefs)(?:\s*#.*)?$", text.lower()):
        raise ValueError("cloud-init growpart/resizefs module unexpectedly enabled")
    module = (
        rootfs / "usr/lib/python3/dist-packages/cloudinit/config/cc_raspberry_pi.py"
    )
    if not module.is_file() or module.is_symlink():
        raise ValueError("cloud-init raspberry_pi module missing or unsafe")
    pi_cfg = cloud_cfg / "cloud.cfg.d/99_raspberry-pi.cfg"
    if not pi_cfg.is_file() or pi_cfg.is_symlink():
        raise ValueError("Raspberry Pi NoCloud configuration missing or unsafe")
    if "file:///boot/firmware" not in pi_cfg.read_text(encoding="utf-8"):
        raise ValueError("NoCloud Imager seedfrom contract missing")
    return "growpart/resizefs absent; raspberry_pi and NoCloud seedfrom retained"


def validate_verification_evidence(evidence: VerificationEvidence) -> None:
    """Require the exact closed check set and all cross-artifact identities."""

    if evidence.schema_version != 1 or evidence.verifier != "dashcam-bootstrap-v1-independent":
        _refuse(BootstrapImageRefusalCode.EVIDENCE_INVALID, "unknown verifier/schema identity")
    expected = {item.requirement_id for item in READBACK_REQUIREMENTS}
    actual = {item.check_id for item in evidence.checks}
    if len(actual) != len(evidence.checks) or actual != expected:
        _refuse(
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
            "verification evidence check set is incomplete or duplicated",
        )
    if (
        not evidence.passed
        or any(not check.passed for check in evidence.checks)
        or evidence.source_archive_sha256 != SOURCE_ARCHIVE_SHA256
        or evidence.source_raw_sha256 != SOURCE_RAW_SHA256
        or _COMMIT_RE.fullmatch(evidence.app_commit) is None
        or evidence.raw.size_bytes != BUILT_RAW_SIZE
        or _SHA256_RE.fullmatch(evidence.raw.sha256) is None
        or evidence.projected_root_free_bytes < MINIMUM_PROJECTED_ROOT_FREE_BYTES
    ):
        _refuse(
            BootstrapImageRefusalCode.EVIDENCE_INVALID,
            "verification evidence contains a failed check or mismatched identity",
        )


def _geometry_detail(raw_image: Path) -> str:
    geometry = verify_built_geometry(raw_image)
    return (
        f"disk_id={geometry.disk_id}; p1 preserved; "
        f"p2={geometry.partitions[1].size_sectors} sectors"
    )


def _zero_prefix_detail(raw_image: Path) -> str:
    verify_zero_prefix(raw_image)
    return f"{ZERO_PREFIX_BYTES} zero bytes at sector {FUTURE_P3_START_SECTOR}"


def _tree_file_inventory(root: Path, *, exclude: set[str]) -> dict[str, tuple[int, str]]:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError(f"inventory root is unsafe: {root}")
    inventory: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic FAT path refused: {relative}")
        if path.is_file():
            inventory[relative] = (path.stat().st_size, _sha256_file(path))
    return inventory


def default_command_runner(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one time/output/input-bounded argv-only command without a shell."""

    payload = None if input_text is None else input_text.encode()
    if payload is not None and len(payload) > MAX_COMMAND_INPUT_BYTES:
        return CommandResult(125, "", "command input exceeded the closed size limit")
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            tuple(argv),
            shell=False,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=None if env is None else dict(env),
        )
        timed_out = False
        try:
            process.communicate(input=payload, timeout=COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_COMMAND_OUTPUT_BYTES + 1)
        stderr = stderr_file.read(MAX_COMMAND_OUTPUT_BYTES + 1)
    if len(stdout) > MAX_COMMAND_OUTPUT_BYTES or len(stderr) > MAX_COMMAND_OUTPUT_BYTES:
        return CommandResult(125, "", "command output exceeded the closed size limit")
    return CommandResult(
        124 if timed_out else process.returncode,
        stdout.decode(errors="replace"),
        (
            "command timed out"
            if timed_out
            else stderr.decode(errors="replace")
        ),
    )


def customize_regular_image(
    *,
    raw_image: Path,
    stage: Path,
    work_root: Path,
    requirements: BuilderRequirements,
    runner: CommandRunner,
) -> None:
    """Mount only a regular image through FUSE and run the checked rootfs stage."""

    verify_built_geometry(raw_image)
    verify_zero_prefix(raw_image)
    if not stage.is_absolute() or not stage.is_dir() or stage.is_symlink():
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "prepared stage directory is unsafe")
    if not work_root.is_absolute() or not work_root.is_dir() or work_root.is_symlink():
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "work root is unsafe")
    root_mount = work_root / "rootfs"
    boot_mount = root_mount / "boot/firmware"
    if root_mount.exists() or root_mount.is_symlink():
        _refuse(
            BootstrapImageRefusalCode.OUTPUT_EXISTS,
            f"mountpoint already exists: {root_mount}",
        )
    root_mount.mkdir()
    guestmount = requirements.tool("guestmount").executable
    guestunmount = requirements.tool("guestunmount").executable
    bash = requirements.tool("bash").executable
    mounted = False
    primary_error: BootstrapImageRefused | None = None
    try:
        # One libguestfs appliance owns the entire writable image. Two
        # concurrent --rw appliances, even for different partitions, could
        # independently cache and corrupt the same raw backing file.
        result = runner(
            (
                guestmount,
                "--rw",
                "--format=raw",
                "-a",
                str(raw_image),
                "-m",
                "/dev/sda2:/",
                "-m",
                "/dev/sda1:/boot/firmware",
                str(root_mount),
            )
        )
        if result.returncode != 0:
            _refuse(
                BootstrapImageRefusalCode.COMMAND_FAILED,
                f"guestmount failed for the combined root/boot view: "
                f"{result.stderr.strip()}",
            )
        mounted = True
        if not boot_mount.is_dir() or boot_mount.is_symlink():
            _refuse(
                BootstrapImageRefusalCode.COMMAND_FAILED,
                "combined image mount did not expose the boot filesystem",
            )
        _configure_apt_snapshots(root_mount, requirements)
        environment = dict(os.environ)
        environment.update(
            {
                "ROOTFS_DIR": str(root_mount),
                "BOOTFS_DIR": str(boot_mount),
                "DASHCAM_DEBIAN_SNAPSHOT": requirements.debian_snapshot,
                "DASHCAM_RASPBERRYPI_SNAPSHOT": requirements.raspberrypi_snapshot,
            }
        )
        stage_result = runner(
            (bash, str(stage / "01-run.sh")),
            env=environment,
        )
        if stage_result.returncode != 0:
            _refuse(
                BootstrapImageRefusalCode.COMMAND_FAILED,
                f"rootfs customization stage failed: {stage_result.stderr.strip()}",
            )
    except BootstrapImageRefused as exc:
        primary_error = exc
    finally:
        unmount_error: str | None = None
        if mounted:
            result = runner((guestunmount, str(root_mount)))
            if result.returncode != 0:
                unmount_error = result.stderr.strip()
        if unmount_error is not None and primary_error is None:
            primary_error = BootstrapImageRefused(
                BootstrapImageRefusalCode.COMMAND_FAILED,
                f"guestunmount failed: {root_mount}: {unmount_error}",
            )
    if primary_error is not None:
        raise primary_error
    verify_built_geometry(raw_image)
    verify_zero_prefix(raw_image)


def mount_readonly_image(
    *,
    raw_image: Path,
    mounts: Sequence[tuple[str, Path]],
    requirements: BuilderRequirements,
    runner: CommandRunner,
) -> tuple[Path, ...]:
    """Mount declared partitions read-only through guestmount for verification."""

    _require_existing_regular_file(raw_image, "raw image")
    guestmount = requirements.tool("guestmount").executable
    mounted: list[Path] = []
    try:
        for partition, mountpoint in mounts:
            if mountpoint.exists() or mountpoint.is_symlink():
                _refuse(
                    BootstrapImageRefusalCode.OUTPUT_EXISTS,
                    f"verification mountpoint exists: {mountpoint}",
                )
            mountpoint.mkdir()
            result = runner(
                (
                    guestmount,
                    "--ro",
                    "--format=raw",
                    "-a",
                    str(raw_image),
                    "-m",
                    partition,
                    str(mountpoint),
                )
            )
            if result.returncode != 0:
                _refuse(
                    BootstrapImageRefusalCode.COMMAND_FAILED,
                    f"read-only guestmount failed for {partition}: {result.stderr.strip()}",
                )
            mounted.append(mountpoint)
    except Exception:
        unmount_readonly(mounted=mounted, requirements=requirements, runner=runner)
        raise
    return tuple(mounted)


def unmount_readonly(
    *,
    mounted: Sequence[Path],
    requirements: BuilderRequirements,
    runner: CommandRunner,
) -> None:
    """Unmount every verifier mount in reverse order and require success."""

    guestunmount = requirements.tool("guestunmount").executable
    failures: list[str] = []
    for mountpoint in reversed(mounted):
        result = runner((guestunmount, str(mountpoint)))
        if result.returncode != 0:
            failures.append(f"{mountpoint}: {result.stderr.strip()}")
    if failures:
        _refuse(
            BootstrapImageRefusalCode.COMMAND_FAILED,
            f"read-only guestunmount failed: {failures}",
        )


def write_new_file(path: Path, payload: bytes, *, suffix: str) -> ArtifactDigest:
    """Write and fsync one new retained regular file."""

    _validate_new_regular_output(path, suffix)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return ArtifactDigest.read(path)


def remove_empty_work_directories(paths: Sequence[Path]) -> None:
    """Remove only explicitly named empty tool-created directories."""

    for path in paths:
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _refuse(
                BootstrapImageRefusalCode.CLEANUP_NOT_OWNED,
                f"work directory is not empty and was preserved: {path}: {exc}",
            )


def remove_prepared_stage_after_verification(stage: Path) -> tuple[Path, ...]:
    """Remove a prepared stage only from its closed, self-authenticating inventory."""

    ready = stage / "files/READY"
    inventory = stage / "files/build-metadata/stage-files.sha256"
    if not ready.is_file() or not inventory.is_file():
        _refuse(
            BootstrapImageRefusalCode.CLEANUP_NOT_OWNED,
            "prepared stage lacks its READY/inventory ownership records",
        )
    expected_inventory_hash = ready.read_text(encoding="utf-8").strip()
    if _SHA256_RE.fullmatch(expected_inventory_hash) is None:
        _refuse(BootstrapImageRefusalCode.CLEANUP_NOT_OWNED, "invalid stage READY digest")
    if _sha256_file(inventory) != expected_inventory_hash:
        _refuse(BootstrapImageRefusalCode.CLEANUP_NOT_OWNED, "stage inventory digest changed")
    owned: list[Path] = []
    for line in inventory.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if separator != "  " or _SHA256_RE.fullmatch(digest) is None:
            _refuse(BootstrapImageRefusalCode.CLEANUP_NOT_OWNED, "invalid stage inventory line")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            _refuse(BootstrapImageRefusalCode.CLEANUP_NOT_OWNED, "unsafe stage inventory path")
        path = stage / "files" / relative_path
        if not path.is_file() or path.is_symlink() or _sha256_file(path) != digest:
            _refuse(BootstrapImageRefusalCode.CLEANUP_NOT_OWNED, f"stage file changed: {path}")
        owned.append(path)
    owned.extend((inventory, ready))
    for path in owned:
        path.unlink()
    assets_root = stage / "files"
    directories = sorted(
        (path for path in assets_root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        directory.rmdir()
    assets_root.rmdir()
    return tuple(owned)


def _configure_apt_snapshots(rootfs: Path, requirements: BuilderRequirements) -> None:
    """Replace moving apt sources with the two reviewed immutable snapshots."""

    apt = rootfs / "etc/apt"
    sources_dir = apt / "sources.list.d"
    if not apt.is_dir() or apt.is_symlink() or not sources_dir.is_dir() or sources_dir.is_symlink():
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, "target apt source directories are unsafe")
    candidates = [apt / "sources.list", *sorted(sources_dir.glob("*.list"))]
    candidates.extend(sorted(sources_dir.glob("*.sources")))
    for source in candidates:
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, f"unsafe apt source: {source}")
        disabled = source.with_name(source.name + ".dashcam-bootstrap-disabled")
        if disabled.exists() or disabled.is_symlink():
            _refuse(
                BootstrapImageRefusalCode.OUTPUT_EXISTS,
                f"apt source backup already exists: {disabled}",
            )
        source.rename(disabled)
    snapshot = sources_dir / "dashcam-bootstrap.sources"
    if snapshot.exists() or snapshot.is_symlink():
        _refuse(BootstrapImageRefusalCode.OUTPUT_EXISTS, "snapshot apt source already exists")
    payload = (
        "Types: deb\n"
        f"URIs: {requirements.debian_snapshot}\n"
        "Suites: trixie\n"
        "Components: main contrib non-free non-free-firmware\n"
        "Architectures: armhf\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
        "Check-Valid-Until: no\n\n"
        "Types: deb\n"
        f"URIs: {requirements.raspberrypi_snapshot}\n"
        "Suites: trixie\n"
        "Components: main\n"
        "Architectures: armhf\n"
        "Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.gpg\n"
        "Check-Valid-Until: no\n"
    )
    with snapshot.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _closed_mapping(
    value: object,
    keys: set[str],
    label: str,
    code: BootstrapImageRefusalCode = BootstrapImageRefusalCode.EVIDENCE_INVALID,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys or not all(
        isinstance(key, str) for key in value
    ):
        _refuse(
            code,
            f"{label} must contain exactly {sorted(keys)}",
        )
    return cast(Mapping[str, object], value)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _refuse(BootstrapImageRefusalCode.EVIDENCE_INVALID, f"{label} must be a string")
    return value


def _required_sha256(
    value: object,
    label: str,
    code: BootstrapImageRefusalCode,
) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _refuse(code, f"{label} must be a lowercase SHA-256")
    return value


def _validate_new_regular_output(path: Path, suffix: str) -> None:
    _refuse_device_path(path)
    _refuse_symlink_ancestors(path)
    if not path.is_absolute() or not str(path).endswith(suffix):
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            f"new output must be an absolute {suffix} path",
        )
    if path.exists() or path.is_symlink():
        _refuse(BootstrapImageRefusalCode.OUTPUT_EXISTS, f"output exists: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            f"output parent must be an existing real directory: {path.parent}",
        )


def _validate_artifact_digest(artifact: ArtifactDigest, *, expected_suffix: str) -> None:
    if artifact.size_bytes <= 0 or _SHA256_RE.fullmatch(artifact.sha256) is None:
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            f"invalid artifact digest for {artifact.path}",
        )
    if not artifact.path.endswith(expected_suffix):
        _refuse(
            BootstrapImageRefusalCode.ARTIFACT_INVALID,
            f"artifact must end with {expected_suffix}: {artifact.path}",
        )


def _require_existing_regular_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        _refuse(BootstrapImageRefusalCode.PATH_UNSAFE, f"{label} path must be absolute")
    _refuse_device_path(path)
    _refuse_symlink_ancestors(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        _refuse(BootstrapImageRefusalCode.SOURCE_INVALID, f"{label} does not exist: {path}")
    if not stat.S_ISREG(mode):
        _refuse(
            BootstrapImageRefusalCode.SOURCE_INVALID,
            f"{label} must be a regular file: {path}",
        )


def _refuse_device_path(path: Path) -> None:
    raw = str(path)
    normalized = raw.replace("/", "\\").lower()
    if (
        raw.startswith("/dev/")
        or normalized.startswith(r"\\.\physicaldrive")
        or normalized.startswith(r"\\?\physicaldrive")
        or _WINDOWS_DEVICE_RE.fullmatch(raw) is not None
    ):
        _refuse(BootstrapImageRefusalCode.BLOCK_DEVICE, f"device path refused: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        _refuse(BootstrapImageRefusalCode.BLOCK_DEVICE, f"device node refused: {path}")
    if stat.S_ISLNK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        _refuse(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            f"special or symbolic path refused: {path}",
        )


def _refuse_symlink_ancestors(path: Path) -> None:
    candidate = path if path.exists() or path.is_symlink() else path.parent
    while candidate != candidate.parent:
        try:
            if stat.S_ISLNK(candidate.lstat().st_mode):
                _refuse(
                    BootstrapImageRefusalCode.PATH_UNSAFE,
                    f"symbolic-link path component refused: {candidate}",
                )
        except FileNotFoundError:
            pass
        candidate = candidate.parent


def _absolute_without_resolution(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _refuse(code: BootstrapImageRefusalCode, message: str) -> NoReturn:
    raise BootstrapImageRefused(code, message)
