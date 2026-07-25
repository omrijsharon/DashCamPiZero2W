from __future__ import annotations

import os
from pathlib import Path

import pytest

from dashcam.provisioning.layout import LayoutSpec, load_layout_toml
from dashcam.provisioning.loopback import (
    SUPPORTED_CAPACITY_BYTES,
    LoopbackRefusal,
    LoopbackStatus,
    assert_disposable_regular_file,
    geometry_for,
    prepare_output_directory,
    run_validation,
)

ROOT = Path(__file__).parents[2]


def _spec() -> LayoutSpec:
    return load_layout_toml((ROOT / "deploy" / "storage" / "layout-v1.toml").read_bytes())


def test_geometry_is_exact_for_reviewed_nominal_card_capacities() -> None:
    spec = _spec()
    for capacity in SUPPORTED_CAPACITY_BYTES:
        geometry = geometry_for(spec, capacity)
        assert geometry.root_size == 6 * 1024**3 // 512
        assert geometry.data_start % 2048 == 0
        assert (geometry.data_end + 1) % 2048 == 0
        assert geometry.data_size >= spec.minimum_data_sectors


def test_output_must_be_new_empty_real_directory(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "old.json").write_text("owner output", encoding="utf-8")
    with pytest.raises(LoopbackRefusal, match="already contains"):
        prepare_output_directory(occupied)
    missing = tmp_path / "missing"
    with pytest.raises(LoopbackRefusal, match="created by the caller"):
        prepare_output_directory(missing)


def test_special_targets_symlinks_and_parent_escape_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    with pytest.raises(LoopbackRefusal, match="parent traversal"):
        assert_disposable_regular_file(root / ".." / "escape.img", root, must_exist=False)
    link = root / "link.img"
    try:
        link.symlink_to(tmp_path / "target.img")
    except OSError:
        pass
    else:
        with pytest.raises(LoopbackRefusal, match="symlink"):
            assert_disposable_regular_file(link, root, must_exist=False)
    owner_image = root / "old.img"
    owner_image.write_bytes(b"old")
    with pytest.raises(LoopbackRefusal, match="existing owner output"):
        assert_disposable_regular_file(owner_image, root, must_exist=False)
    with pytest.raises(LoopbackRefusal, match="block, character"):
        assert_disposable_regular_file(root, root, must_exist=True)
    if os.name == "posix":
        with pytest.raises(LoopbackRefusal, match="host root"):
            assert_disposable_regular_file(Path("/dev/null"), root, must_exist=True)


@pytest.mark.integration
def test_regular_file_harness_passes_or_explicitly_skips(tmp_path: Path) -> None:
    output = tmp_path / "new-output"
    output.mkdir()
    report = run_validation(output, _spec(), fault_matrix=True)
    assert report.status in {LoopbackStatus.PASSED, LoopbackStatus.SKIPPED}
    if report.status is LoopbackStatus.PASSED:
        cases = {str(item.get("case")): item for item in report.cases if "case" in item}
        assert set(cases) == {"31457280000B", "64000000000B"}
        assert all(item["idempotent"] is True for item in cases.values())
        assert all(item["data_signature_refusal"] is True for item in cases.values())
        assert all(item["backup_identity_refusal"] is True for item in cases.values())
        assert all(len(str(item["backup_restore_sha256"])) == 64 for item in cases.values())
        faults = [item for item in report.cases if "fault_after" in item]
        assert len(faults) == 8
    else:
        assert report.code == "dependencies_unavailable"
        assert list(output.iterdir()) == []
