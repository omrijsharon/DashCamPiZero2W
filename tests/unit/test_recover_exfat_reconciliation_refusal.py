from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "bootstrap" / "ssh-dev" / "recover-exfat-reconciliation-refusal.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recover_exfat_reconciliation_refusal", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths(tmp_path: Path, module: ModuleType) -> tuple[Path, Path]:
    state = tmp_path / "bootstrap-v1.json"
    state.write_bytes(module.SOURCE)
    return state, tmp_path / "exfat-audit"


def _verify(module: ModuleType) -> Callable[[bytes], None]:
    return lambda value: module._need(
        value in {module.SOURCE, module.REPLACEMENT}, "unexpected test state"
    )


def _blkid(module: ModuleType) -> str:
    return "\n".join(
        (
            "DEVNAME=/dev/mmcblk0p3",
            "LABEL=DASHCAM",
            f"UUID={module.DATA_UUID}",
            "VERSION=1.0",
            "FSBLOCKSIZE=512",
            "BLOCK_SIZE=512",
            "FSSIZE=24468520960",
            "TYPE=exfat",
            "USAGE=filesystem",
            "PART_ENTRY_SCHEME=dos",
            "PART_ENTRY_UUID=4f2c9ea0-03",
            "PART_ENTRY_TYPE=0x7",
            "PART_ENTRY_NUMBER=3",
            "PART_ENTRY_OFFSET=13647872",
            "PART_ENTRY_SIZE=47790080",
            "PART_ENTRY_DISK=179:0",
            "",
        )
    )


def _wipefs(module: ModuleType) -> str:
    return json.dumps(
        {
            "signatures": [
                {
                    "device": "mmcblk0p3",
                    "offset": "0x3",
                    "type": "exfat",
                    "uuid": module.DATA_UUID,
                    "label": "DASHCAM",
                },
                {
                    "device": "mmcblk0p3",
                    "offset": "0x1fe",
                    "type": "dos",
                    "uuid": None,
                    "label": None,
                },
            ]
        }
    )


def test_embedded_source_is_exact_and_replacement_changes_only_latch_fields() -> None:
    module = _module()

    assert hashlib.sha256(module.SOURCE).hexdigest() == module.SOURCE_SHA
    assert module._normalized_self_hash(SCRIPT.read_bytes()) == module.SELF_SHA
    source = json.loads(module.SOURCE)
    replacement = json.loads(module.REPLACEMENT)
    changed = {key for key in source if source[key] != replacement[key]}

    assert changed == {"phase", "refusal_code", "refusal_message"}
    assert replacement["phase"] == "format_intent"
    assert replacement["refusal_code"] is None
    assert replacement["refusal_message"] is None


def test_dry_run_is_true_and_ignores_separate_completed_fssize_audit(
    tmp_path: Path,
) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)
    earlier = tmp_path / "fssize-recovery-v1"
    earlier.mkdir()
    (earlier / "recovery.json").write_text('{"status":"complete"}\n')

    result = module.recover(apply=False, state_path=state, audit_dir=audit, verify=_verify(module))

    assert result == {"operation": "dry-run", "ready": True, "state": "refused"}
    assert state.read_bytes() == module.SOURCE
    assert not audit.exists()
    assert (earlier / "recovery.json").exists()


@pytest.mark.parametrize("point", ("after_archive", "after_prepared", "after_state_replace"))
def test_faults_leave_exact_refused_or_intent_and_retry_completes(
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


def test_other_refusal_cannot_be_cleared_and_writes_nothing(tmp_path: Path) -> None:
    module = _module()
    state, audit = _paths(tmp_path, module)
    other = json.loads(module.SOURCE)
    other["data_uuid"] = "FOREIGN"
    state.write_text(json.dumps(other, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(module.RecoveryError, match="exact source"):
        module.recover(apply=True, state_path=state, audit_dir=audit, verify=lambda _value: None)

    assert json.loads(state.read_bytes())["data_uuid"] == "FOREIGN"
    assert not audit.exists()


def test_live_exfat_parsers_bind_exact_identity_and_wipefs_shape() -> None:
    module = _module()

    module._parse_p3_blkid(_blkid(module))
    module._parse_p3_wipefs(_wipefs(module))

    foreign_blkid = _blkid(module).replace("LABEL=DASHCAM", "LABEL=FOREIGN")
    with pytest.raises(module.RecoveryError, match="blkid identity differs"):
        module._parse_p3_blkid(foreign_blkid)

    foreign_wipefs = json.loads(_wipefs(module))
    foreign_wipefs["signatures"].append(
        {
            "device": "mmcblk0p3",
            "offset": "0x438",
            "type": "ext4",
            "uuid": "FOREIGN",
            "label": None,
        }
    )
    with pytest.raises(module.RecoveryError, match="wipefs signature shape differs"):
        module._parse_p3_wipefs(json.dumps(foreign_wipefs))


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
