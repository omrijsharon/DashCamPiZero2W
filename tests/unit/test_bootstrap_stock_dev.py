from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import dashcam.provisioning.bootstrap as bootstrap_module
from dashcam.provisioning.bootstrap import (
    AUTHORIZED_CID,
    AUTHORIZED_SIZE_BYTES,
    DATA_ZERO_PREFIX_BYTES,
    EXACT_STOCK_CARD_AUTHORIZATION,
    KNOWN_CLOUD_INIT_WARNING,
    SSH_DEV_TRIGGER,
    ActionKind,
    BootstrapError,
    Evidence,
    Partition,
    Phase,
    PosixRuntime,
    Refusal,
    RefusalCode,
    _canonical_sysfs_cid_path,
    _classify_cloud_init_status,
    journal_from_json,
    journal_json,
    load_bootstrap_contract,
    main,
    partition_path,
    plan_stage_a,
    plan_stage_b,
)

SECTOR_SIZE = 512
TOTAL_SECTORS = 61_440_000
BOOT = Partition(1, 16_384, 1_048_576, 0x0C)
STOCK_ROOT = Partition(2, 1_064_960, 4_161_536, 0x83)
TARGET_ROOT_SIZE = 12_582_912
TARGET_DATA_START = 13_647_872
TARGET_DATA_SIZE = 47_790_080
STOCK_PREFIX_SHA256 = hashlib.sha256(b"stable nonzero stock prefix").hexdigest()


def _mbr(parts: tuple[Partition, ...], disk_id: int = 0x1234ABCD) -> bytes:
    result = bytearray(512)
    result[440:444] = disk_id.to_bytes(4, "little")
    for part in parts:
        offset = 446 + (part.number - 1) * 16
        result[offset + 4] = part.type_code
        result[offset + 8 : offset + 12] = part.start_sector.to_bytes(4, "little")
        result[offset + 12 : offset + 16] = part.size_sectors.to_bytes(4, "little")
    result[510:512] = b"\x55\xaa"
    return bytes(result)


def _source_evidence() -> Evidence:
    disk = "/dev/mmcblk0"
    parts = (BOOT, STOCK_ROOT)
    return Evidence(
        cmdline=("console=serial0,115200", SSH_DEV_TRIGGER),
        boot_id="stock-boot-a",
        root_partition=partition_path(disk, 2),
        disk=disk,
        cid=AUTHORIZED_CID,
        size_bytes=AUTHORIZED_SIZE_BYTES,
        sector_size=SECTOR_SIZE,
        mbr=_mbr(parts),
        partitions=parts,
        root_filesystem_bytes=STOCK_ROOT.size_sectors * SECTOR_SIZE,
        root_filesystem="ext4",
        root_uuid="ROOT-1234",
        root_partuuid="1234abcd-02",
        boot_partition=partition_path(disk, 1),
        boot_mounted_source=partition_path(disk, 1),
        boot_filesystem="vfat",
        boot_uuid="BOOT-1234",
        boot_partuuid="1234abcd-01",
        cloud_init_status="done",
        data_prefix_sha256=STOCK_PREFIX_SHA256,
    )


def _stock_contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "bootstrap_trigger": SSH_DEV_TRIGGER,
        "cid": AUTHORIZED_CID,
        "size_bytes": AUTHORIZED_SIZE_BYTES,
        "sector_size": SECTOR_SIZE,
        "source": {
            "boot_start_sector": BOOT.start_sector,
            "boot_size_sectors": BOOT.size_sectors,
            "root_start_sector": STOCK_ROOT.start_sector,
            "root_size_sectors": STOCK_ROOT.size_sectors,
        },
        "target": {
            "root_size_bytes": 6 * 1024**3,
            "minimum_device_bytes": 28 * 1024**3,
            "minimum_data_bytes": 8 * 1024**3,
            "alignment_bytes": 1024**2,
            "trailing_reserve_bytes": 1024**2,
        },
    }


def _sysfs_resolution(
    *,
    block: str = "/sys/devices/platform/soc/mmc/mmc0:0001/block/mmcblk0",
    device: str = "/sys/devices/platform/soc/mmc/mmc0:0001",
    cid: str = "/sys/devices/platform/soc/mmc/mmc0:0001/cid",
) -> dict[str, str]:
    class_root = "/sys/class/block/mmcblk0"
    return {
        class_root: block,
        f"{class_root}/device": device,
        f"{class_root}/device/cid": cid,
    }


def _patch_realpath(monkeypatch: pytest.MonkeyPatch, resolutions: dict[str, str]) -> None:
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda path: resolutions.get(os.fspath(path), os.fspath(path)),
    )


def test_cid_path_accepts_exact_canonical_sysfs_disk_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "/sys/devices/platform/soc/mmc/mmc0:0001/cid"
    _patch_realpath(monkeypatch, _sysfs_resolution(cid=expected))
    assert _canonical_sysfs_cid_path("mmcblk0") == expected


@pytest.mark.parametrize(
    "cid_path",
    [
        "/etc/passwd",
        "/sys/devices/platform/soc/mmc/bad path/cid",
    ],
)
def test_cid_path_refuses_escape_or_unsafe_canonical_components(
    cid_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_realpath(monkeypatch, _sysfs_resolution(cid=cid_path))
    with pytest.raises(Refusal) as caught:
        _canonical_sysfs_cid_path("mmcblk0")
    assert caught.value.code is RefusalCode.IDENTITY_MISMATCH


def test_cid_path_refuses_a_path_that_remains_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = "/sys/devices/platform/soc/mmc/mmc0:0001/cid"
    resolutions = _sysfs_resolution(cid=canonical)
    resolutions[canonical] = "/sys/devices/platform/soc/mmc/mmc0:0001/other"
    _patch_realpath(monkeypatch, resolutions)
    with pytest.raises(Refusal) as caught:
        _canonical_sysfs_cid_path("mmcblk0")
    assert caught.value.code is RefusalCode.IDENTITY_MISMATCH


def test_cid_path_refuses_wrong_disk_to_device_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _sysfs_resolution(
        block="/sys/devices/platform/soc/mmc/device-a/block/mmcblk0",
        device="/sys/devices/platform/soc/mmc/device-b",
        cid="/sys/devices/platform/soc/mmc/device-b/cid",
    )
    _patch_realpath(monkeypatch, resolutions)
    with pytest.raises(Refusal) as caught:
        _canonical_sysfs_cid_path("mmcblk0")
    assert caught.value.code is RefusalCode.IDENTITY_MISMATCH


@pytest.mark.parametrize("disk_name", ["", "../mmcblk0", "mmcblk0/partition", "bad disk"])
def test_cid_path_refuses_malformed_derived_disk_name(disk_name: str) -> None:
    with pytest.raises(Refusal) as caught:
        _canonical_sysfs_cid_path(disk_name)
    assert caught.value.code is RefusalCode.IDENTITY_MISMATCH


def test_raw_prefix_reader_hashes_exact_offset_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = b"header-not-in-prefix"
    prefix = bytes(range(251)) * (DATA_ZERO_PREFIX_BYTES // 251)
    prefix += bytes(range(DATA_ZERO_PREFIX_BYTES - len(prefix)))
    raw = tmp_path / "raw-device-fixture.img"
    raw.write_bytes(header + prefix + b"trailer-not-in-prefix")

    def open_fixture(_runtime: PosixRuntime, _path: str, *, allow_block: bool) -> int:
        assert allow_block
        return os.open(raw, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    monkeypatch.setattr(PosixRuntime, "_open_readonly", open_fixture)
    runtime = PosixRuntime()

    assert (
        runtime.sha256_region(
            "/dev/fixture",
            offset=len(header),
            length=DATA_ZERO_PREFIX_BYTES,
        )
        == hashlib.sha256(prefix).hexdigest()
    )
    with pytest.raises(BootstrapError, match="outside its exact bound"):
        runtime.sha256_region(
            "/dev/fixture",
            offset=-1,
            length=DATA_ZERO_PREFIX_BYTES,
        )
    with pytest.raises(BootstrapError, match="outside its exact bound"):
        runtime.sha256_region(
            "/dev/fixture",
            offset=len(header),
            length=DATA_ZERO_PREFIX_BYTES - 1,
        )


class _DryRunRuntime:
    def __init__(self, contract: str) -> None:
        self.contract = contract

    def read_text(self, path: str, *, limit: int = 64 * 1024) -> str:
        assert path == "/etc/dev-stock-contract.json"
        assert limit == 64 * 1024
        return self.contract

    def exists(self, path: str) -> bool:
        assert path == bootstrap_module.STATE_PATH
        return False

    def atomic_write(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run attempted an atomic write")

    def run(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("dry-run attempted a runtime command")

    def sync(self) -> None:
        raise AssertionError("dry-run attempted sync")


def _invoke_stage_a_dry_run(
    evidence: Evidence,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    runtime = _DryRunRuntime(json.dumps(_stock_contract()))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(bootstrap_module, "PosixRuntime", lambda: runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "collect_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "execute_stage_a",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run called Stage A executor")
        ),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "execute_stage_b",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run called Stage B executor")
        ),
    )

    result = main(
        [
            "--stage",
            "a",
            "--contract",
            "/etc/dev-stock-contract.json",
            "--dry-run",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    return result, report


def test_live_dry_run_reports_exact_target_without_executing_mutators(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = replace(_source_evidence(), cloud_init_status="done_known_degraded")
    result, report = _invoke_stage_a_dry_run(evidence, monkeypatch, capsys)

    assert result == 0
    assert report["dry_run"] is True
    assert report["ready"] is True
    assert report["outcome"] == "ready"
    assert report["refusal"] is None
    assert report["authorization"]["cid"] == AUTHORIZED_CID
    assert report["evidence"]["mbr_sha256"] == hashlib.sha256(evidence.mbr).hexdigest()
    assert report["evidence"]["data_prefix_sha256"] == STOCK_PREFIX_SHA256
    assert report["journal"]["target"]["root"]["size_sectors"] == TARGET_ROOT_SIZE
    assert report["journal"]["target"]["data"]["start_sector"] == TARGET_DATA_START
    commands = [action["argv"] for action in report["actions"] if action["kind"] == "command"]
    assert commands == [
        ["/usr/sbin/sfdisk", "--no-reread", "--force", evidence.disk],
        ["/usr/bin/sync"],
    ]


@pytest.mark.parametrize("status", ["running", "unknown"])
def test_live_dry_run_deferred_cloud_status_is_not_ready(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, report = _invoke_stage_a_dry_run(
        replace(_source_evidence(), cloud_init_status=status),
        monkeypatch,
        capsys,
    )
    assert result == 3
    assert report["ready"] is False
    assert report["outcome"] == "deferred"
    assert report["refusal"] is None
    assert [action["kind"] for action in report["actions"]] == ["defer"]


@pytest.mark.parametrize("mismatch", ["trigger", "layout"])
def test_live_dry_run_trigger_or_layout_refusal_is_not_ready(
    mismatch: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = _source_evidence()
    if mismatch == "trigger":
        evidence = replace(evidence, cmdline=("dashcam.bootstrap=v1",))
    else:
        wrong_root = replace(STOCK_ROOT, size_sectors=STOCK_ROOT.size_sectors + 2_048)
        wrong_parts = (BOOT, wrong_root)
        evidence = replace(
            evidence,
            partitions=wrong_parts,
            mbr=_mbr(wrong_parts),
        )

    result, report = _invoke_stage_a_dry_run(evidence, monkeypatch, capsys)
    assert result == 3
    assert report["ready"] is False
    assert report["outcome"] == "refused"
    refusal = report["refusal"]
    assert isinstance(refusal, dict)
    assert refusal["code"] in {
        RefusalCode.TRIGGER_MISSING,
        RefusalCode.SOURCE_LAYOUT_MISMATCH,
    }


def test_exact_stock_layout_plans_reviewed_six_gib_target_once() -> None:
    evidence = _source_evidence()
    plan = plan_stage_a(evidence, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)

    assert AUTHORIZED_SIZE_BYTES // SECTOR_SIZE == TOTAL_SECTORS
    assert plan.journal is not None
    assert plan.journal.schema_version == 2
    assert plan.journal.target.root == Partition(2, STOCK_ROOT.start_sector, TARGET_ROOT_SIZE, 0x83)
    assert plan.journal.target.data == Partition(3, TARGET_DATA_START, TARGET_DATA_SIZE, 0x07)
    assert [action.argv[:1] for action in plan.mutating_commands] == [("/usr/sbin/sfdisk",)]
    write = plan.mutating_commands[0]
    assert write.argv == (
        "/usr/sbin/sfdisk",
        "--no-reread",
        "--force",
        evidence.disk,
    )
    assert f"start={TARGET_DATA_START}, size={TARGET_DATA_SIZE}" in (write.stdin or "")


def _known_degraded_cloud_init() -> dict[str, object]:
    return {
        "status": "done",
        "extended_status": "degraded done",
        "errors": [],
        "recoverable_errors": {"WARNING": [KNOWN_CLOUD_INIT_WARNING]},
        "stage": None,
    }


def test_exact_known_cloud_init_degradation_is_pinned_terminal_for_dev_only() -> None:
    assert _classify_cloud_init_status(_known_degraded_cloud_init(), 2) == "done_known_degraded"
    dev = plan_stage_a(
        replace(_source_evidence(), cloud_init_status="done_known_degraded"),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert any(action.argv[:1] == ("/usr/sbin/sfdisk",) for action in dev.actions)
    dev_stage_b = plan_stage_b(
        replace(_source_evidence(), cloud_init_status="done_known_degraded"),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert [action.kind for action in dev_stage_b.actions] == [ActionKind.LATCH]
    assert dev_stage_b.journal is not None
    assert dev_stage_b.journal.refusal_code == RefusalCode.JOURNAL_CONFLICT

    release_evidence = replace(
        _source_evidence(),
        cmdline=("dashcam.bootstrap=v1",),
        cloud_init_status="done_known_degraded",
    )
    release = plan_stage_a(release_evidence, None)
    assert [action.kind for action in release.actions] == [ActionKind.DEFER]
    assert not release.mutating_commands


@pytest.mark.parametrize(
    ("change", "returncode"),
    [
        ({"recoverable_errors": {"WARNING": ["another warning"]}}, 2),
        (
            {"recoverable_errors": {"WARNING": [KNOWN_CLOUD_INIT_WARNING, "additional warning"]}},
            2,
        ),
        ({"errors": ["fatal"]}, 2),
        ({"stage": "modules-final"}, 2),
        ({"extended_status": "done"}, 2),
        ({}, 0),
    ],
)
def test_cloud_init_degraded_pin_rejects_any_shape_or_exit_drift(
    change: dict[str, object], returncode: int
) -> None:
    value = _known_degraded_cloud_init()
    value.update(change)
    status = _classify_cloud_init_status(value, returncode)
    assert status != "done_known_degraded"
    dev = plan_stage_a(
        replace(_source_evidence(), cloud_init_status=status),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert [action.kind for action in dev.actions] == [ActionKind.DEFER]
    assert not dev.mutating_commands


@pytest.mark.parametrize(
    ("wrong_cid", "wrong_size", "code"),
    [
        ("0" * 32, AUTHORIZED_SIZE_BYTES, RefusalCode.IDENTITY_MISMATCH),
        (
            AUTHORIZED_CID,
            AUTHORIZED_SIZE_BYTES + SECTOR_SIZE,
            RefusalCode.IDENTITY_MISMATCH,
        ),
    ],
)
def test_stock_contract_refuses_wrong_card_identity(
    wrong_cid: str, wrong_size: int, code: RefusalCode
) -> None:
    plan = plan_stage_a(
        replace(_source_evidence(), cid=wrong_cid, size_bytes=wrong_size),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == code
    assert not plan.mutating_commands


def test_stock_contract_refuses_mismatched_source_p2() -> None:
    evidence = _source_evidence()
    wrong_root = replace(STOCK_ROOT, size_sectors=STOCK_ROOT.size_sectors + 2_048)
    wrong_parts = (BOOT, wrong_root)
    evidence = replace(evidence, partitions=wrong_parts, mbr=_mbr(wrong_parts))

    plan = plan_stage_a(evidence, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)

    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.SOURCE_LAYOUT_MISMATCH
    assert not plan.mutating_commands


def test_stage_b_refuses_target_geometry_drift() -> None:
    source = _source_evidence()
    stage_a = plan_stage_a(source, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert stage_a.journal is not None
    journal = replace(stage_a.journal, phase=Phase.TABLE_COMMITTED)
    wrong_root = replace(journal.target.root, size_sectors=TARGET_ROOT_SIZE - 2_048)
    parts = (BOOT, wrong_root, journal.target.data)
    target = replace(
        source,
        boot_id="stock-boot-b",
        partitions=parts,
        mbr=_mbr(parts),
        data_partuuid="1234abcd-03",
    )

    plan = plan_stage_b(target, journal, authorization=EXACT_STOCK_CARD_AUTHORIZATION)

    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.TORN_TABLE
    assert not plan.mutating_commands


def test_stock_provenance_accepts_stable_nonzero_bounded_prefix_hash() -> None:
    source = _source_evidence()
    stage_a = plan_stage_a(source, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert stage_a.journal is not None
    parts = (BOOT, stage_a.journal.target.root, stage_a.journal.target.data)
    mbr = _mbr(parts)
    journal = replace(
        stage_a.journal,
        phase=Phase.ROOT_RESIZED,
        committed_mbr_sha256=hashlib.sha256(mbr).hexdigest(),
    )
    evidence = replace(
        source,
        boot_id="stock-boot-b",
        partitions=parts,
        mbr=mbr,
        root_filesystem_bytes=TARGET_ROOT_SIZE * SECTOR_SIZE,
        data_partuuid="1234abcd-03",
        data_filesystem=None,
        data_signatures=(),
        data_zero_prefix_bytes=0,
        data_prefix_sha256=STOCK_PREFIX_SHA256,
    )

    blank = plan_stage_b(evidence, journal, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert blank.journal is not None and blank.journal.phase is Phase.FORMAT_INTENT
    assert blank.mutating_commands[0].argv[:1] == ("/usr/sbin/mkfs.exfat",)

    signed = plan_stage_b(
        replace(evidence, data_signatures=("ext4",)),
        journal,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert signed.journal is not None and signed.journal.phase is Phase.REFUSED
    assert signed.journal.refusal_code == RefusalCode.FORMAT_NOT_BLANK
    assert not signed.mutating_commands

    drifted_prefix = plan_stage_b(
        replace(
            evidence,
            data_prefix_sha256=hashlib.sha256(b"changed prefix").hexdigest(),
        ),
        journal,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert drifted_prefix.journal is not None and drifted_prefix.journal.phase is Phase.REFUSED
    assert drifted_prefix.journal.refusal_code == RefusalCode.FORMAT_NOT_BLANK
    assert "prefix hash drifted" in (drifted_prefix.journal.refusal_message or "")
    assert not drifted_prefix.mutating_commands


@pytest.mark.parametrize("prefix_hash", [None, "not-a-sha256"])
def test_stock_provenance_refuses_missing_or_malformed_observed_hash(
    prefix_hash: str | None,
) -> None:
    source = _source_evidence()
    stage_a = plan_stage_a(source, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert stage_a.journal is not None
    parts = (BOOT, stage_a.journal.target.root, stage_a.journal.target.data)
    mbr = _mbr(parts)
    journal = replace(
        stage_a.journal,
        phase=Phase.ROOT_RESIZED,
        committed_mbr_sha256=hashlib.sha256(mbr).hexdigest(),
    )
    evidence = replace(
        source,
        boot_id="stock-boot-b",
        partitions=parts,
        mbr=mbr,
        root_filesystem_bytes=TARGET_ROOT_SIZE * SECTOR_SIZE,
        data_partuuid="1234abcd-03",
        data_prefix_sha256=prefix_hash,
    )

    plan = plan_stage_b(evidence, journal, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.FORMAT_NOT_BLANK
    assert not plan.mutating_commands


@pytest.mark.parametrize("prefix_hash", [None, "not-a-sha256"])
def test_stock_stage_a_refuses_missing_or_malformed_prefix_hash(
    prefix_hash: str | None,
) -> None:
    plan = plan_stage_a(
        replace(_source_evidence(), data_prefix_sha256=prefix_hash),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.FORMAT_NOT_BLANK
    assert not plan.mutating_commands


@pytest.mark.parametrize("prefix_hash", [None, "not-a-sha256"])
def test_stock_journal_missing_or_malformed_prefix_hash_conflicts(
    prefix_hash: str | None,
) -> None:
    source = _source_evidence()
    stage_a = plan_stage_a(source, None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert stage_a.journal is not None
    parts = (BOOT, stage_a.journal.target.root, stage_a.journal.target.data)
    mbr = _mbr(parts)
    journal = replace(
        stage_a.journal,
        phase=Phase.ROOT_RESIZED,
        committed_mbr_sha256=hashlib.sha256(mbr).hexdigest(),
        data_prefix_sha256=prefix_hash,
    )
    evidence = replace(
        source,
        boot_id="stock-boot-b",
        partitions=parts,
        mbr=mbr,
        root_filesystem_bytes=TARGET_ROOT_SIZE * SECTOR_SIZE,
        data_partuuid="1234abcd-03",
    )

    plan = plan_stage_b(evidence, journal, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.JOURNAL_CONFLICT
    assert not plan.mutating_commands


def test_stock_journal_hash_round_trip_is_closed_and_required() -> None:
    plan = plan_stage_a(_source_evidence(), None, authorization=EXACT_STOCK_CARD_AUTHORIZATION)
    assert plan.journal is not None
    assert journal_from_json(journal_json(plan.journal).decode()) == plan.journal

    raw = json.loads(journal_json(plan.journal))
    del raw["data_prefix_sha256"]
    with pytest.raises(BootstrapError, match="prefix hash"):
        journal_from_json(json.dumps(raw))


def test_contract_and_runtime_triggers_cannot_be_confused() -> None:
    source = _source_evidence()
    wrong = plan_stage_a(
        replace(source, cmdline=("dashcam.bootstrap=v1",)),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    ambiguous = plan_stage_a(
        replace(
            source,
            cmdline=("dashcam.bootstrap=v1", SSH_DEV_TRIGGER),
        ),
        None,
        authorization=EXACT_STOCK_CARD_AUTHORIZATION,
    )
    assert wrong.journal is not None and wrong.journal.phase is Phase.REFUSED
    assert ambiguous.journal is not None and ambiguous.journal.phase is Phase.REFUSED
    assert wrong.journal.refusal_code == RefusalCode.TRIGGER_MISSING
    assert ambiguous.journal.refusal_code == RefusalCode.TRIGGER_MISSING
    assert not wrong.mutating_commands and not ambiguous.mutating_commands

    confused_contract = _stock_contract()
    confused_contract["bootstrap_trigger"] = "dashcam.bootstrap=v1"
    with pytest.raises(BootstrapError, match="authorized exact trial card"):
        load_bootstrap_contract(json.dumps(confused_contract))


def test_exact_stock_contract_loads_and_target_policy_drift_refuses() -> None:
    authorization, policy = load_bootstrap_contract(json.dumps(_stock_contract()))
    assert authorization == EXACT_STOCK_CARD_AUTHORIZATION
    assert policy.root_target_bytes // SECTOR_SIZE == TARGET_ROOT_SIZE

    drifted = _stock_contract()
    target = drifted["target"]
    assert isinstance(target, dict)
    target["root_size_bytes"] = 7 * 1024**3
    with pytest.raises(BootstrapError, match="geometry differs"):
        load_bootstrap_contract(json.dumps(drifted))
