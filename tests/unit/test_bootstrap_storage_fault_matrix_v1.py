from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from dashcam.provisioning.bootstrap import (
    AUTHORIZED_SIZE_BYTES,
    COMPLETE_PATH,
    DATA_ZERO_PREFIX_BYTES,
    EXACT_CARD_AUTHORIZATION,
    Action,
    ActionKind,
    Evidence,
    Journal,
    Partition,
    Phase,
    RefusalCode,
    _identity_mapping,
    _sentinel_mapping,
    execute_stage_b,
    partition_path,
    plan_stage_a,
    plan_stage_b,
)

DISK = "/dev/mmcblk7"
ROOT_START = 1_064_960
SOURCE_ROOT_SIZE = 8_388_608
BOOT = Partition(1, 16_384, 1_048_576, 0x0C)
SOURCE_ROOT = Partition(2, ROOT_START, SOURCE_ROOT_SIZE, 0x83)
DATA_UUID = "ABCD-1234"


def _mbr(parts: tuple[Partition, ...], disk_id: int = 0x4F2C9EA0) -> bytes:
    result = bytearray(512)
    result[440:444] = disk_id.to_bytes(4, "little")
    for part in parts:
        offset = 446 + (part.number - 1) * 16
        result[offset] = 0x80 if part.bootable else 0
        result[offset + 4] = part.type_code
        result[offset + 8 : offset + 12] = part.start_sector.to_bytes(4, "little")
        result[offset + 12 : offset + 16] = part.size_sectors.to_bytes(4, "little")
    result[510:512] = b"\x55\xaa"
    return bytes(result)


def _source_evidence() -> Evidence:
    parts = (BOOT, SOURCE_ROOT)
    return Evidence(
        cmdline=("console=serial0,115200", "dashcam.bootstrap=v1"),
        boot_id="boot-a",
        root_partition=partition_path(DISK, 2),
        disk=DISK,
        cid=EXACT_CARD_AUTHORIZATION.cid,
        size_bytes=AUTHORIZED_SIZE_BYTES,
        sector_size=512,
        mbr=_mbr(parts),
        partitions=parts,
        root_filesystem_bytes=SOURCE_ROOT_SIZE * 512,
        root_filesystem="ext4",
        root_uuid="ROOT-1234",
        root_partuuid="4f2c9ea0-02",
        boot_partition=partition_path(DISK, 1),
        boot_mounted_source=partition_path(DISK, 1),
        boot_filesystem="vfat",
        boot_uuid="BOOT-1234",
        boot_partuuid="4f2c9ea0-01",
        cloud_init_status="done",
    )


def _intent() -> Journal:
    planned = plan_stage_a(_source_evidence(), None)
    assert planned.journal is not None
    return planned.journal


def _target_snapshot(
    phase: Phase,
    *,
    root_exact: bool = False,
    formatted: bool = False,
    mounted: bool = False,
    sentinel: bool = False,
    complete: bool = False,
) -> tuple[Evidence, Journal]:
    intent = _intent()
    target_mbr = _mbr((BOOT, intent.target.root, intent.target.data))
    journal = replace(
        intent,
        phase=phase,
        committed_mbr_sha256=hashlib.sha256(target_mbr).hexdigest(),
        data_uuid=DATA_UUID
        if phase in {Phase.DATA_FORMATTED, Phase.CONFIGURED, Phase.COMPLETE}
        else None,
    )
    evidence = replace(
        _source_evidence(),
        boot_id="boot-b",
        mbr=target_mbr,
        partitions=(BOOT, intent.target.root, intent.target.data),
        root_filesystem_bytes=(
            intent.target.root.size_sectors * intent.target.sector_size
            if root_exact
            else SOURCE_ROOT_SIZE * 512
        ),
        data_filesystem="exfat" if formatted else None,
        data_label="DASHCAM" if formatted else None,
        data_uuid=DATA_UUID if formatted else None,
        data_signatures=("exfat",) if formatted else (),
        data_zero_prefix_bytes=0 if formatted else DATA_ZERO_PREFIX_BYTES,
        data_partuuid="4f2c9ea0-03",
        mounted_source=journal.data_partition if mounted else None,
        mounted_filesystem="exfat" if mounted else None,
        mounted_uuid=DATA_UUID if mounted else None,
    )
    if sentinel:
        evidence = replace(evidence, sentinel_identity=_sentinel_mapping(journal))
    if complete:
        evidence = replace(
            evidence,
            complete_identity=_identity_mapping(journal, evidence),
        )
    return evidence, journal


def _destructive_names(actions: tuple[Action, ...]) -> list[str]:
    result: list[str] = []
    for action in actions:
        if action.argv and action.argv[0] in {
            "/usr/sbin/sfdisk",
            "/usr/sbin/resize2fs",
            "/usr/sbin/mkfs.exfat",
        }:
            result.append(action.argv[0])
    return result


@dataclass(frozen=True)
class StageACut:
    name: str
    phase: Phase | None
    target_written: bool
    expected_phase: Phase
    expected_refusal: str | None
    expected_destructive: tuple[str, ...]
    expect_reboot: bool


@pytest.mark.parametrize(
    "case",
    [
        StageACut(
            "before_intent_or_during_backup",
            None,
            False,
            Phase.STAGE_A_INTENT,
            None,
            ("/usr/sbin/sfdisk",),
            True,
        ),
        StageACut(
            "intent_durable_before_write_started",
            Phase.STAGE_A_INTENT,
            False,
            Phase.REFUSED,
            RefusalCode.EXECUTION_FAILED,
            (),
            False,
        ),
        StageACut(
            "write_started_durable_before_sfdisk",
            Phase.TABLE_WRITE_STARTED,
            False,
            Phase.REFUSED,
            RefusalCode.EXECUTION_FAILED,
            (),
            False,
        ),
        StageACut(
            "sfdisk_effect_durable_after_intent",
            Phase.STAGE_A_INTENT,
            True,
            Phase.TABLE_COMMITTED,
            None,
            (),
            True,
        ),
        StageACut(
            "sfdisk_effect_durable_after_write_started",
            Phase.TABLE_WRITE_STARTED,
            True,
            Phase.TABLE_COMMITTED,
            None,
            (),
            True,
        ),
        StageACut(
            "table_commit_durable_before_sync_or_reboot",
            Phase.TABLE_COMMITTED,
            True,
            Phase.TABLE_COMMITTED,
            None,
            (),
            False,
        ),
    ],
    ids=lambda case: case.name,
)
def test_stage_a_interruption_restart_matrix(case: StageACut) -> None:
    source = _source_evidence()
    journal = replace(_intent(), phase=case.phase) if case.phase is not None else None
    if case.target_written:
        assert journal is not None
        evidence = replace(
            source,
            mbr=_mbr((BOOT, journal.target.root, journal.target.data)),
            partitions=(BOOT, journal.target.root, journal.target.data),
        )
    else:
        evidence = source

    restarted = plan_stage_a(evidence, journal)

    assert restarted.journal is not None
    assert restarted.journal.phase is case.expected_phase
    assert restarted.journal.refusal_code == case.expected_refusal
    assert tuple(_destructive_names(restarted.actions)) == case.expected_destructive
    has_reboot = any(action.kind is ActionKind.REBOOT for action in restarted.actions)
    assert has_reboot is case.expect_reboot
    flattened = " ".join(argument for action in restarted.actions for argument in action.argv)
    assert "partprobe" not in flattened
    assert "rereadpt" not in flattened


@dataclass(frozen=True)
class StageBCut:
    name: str
    phase: Phase
    root_exact: bool
    formatted: bool
    mounted: bool
    sentinel: bool
    complete: bool
    expected_phase: Phase
    expected_destructive: tuple[str, ...]
    expected_first_action: ActionKind


@pytest.mark.parametrize(
    "case",
    [
        StageBCut(
            "before_resize",
            Phase.TABLE_COMMITTED,
            False,
            False,
            False,
            False,
            False,
            Phase.ROOT_RESIZED,
            ("/usr/sbin/resize2fs",),
            ActionKind.COMMAND,
        ),
        StageBCut(
            "resize_effect_before_root_resized_state",
            Phase.TABLE_COMMITTED,
            True,
            False,
            False,
            False,
            False,
            Phase.ROOT_RESIZED,
            (),
            ActionKind.WRITE_STATE,
        ),
        StageBCut(
            "root_resized_state_before_format_intent",
            Phase.ROOT_RESIZED,
            True,
            False,
            False,
            False,
            False,
            Phase.FORMAT_INTENT,
            ("/usr/sbin/mkfs.exfat",),
            ActionKind.WRITE_STATE,
        ),
        StageBCut(
            "mkfs_effect_before_data_formatted_state",
            Phase.FORMAT_INTENT,
            True,
            True,
            False,
            False,
            False,
            Phase.DATA_FORMATTED,
            (),
            ActionKind.WRITE_STATE,
        ),
        StageBCut(
            "data_formatted_state_before_configuration",
            Phase.DATA_FORMATTED,
            True,
            True,
            False,
            False,
            False,
            Phase.CONFIGURED,
            (),
            ActionKind.CONFIGURE,
        ),
        StageBCut(
            "configuration_effect_before_configured_state",
            Phase.DATA_FORMATTED,
            True,
            True,
            True,
            True,
            False,
            Phase.CONFIGURED,
            (),
            ActionKind.CONFIGURE,
        ),
        StageBCut(
            "configured_state_before_completion",
            Phase.CONFIGURED,
            True,
            True,
            True,
            True,
            False,
            Phase.COMPLETE,
            (),
            ActionKind.COMPLETE,
        ),
        StageBCut(
            "completion_effect_with_configured_journal",
            Phase.CONFIGURED,
            True,
            True,
            True,
            True,
            True,
            Phase.CONFIGURED,
            (),
            ActionKind.NOOP,
        ),
    ],
    ids=lambda case: case.name,
)
def test_stage_b_interruption_restart_matrix(case: StageBCut) -> None:
    evidence, journal = _target_snapshot(
        case.phase,
        root_exact=case.root_exact,
        formatted=case.formatted,
        mounted=case.mounted,
        sentinel=case.sentinel,
        complete=case.complete,
    )

    restarted = plan_stage_b(evidence, journal)

    assert restarted.journal is not None
    assert restarted.journal.phase is case.expected_phase
    assert restarted.actions[0].kind is case.expected_first_action
    assert tuple(_destructive_names(restarted.actions)) == case.expected_destructive
    if "/usr/sbin/mkfs.exfat" in case.expected_destructive:
        assert restarted.actions[0].kind is ActionKind.WRITE_STATE
        assert restarted.actions[1].argv[0] == "/usr/sbin/mkfs.exfat"


def test_format_intent_without_a_format_effect_refuses_instead_of_reformatting() -> None:
    evidence, journal = _target_snapshot(
        Phase.FORMAT_INTENT,
        root_exact=True,
        formatted=False,
    )

    restarted = plan_stage_b(evidence, journal)

    assert restarted.journal is not None
    assert restarted.journal.phase is Phase.REFUSED
    assert restarted.journal.refusal_code == RefusalCode.FOREIGN_FILESYSTEM
    assert _destructive_names(restarted.actions) == []


def _drift_table(evidence: Evidence) -> Evidence:
    return replace(evidence, partitions=(BOOT, SOURCE_ROOT))


def _add_foreign_signature(evidence: Evidence) -> Evidence:
    return replace(evidence, data_signatures=("ext4",), data_zero_prefix_bytes=0)


def _change_format_label(evidence: Evidence) -> Evidence:
    return replace(
        evidence,
        data_filesystem="exfat",
        data_label="FOREIGN",
        data_uuid=DATA_UUID,
        data_signatures=("exfat",),
        data_zero_prefix_bytes=0,
    )


def _change_data_uuid(evidence: Evidence) -> Evidence:
    return replace(evidence, data_uuid="FOREIGN-UUID")


def _remove_sentinel(evidence: Evidence) -> Evidence:
    return replace(evidence, sentinel_identity=None)


@pytest.mark.parametrize(
    ("phase", "mutation", "expected_code"),
    [
        (
            Phase.TABLE_COMMITTED,
            _drift_table,
            RefusalCode.TORN_TABLE,
        ),
        (
            Phase.ROOT_RESIZED,
            _add_foreign_signature,
            RefusalCode.FORMAT_NOT_BLANK,
        ),
        (
            Phase.FORMAT_INTENT,
            _change_format_label,
            RefusalCode.FOREIGN_FILESYSTEM,
        ),
        (
            Phase.DATA_FORMATTED,
            _change_data_uuid,
            RefusalCode.FOREIGN_FILESYSTEM,
        ),
        (
            Phase.CONFIGURED,
            _remove_sentinel,
            RefusalCode.JOURNAL_CONFLICT,
        ),
    ],
)
def test_conflicting_restart_state_latches_and_never_becomes_destructive(
    phase: Phase,
    mutation: Callable[[Evidence], Evidence],
    expected_code: RefusalCode,
) -> None:
    formatted = phase in {Phase.FORMAT_INTENT, Phase.DATA_FORMATTED, Phase.CONFIGURED}
    mounted = phase is Phase.CONFIGURED
    evidence, journal = _target_snapshot(
        phase,
        root_exact=True,
        formatted=formatted,
        mounted=mounted,
        sentinel=mounted,
    )
    evidence = mutation(evidence)

    refused = plan_stage_b(evidence, journal)

    assert refused.journal is not None
    assert refused.journal.phase is Phase.REFUSED
    assert refused.journal.refusal_code == expected_code
    assert _destructive_names(refused.actions) == []

    repaired_evidence, _ = _target_snapshot(
        Phase.CONFIGURED,
        root_exact=True,
        formatted=True,
        mounted=True,
        sentinel=True,
    )
    later = plan_stage_b(repaired_evidence, refused.journal)
    assert [action.kind for action in later.actions] == [ActionKind.NOOP]
    assert _destructive_names(later.actions) == []


def test_complete_journal_without_exact_completion_marker_must_refuse() -> None:
    evidence, journal = _target_snapshot(
        Phase.COMPLETE,
        root_exact=True,
        formatted=True,
        mounted=True,
        sentinel=True,
        complete=False,
    )

    restarted = plan_stage_b(evidence, journal)

    assert restarted.journal is not None
    assert restarted.journal.phase is Phase.REFUSED
    assert restarted.journal.refusal_code == RefusalCode.JOURNAL_CONFLICT
    assert _destructive_names(restarted.actions) == []


class _PowerCut(BaseException):
    pass


class _CompletionRuntime:
    def __init__(self, *, cut: str | None = None) -> None:
        self.cut = cut
        self.writes: list[tuple[str, bytes]] = []
        self.synced = 0

    def read_bytes(self, path: str, *, limit: int | None = None) -> bytes:
        raise AssertionError((path, limit))

    def read_text(self, path: str, *, limit: int = 64 * 1024) -> str:
        raise AssertionError((path, limit))

    def exists(self, path: str) -> bool:
        return any(written_path == path for written_path, _ in self.writes)

    def atomic_write(self, path: str, data: bytes, mode: int = 0o600) -> None:
        if self.cut == "before":
            raise _PowerCut
        self.writes.append((path, data))
        if self.cut == "after":
            raise _PowerCut

    def mkdir(self, path: str, mode: int = 0o750) -> None:
        raise AssertionError((path, mode))

    def set_owner(self, path: str, uid: int, gid: int, mode: int) -> None:
        raise AssertionError((path, uid, gid, mode))

    def run(
        self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30
    ) -> str:
        raise AssertionError((argv, stdin, timeout))

    def sync(self) -> None:
        self.synced += 1


@pytest.mark.parametrize("cut", ["before", "after"])
def test_completion_marker_is_the_last_and_only_write_across_a_power_cut(cut: str) -> None:
    evidence, journal = _target_snapshot(
        Phase.CONFIGURED,
        root_exact=True,
        formatted=True,
        mounted=True,
        sentinel=True,
    )
    runtime = _CompletionRuntime(cut=cut)

    with pytest.raises(_PowerCut):
        execute_stage_b(evidence, journal, runtime)

    if cut == "before":
        assert runtime.writes == []
        retry = _CompletionRuntime()
        result = execute_stage_b(evidence, journal, retry)
        assert [path for path, _ in retry.writes] == [COMPLETE_PATH]
        assert retry.synced == 1
        assert result.journal is not None and result.journal.phase is Phase.COMPLETE
    else:
        assert [path for path, _ in runtime.writes] == [COMPLETE_PATH]
        persisted = replace(
            evidence,
            complete_identity=_identity_mapping(replace(journal, phase=Phase.COMPLETE), evidence),
        )
        restarted = plan_stage_b(persisted, journal)
        assert [action.kind for action in restarted.actions] == [ActionKind.NOOP]
        assert _destructive_names(restarted.actions) == []
