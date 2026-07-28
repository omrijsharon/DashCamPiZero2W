#!/usr/bin/env python3
"""Build one hash-closed application bundle for the live SSH-development Pi."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

PACKAGE_SOURCE = Path("deploy/bootstrap/image/pi-gen-stage/00-packages")
SOURCE_FILES = {
    "README.md": Path("deploy/ssh-dev-app/README.md"),
    "install.py": Path("deploy/ssh-dev-app/install.py"),
    "config.toml": Path("config/default.toml"),
    "dashcam-network-fallback.service": Path("deploy/ssh-dev-app/dashcam-network-fallback.service"),
    "dashcam-storage-check.service": Path("deploy/ssh-dev-app/dashcam-storage-check.service"),
    "dashcamd.service": Path("systemd/dashcamd.service"),
}
APP_NAME = "dashcam-pizero2w"
TZDATA_NAME = "tzdata"
TZDATA_VERSION = "2026.3"
TZDATA_WHEEL_SHA256 = "dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931"
TZDATA_WHEEL_SIZE = 348_168
MAX_FILE_BYTES = 64 * 1024 * 1024
# Release construction is currently measured at 18,710,528 bytes on the exact
# Pi. Keep more than 27 times that measured bound; the installer accounts for
# the separately simulated APT peak in addition to this reserve and still
# requires 2 GiB free after both.
INSTALL_BUDGET_BYTES = 512 * 1024**2


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def checked_read(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"unsafe source file: {path}")
    if info.st_size > limit:
        raise ValueError(f"oversized source file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError(f"source identity changed: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(value) > limit:
        raise ValueError(f"oversized source file: {path}")
    if len(value) != current.st_size:
        raise ValueError(f"source file was not read completely: {path}")
    return value


def wheel_identity(path: Path) -> tuple[str, str, bytes]:
    payload = checked_read(path)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError("wheel must contain exactly one METADATA file")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid wheel: {path.name}") from exc
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version or len(payload) == 0:
        raise ValueError(f"wheel metadata is incomplete: {path.name}")
    return name.lower().replace("_", "-"), version, payload


def parse_packages(payload: bytes) -> list[str]:
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("APT package list is not ASCII") from exc
    packages = [
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if not packages or packages != sorted(set(packages)):
        raise ValueError("APT package list must be nonempty, sorted, and unique")
    if any(
        not package.replace("+", "").replace("-", "").replace(".", "").isalnum()
        for package in packages
    ):
        raise ValueError("APT package list contains an unsafe name")
    return packages


def _copy_file(source: Path, destination: Path) -> dict[str, object]:
    payload = checked_read(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    copied = checked_read(destination)
    source_identity = {"sha256": sha256(payload), "size": len(payload)}
    copied_identity = {"sha256": sha256(copied), "size": len(copied)}
    if copied_identity != source_identity:
        raise ValueError(f"copied file identity differs from source: {source.name}")
    return copied_identity


def _build_app_wheel(repository: Path, output: Path) -> Path:
    result = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(output)],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"uv wheel build failed: {result.stderr[-2000:]}")
    matches = sorted(output.glob("dashcam_pizero2w-*.whl"))
    if len(matches) != 1:
        raise ValueError("working-tree build did not produce exactly one app wheel")
    return matches[0]


def prepare(
    repository: Path,
    output: Path,
    tzdata_wheel: Path,
    *,
    app_wheel: Path | None = None,
) -> None:
    repository = repository.resolve(strict=True)
    output = output.resolve()
    if (
        not repository.is_dir()
        or output.exists()
        or output.parent.resolve(strict=True) == repository
    ):
        raise ValueError("repository/output contract is unsafe")
    if repository in output.parents or output in repository.parents:
        raise ValueError("repository and bundle may not overlap")
    tz_name, tz_version, tz_payload = wheel_identity(tzdata_wheel)
    if (tz_name, tz_version) != (TZDATA_NAME, TZDATA_VERSION):
        raise ValueError(f"tzdata wheel must be exactly {TZDATA_NAME} {TZDATA_VERSION}")
    if len(tz_payload) != TZDATA_WHEEL_SIZE or sha256(tz_payload) != TZDATA_WHEEL_SHA256:
        raise ValueError("tzdata wheel differs from the exact uv.lock wheel identity")

    with tempfile.TemporaryDirectory(prefix="dashcam-wheel-") as temporary:
        built = app_wheel or _build_app_wheel(repository, Path(temporary))
        app_name, app_version, app_payload = wheel_identity(built)
        if app_name != APP_NAME:
            raise ValueError(f"application wheel must be {APP_NAME}")

        output.mkdir(mode=0o700)
        files: dict[str, dict[str, object]] = {}
        for destination, source in SOURCE_FILES.items():
            files[destination] = _copy_file(repository / source, output / destination)
        package_payload = checked_read(repository / PACKAGE_SOURCE)
        packages = parse_packages(package_payload)
        files["apt-packages.txt"] = _copy_file(
            repository / PACKAGE_SOURCE, output / "apt-packages.txt"
        )
        app_destination = f"wheels/{built.name}"
        tz_destination = f"wheels/{tzdata_wheel.name}"
        files[app_destination] = _copy_file(built, output / app_destination)
        files[tz_destination] = _copy_file(tzdata_wheel, output / tz_destination)
        if files[app_destination] != {
            "sha256": sha256(app_payload),
            "size": len(app_payload),
        }:
            raise ValueError("copied application wheel differs from validated source")
        if files[tz_destination] != {
            "sha256": TZDATA_WHEEL_SHA256,
            "size": TZDATA_WHEEL_SIZE,
        }:
            raise ValueError("copied tzdata wheel differs from the uv.lock identity")

        release_inputs = {
            "schema_version": 1,
            "application": {
                "name": APP_NAME,
                "version": app_version,
                "wheel": app_destination,
            },
            "tzdata": {
                "name": TZDATA_NAME,
                "version": TZDATA_VERSION,
                "wheel": tz_destination,
            },
            "apt_packages": packages,
            "install_budget_bytes": INSTALL_BUDGET_BYTES,
            "files": dict(sorted(files.items())),
        }
        release_digest = sha256(
            json.dumps(release_inputs, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
        release_id = f"{app_version}-{release_digest[:16]}"
        manifest = {"release_id": release_id, **release_inputs}
        manifest_payload = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        manifest_path = output / "manifest.json"
        manifest_path.write_bytes(manifest_payload)
        os.chmod(manifest_path, 0o600)

        sums = {
            **{name: str(details["sha256"]) for name, details in files.items()},
            "manifest.json": sha256(manifest_payload),
        }
        sums_payload = "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items()))
        (output / "SHA256SUMS").write_text(sums_payload, encoding="ascii", newline="\n")
        os.chmod(output / "SHA256SUMS", 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tzdata-wheel", required=True, type=Path)
    parser.add_argument("--app-wheel", type=Path)
    arguments = parser.parse_args(argv)
    try:
        prepare(
            arguments.repository,
            arguments.output,
            arguments.tzdata_wheel.resolve(strict=True),
            app_wheel=arguments.app_wheel.resolve(strict=True) if arguments.app_wheel else None,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
