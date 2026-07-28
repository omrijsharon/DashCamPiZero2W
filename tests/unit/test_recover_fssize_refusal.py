from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "bootstrap" / "ssh-dev" / "recover-fssize-refusal.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recover_fssize_refusal", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths(tmp_path: Path, module: ModuleType) -> tuple[Path, Path]:
    state = tmp_path / "bootstrap-v1.json"
    state.write_bytes(module.SOURCE)
    return state, tmp_path / "audit"


def _verify(module: ModuleType) -> Callable[[bytes], None]:
    return lambda value: module._need(
        value in {module.SOURCE, module.REPLACEMENT}, "unexpected test state"
    )


def test_embedded_source_and_replacement_are_exact_and_change_only_latch_fields() -> None:
    module = _module()

    assert hashlib.sha256(module.SOURCE).hexdigest() == module.SOURCE_SHA
    source = json.loads(module.SOURCE)
    replacement = json.loads(module.REPLACEMENT)
    changed = {key for key in source if source[key] != replacement[key]}

    assert changed == {"phase", "refusal_code", "refusal_message"}
    assert replacement["phase"] == "table_committed"
    assert replacement["refusal_code"] is None
    assert replacement["refusal_message"] is None


def test_dry_run_is_true_and_creates_no_recovery_artifacts(tmp_path: Path) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)

    result = module.recover(apply=False, state_path=state, audit_dir=audit, verify=_verify(module))

    assert result == {"operation": "dry-run", "ready": True, "state": "refused"}
    assert state.read_bytes() == module.SOURCE
    assert not audit.exists()


@pytest.mark.parametrize("point", ("after_archive", "after_prepared", "after_state_replace"))
def test_faults_leave_exact_refused_or_exact_restored_and_retry_completes(
    tmp_path: Path, point: str
) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)

    def fault(observed: str) -> None:
        if observed == point:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=point):
        module.recover(
            apply=True,
            state_path=state,
            audit_dir=audit,
            verify=_verify(module),
            fault=fault,
        )
    assert state.read_bytes() in {module.SOURCE, module.REPLACEMENT}

    recovered = module.recover(
        apply=True, state_path=state, audit_dir=audit, verify=_verify(module)
    )
    assert recovered["state"] == "restored"
    assert state.read_bytes() == module.REPLACEMENT
    archive = audit / f"bootstrap-v1.refused-{module.SOURCE_SHA}.json"
    record = audit / f"recovery-{module.SOURCE_SHA}.json"
    assert archive.read_bytes() == module.SOURCE
    assert json.loads(record.read_bytes())["status"] == "complete"

    again = module.recover(apply=True, state_path=state, audit_dir=audit, verify=_verify(module))
    assert again["state"] == "already-restored"


def test_later_or_different_refusal_cannot_be_cleared_and_writes_nothing(
    tmp_path: Path,
) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)
    later = json.loads(module.SOURCE)
    later["refusal_code"] = "format_not_blank"
    state.write_text(json.dumps(later, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(module.RecoveryError, match="exact source"):
        module.recover(apply=True, state_path=state, audit_dir=audit, verify=lambda _value: None)

    assert json.loads(state.read_bytes())["refusal_code"] == "format_not_blank"
    assert not audit.exists()


def test_wrong_existing_archive_refuses_before_any_additional_write(tmp_path: Path) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)
    audit.mkdir()
    archive = audit / f"bootstrap-v1.refused-{module.SOURCE_SHA}.json"
    archive.write_bytes(b"foreign")

    with pytest.raises(module.RecoveryError, match="archive differs"):
        module.recover(apply=True, state_path=state, audit_dir=audit, verify=_verify(module))

    assert state.read_bytes() == module.SOURCE
    assert tuple(audit.iterdir()) == (archive,)


def test_helper_contains_no_storage_mutator_subprocess() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "/usr/sbin/sfdisk",
        "/usr/sbin/resize2fs",
        "/usr/sbin/mkfs",
        "/usr/bin/mount",
        "/usr/bin/systemctl",
    ):
        assert forbidden not in source


def test_blank_p3_partition_metadata_is_not_mistaken_for_a_filesystem() -> None:
    module = _module()
    module._parse_p3_blkid(
        "DEVNAME=/dev/mmcblk0p3\n"
        "PART_ENTRY_SCHEME=dos\n"
        "PART_ENTRY_UUID=4f2c9ea0-03\n"
        "PART_ENTRY_TYPE=0x7\n"
        "PART_ENTRY_NUMBER=3\n"
        "PART_ENTRY_OFFSET=13647872\n"
        "PART_ENTRY_SIZE=47790080\n"
        "PART_ENTRY_DISK=179:0\n"
    )

    with pytest.raises(module.RecoveryError, match="filesystem identity"):
        module._parse_p3_blkid(
            "TYPE=exfat\nLABEL=DASHCAM\nUUID=1234-ABCD\n"
            "PART_ENTRY_SCHEME=dos\nPART_ENTRY_UUID=4f2c9ea0-03\n"
            "PART_ENTRY_TYPE=0x7\nPART_ENTRY_NUMBER=3\n"
            "PART_ENTRY_OFFSET=13647872\nPART_ENTRY_SIZE=47790080\n"
        )


def test_unsafe_existing_audit_directory_refuses_before_writes(tmp_path: Path) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    try:
        audit.symlink_to(foreign, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create directory symlinks: {exc}")

    with pytest.raises(module.RecoveryError, match="audit directory is unsafe"):
        module.recover(apply=True, state_path=state, audit_dir=audit, verify=_verify(module))

    assert state.read_bytes() == module.SOURCE
    assert not tuple(foreign.iterdir())
