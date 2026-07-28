from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dashcam.config import default_config
from dashcam.provisioning.bootstrap import (
    AUTHORIZED_SIZE_BYTES,
    COMPLETE_PATH,
    DATA_ZERO_PREFIX_BYTES,
    ENV_PATH,
    EXACT_CARD_AUTHORIZATION,
    STATE_PATH,
    ActionKind,
    BootstrapError,
    CapacityPolicy,
    Evidence,
    Journal,
    Partition,
    Phase,
    PosixRuntime,
    Refusal,
    RefusalCode,
    _identity_mapping,
    _parse_dumpe2fs_header,
    _sentinel_mapping,
    compute_geometry,
    execute_stage_a,
    execute_stage_b,
    journal_from_json,
    journal_json,
    load_bootstrap_contract,
    partition_path,
    plan_stage_a,
    plan_stage_b,
)
from dashcam.storage.preflight import (
    policy_from_identity,
    recording_root_facts_from_mapping,
    storage_identity_from_env,
)

ROOT_START = 1_064_960
SOURCE_ROOT_SIZE = 8_388_608
BOOT = Partition(1, 16_384, 1_048_576, 0x0C)
SOURCE_ROOT = Partition(2, ROOT_START, SOURCE_ROOT_SIZE, 0x83)


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


def _source_evidence(disk: str = "/dev/mmcblk7") -> Evidence:
    parts = (BOOT, SOURCE_ROOT)
    return Evidence(
        cmdline=("console=serial0,115200", "dashcam.bootstrap=v1"),
        boot_id="boot-a",
        root_partition=partition_path(disk, 2),
        disk=disk,
        cid=EXACT_CARD_AUTHORIZATION.cid,
        size_bytes=AUTHORIZED_SIZE_BYTES,
        sector_size=512,
        mbr=_mbr(parts),
        partitions=parts,
        root_filesystem_bytes=SOURCE_ROOT_SIZE * 512,
        root_filesystem="ext4",
        root_uuid="ROOT-1234",
        root_partuuid="4f2c9ea0-02",
        boot_partition=partition_path(disk, 1),
        boot_mounted_source=partition_path(disk, 1),
        boot_filesystem="vfat",
        boot_uuid="BOOT-1234",
        boot_partuuid="4f2c9ea0-01",
        cloud_init_status="done",
    )


def _intent(disk: str = "/dev/mmcblk7") -> Journal:
    plan = plan_stage_a(_source_evidence(disk), None)
    assert plan.journal is not None
    return plan.journal


def _target_evidence(
    *,
    phase: Phase = Phase.TABLE_COMMITTED,
    filesystem: str | None = None,
    label: str | None = None,
    uuid: str | None = None,
    signatures: tuple[str, ...] = (),
    disk: str = "/dev/mmcblk7",
) -> tuple[Evidence, Journal]:
    journal = replace(_intent(disk), phase=phase)
    parts = (BOOT, journal.target.root, journal.target.data)
    target_mbr = _mbr(parts)
    journal = replace(journal, committed_mbr_sha256=hashlib.sha256(target_mbr).hexdigest())
    observed_signatures = (
        ("exfat", "dos") if filesystem == "exfat" and not signatures else signatures
    )
    evidence = replace(
        _source_evidence(disk),
        boot_id="boot-b",
        mbr=target_mbr,
        partitions=parts,
        data_filesystem=filesystem,
        data_label=label,
        data_uuid=uuid,
        data_signatures=observed_signatures,
        data_zero_prefix_bytes=(
            DATA_ZERO_PREFIX_BYTES if filesystem is None and not observed_signatures else 0
        ),
        data_partuuid="4f2c9ea0-03",
    )
    return evidence, journal


def test_geometry_supports_declared_32_and_64_gb_capacity_classes() -> None:
    policy = CapacityPolicy()
    geometry_32 = compute_geometry(
        size_bytes=AUTHORIZED_SIZE_BYTES,
        sector_size=512,
        root_start_sector=ROOT_START,
        policy=policy,
    )
    geometry_64 = compute_geometry(
        size_bytes=64_000_000_000,
        sector_size=512,
        root_start_sector=ROOT_START,
        policy=policy,
    )

    assert geometry_32.root.size_sectors * 512 == 6 * 1024**3
    assert geometry_64.root == geometry_32.root
    assert geometry_64.data.size_sectors > geometry_32.data.size_sectors
    assert geometry_32.data.start_sector % 2048 == 0
    assert geometry_32.data.end_sector < geometry_32.total_sectors


def test_geometry_refuses_undersized_card() -> None:
    with pytest.raises(Refusal) as caught:
        compute_geometry(
            size_bytes=27 * 1024**3,
            sector_size=512,
            root_start_sector=ROOT_START,
            policy=CapacityPolicy(),
        )
    assert caught.value.code is RefusalCode.UNDERSIZED_MEDIA


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        ("/dev/mmcblk12", "/dev/mmcblk12p3"),
        ("/dev/nvme0n1", "/dev/nvme0n1p3"),
        ("/dev/sdz", "/dev/sdz3"),
    ],
)
def test_partition_paths_do_not_hardcode_mmcblk0(disk: str, expected: str) -> None:
    assert partition_path(disk, 3) == expected
    plan = plan_stage_a(_source_evidence(disk), None)
    write = next(action for action in plan.actions if action.argv[:1] == ("/usr/sbin/sfdisk",))
    assert write.argv == ("/usr/sbin/sfdisk", "--no-reread", "--force", disk)
    assert expected in (write.stdin or "")
    assert "label-id: 0x4f2c9ea0" in (write.stdin or "")


def test_imager_first_run_defers_without_journal_or_mutation() -> None:
    evidence = replace(
        _source_evidence(),
        cmdline=("dashcam.bootstrap=v1", "systemd.run=/boot/firmware/firstrun.sh"),
    )
    plan = plan_stage_a(evidence, None)
    assert [action.kind for action in plan.actions] == [ActionKind.DEFER]
    assert plan.journal is None
    assert not plan.mutating_commands


@pytest.mark.parametrize("status", ["absent", "running", "error", "unknown"])
def test_cloud_init_non_success_defers_without_journal_or_mutation(status: str) -> None:
    evidence = replace(_source_evidence(), cloud_init_status=status)
    stage_a = plan_stage_a(evidence, None)
    stage_b = plan_stage_b(evidence, None)
    assert [action.kind for action in stage_a.actions] == [ActionKind.DEFER]
    assert [action.kind for action in stage_b.actions] == [ActionKind.DEFER]
    assert stage_a.journal is None and stage_b.journal is None
    assert not stage_a.mutating_commands and not stage_b.mutating_commands


def test_cloud_init_terminal_success_allows_stage_a_planning() -> None:
    plan = plan_stage_a(replace(_source_evidence(), cloud_init_status="done"), None)
    assert any(action.argv[:1] == ("/usr/sbin/sfdisk",) for action in plan.actions)


def test_stage_a_has_one_table_write_and_no_reread_commands() -> None:
    plan = plan_stage_a(_source_evidence("/dev/sda"), None)
    writes = [action for action in plan.actions if action.argv[:1] == ("/usr/sbin/sfdisk",)]
    assert len(writes) == 1
    assert writes[0].argv == (
        "/usr/sbin/sfdisk",
        "--no-reread",
        "--force",
        "/dev/sda",
    )
    flattened = " ".join(argument for action in plan.actions for argument in action.argv)
    assert "partprobe" not in flattened
    assert "rereadpt" not in flattened
    assert "blockdev" not in flattened


def test_raw_mbr_torn_readback_is_latched_without_destructive_retry() -> None:
    intent = replace(_intent(), phase=Phase.TABLE_WRITE_STARTED)
    source = _source_evidence()
    torn_parts = (BOOT, intent.target.root)
    torn = replace(source, mbr=_mbr(torn_parts), partitions=torn_parts)

    plan = plan_stage_a(torn, intent)

    assert plan.journal is not None
    assert plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.TORN_TABLE
    assert not plan.mutating_commands


def test_stage_a_target_after_power_cut_reconciles_without_second_sfdisk() -> None:
    intent = replace(_intent(), phase=Phase.TABLE_WRITE_STARTED)
    parts = (BOOT, intent.target.root, intent.target.data)
    evidence = replace(_source_evidence(), mbr=_mbr(parts), partitions=parts)

    plan = plan_stage_a(evidence, intent)

    assert plan.journal is not None
    assert plan.journal.phase is Phase.TABLE_COMMITTED
    assert all(action.argv[:1] != ("/usr/sbin/sfdisk",) for action in plan.actions)
    assert sum(action.kind is ActionKind.REBOOT for action in plan.actions) == 1


def test_stage_b_requires_a_different_boot_id() -> None:
    evidence, journal = _target_evidence()
    evidence = replace(evidence, boot_id=journal.stage_a_boot_id)
    plan = plan_stage_b(evidence, journal)
    assert plan.journal == journal
    assert [action.kind for action in plan.actions] == [ActionKind.DEFER]
    assert not plan.mutating_commands


def test_stage_b_only_runs_online_resize2fs_at_commit_boundary() -> None:
    evidence, journal = _target_evidence()
    plan = plan_stage_b(evidence, journal)
    assert [action.argv for action in plan.mutating_commands] == [
        ("/usr/sbin/resize2fs", evidence.root_partition)
    ]
    flattened = " ".join(argument for action in plan.actions for argument in action.argv)
    assert "e2fsck" not in flattened


def test_stage_b_reconciles_an_already_exact_root_without_resize_retry() -> None:
    evidence, journal = _target_evidence()
    evidence = replace(
        evidence,
        root_filesystem_bytes=journal.target.root.size_sectors * journal.target.sector_size,
    )
    plan = plan_stage_b(evidence, journal)
    assert all(action.argv[:1] != ("/usr/sbin/resize2fs",) for action in plan.actions)
    assert plan.journal is not None and plan.journal.phase is Phase.ROOT_RESIZED


def test_format_requires_durable_intent_and_blank_partition() -> None:
    evidence, journal = _target_evidence(phase=Phase.ROOT_RESIZED)
    plan = plan_stage_b(evidence, journal)
    assert plan.actions[0].kind is ActionKind.WRITE_STATE
    assert plan.actions[1].argv == (
        "/usr/sbin/mkfs.exfat",
        "-n",
        "DASHCAM",
        journal.data_partition,
    )
    assert plan.journal is not None and plan.journal.phase is Phase.FORMAT_INTENT

    foreign = replace(evidence, data_signatures=("ext4",))
    refused = plan_stage_b(foreign, journal)
    assert refused.journal is not None
    assert refused.journal.phase is Phase.REFUSED
    assert not refused.mutating_commands

    unproven = replace(evidence, data_zero_prefix_bytes=0)
    refused_unproven = plan_stage_b(unproven, journal)
    assert refused_unproven.journal is not None
    assert refused_unproven.journal.phase is Phase.REFUSED
    assert not refused_unproven.mutating_commands


@pytest.mark.parametrize(
    ("filesystem", "label"),
    [("ext4", "rootfs"), ("exfat", "FOREIGN"), ("vfat", "DASHCAM")],
)
def test_foreign_filesystem_after_format_intent_latches_no_retry(
    filesystem: str, label: str
) -> None:
    evidence, journal = _target_evidence(
        phase=Phase.FORMAT_INTENT, filesystem=filesystem, label=label, uuid="ABCD-1234"
    )
    plan = plan_stage_b(evidence, journal)
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.FOREIGN_FILESYSTEM
    assert all(action.argv[:1] != ("/usr/sbin/mkfs.exfat",) for action in plan.actions)


def test_exact_intended_exfat_is_reconciled_without_reformat() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.FORMAT_INTENT,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    plan = plan_stage_b(evidence, journal)
    assert plan.journal is not None
    assert plan.journal.phase is Phase.DATA_FORMATTED
    assert plan.journal.data_uuid == "ABCD-1234"
    assert not plan.mutating_commands


@pytest.mark.parametrize(
    "signatures",
    (
        ("exfat",),
        ("dos",),
        ("exfat", "ext4"),
        ("exfat", "dos", "ext4"),
        ("exfat", "dos", "dos"),
    ),
)
def test_format_intent_rejects_any_non_exact_wipefs_signature_shape(
    signatures: tuple[str, ...],
) -> None:
    evidence, journal = _target_evidence(
        phase=Phase.FORMAT_INTENT,
        filesystem="exfat",
        label="DASHCAM",
        uuid="7EED-3EA7",
        signatures=signatures,
    )

    plan = plan_stage_b(evidence, journal)

    assert plan.journal is not None
    assert plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.FOREIGN_FILESYSTEM
    assert not plan.mutating_commands


def test_live_exfat_signature_pair_reconciles_only_after_format_intent() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.FORMAT_INTENT,
        filesystem="exfat",
        label="DASHCAM",
        uuid="7EED-3EA7",
        signatures=("exfat", "dos"),
    )

    reconciled = plan_stage_b(evidence, journal)

    assert reconciled.journal is not None
    assert reconciled.journal.phase is Phase.DATA_FORMATTED
    assert reconciled.journal.data_uuid == "7EED-3EA7"
    assert not reconciled.mutating_commands

    pre_format = replace(journal, phase=Phase.ROOT_RESIZED)
    refused = plan_stage_b(evidence, pre_format)
    assert refused.journal is not None
    assert refused.journal.phase is Phase.REFUSED
    assert refused.journal.refusal_code == RefusalCode.FORMAT_NOT_BLANK
    assert not refused.mutating_commands


def test_data_formatted_rejects_an_extra_foreign_signature() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.DATA_FORMATTED,
        filesystem="exfat",
        label="DASHCAM",
        uuid="7EED-3EA7",
        signatures=("exfat", "dos", "ext4"),
    )
    journal = replace(journal, data_uuid="7EED-3EA7")

    refused = plan_stage_b(evidence, journal)

    assert refused.journal is not None
    assert refused.journal.phase is Phase.REFUSED
    assert refused.journal.refusal_code == RefusalCode.FOREIGN_FILESYSTEM
    assert not refused.mutating_commands


def test_latched_refusal_never_retries_even_when_evidence_later_looks_blank() -> None:
    evidence, journal = _target_evidence(phase=Phase.ROOT_RESIZED, signatures=("ext4",))
    refused = plan_stage_b(evidence, journal)
    assert refused.journal is not None and refused.journal.phase is Phase.REFUSED
    blank = replace(evidence, data_signatures=())
    later = plan_stage_b(blank, refused.journal)
    assert [action.kind for action in later.actions] == [ActionKind.NOOP]
    assert not later.mutating_commands


def test_complete_verified_mount_and_sentinel_is_noop() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.COMPLETE,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    evidence = replace(
        evidence,
        root_filesystem_bytes=journal.target.root.size_sectors * journal.target.sector_size,
        mounted_source=journal.data_partition,
        mounted_filesystem="exfat",
        mounted_uuid="ABCD-1234",
    )
    evidence = replace(
        evidence,
        complete_identity=_identity_mapping(journal, evidence),
        sentinel_identity=_sentinel_mapping(journal),
    )
    plan = plan_stage_b(evidence, journal)
    assert [action.kind for action in plan.actions] == [ActionKind.NOOP]


def test_complete_journal_without_completion_marker_is_latched_refusal() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.COMPLETE,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    evidence = replace(
        evidence,
        mounted_source=journal.data_partition,
        mounted_filesystem="exfat",
        mounted_uuid="ABCD-1234",
        sentinel_identity=_sentinel_mapping(journal),
    )

    plan = plan_stage_b(evidence, journal)

    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED
    assert plan.journal.refusal_code == RefusalCode.JOURNAL_CONFLICT
    assert "without the exact completion marker" in (plan.journal.refusal_message or "")
    assert [action.kind for action in plan.actions] == [ActionKind.LATCH]


def test_uuid_only_completion_marker_is_refused() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.COMPLETE,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    evidence = replace(
        evidence,
        complete_identity={"uuid": "ABCD-1234"},
        sentinel_identity={"dashcam_uuid": "ABCD-1234"},
        mounted_source=journal.data_partition,
        mounted_filesystem="exfat",
        mounted_uuid="ABCD-1234",
    )
    plan = plan_stage_b(evidence, journal)
    assert plan.journal is not None and plan.journal.phase is Phase.REFUSED


def test_bootstrap_sentinel_is_the_closed_recorder_preflight_schema() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.CONFIGURED,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    facts = recording_root_facts_from_mapping(
        {
            "mount": {
                "target": "/srv/dashcam",
                "mounted": True,
                "source": journal.data_partition,
                "filesystem": "exfat",
                "label": "DASHCAM",
                "uuid": journal.data_uuid,
                "mount_options": ["rw", "noexec"],
                "device_id": "179:3",
                "os_root_device_id": "179:2",
            },
            "space": {
                "capacity_bytes": journal.target.data.size_sectors * 512,
                "free_bytes": journal.target.data.size_sectors * 256,
            },
            "sentinel": dict(_sentinel_mapping(journal)),
        }
    )
    assert facts.sentinel is not None
    assert facts.sentinel.serial == evidence.cid
    assert facts.sentinel.dashcam_uuid == "ABCD-1234"
    assert facts.sentinel.data_start_sector == journal.target.data.start_sector


def test_journal_round_trip_uses_closed_durable_schema() -> None:
    journal = _intent()
    assert journal_from_json(journal_json(journal).decode()) == journal
    raw = json.loads(journal_json(journal))
    raw["surprise"] = True
    with pytest.raises(Exception, match="closed v1 schema"):
        journal_from_json(json.dumps(raw))


def test_checked_exact_card_contract_loads_and_rejects_drift() -> None:
    root = Path(__file__).resolve().parents[2]
    contract_path = root / "deploy" / "bootstrap" / "storage" / "authorized-exact-card-v1.json"
    payload = contract_path.read_text()
    authorization, policy = load_bootstrap_contract(payload)
    assert authorization == EXACT_CARD_AUTHORIZATION
    assert policy == CapacityPolicy()

    raw = json.loads(payload)
    raw["size_bytes"] += 512
    with pytest.raises(Exception, match="authorized exact trial card"):
        load_bootstrap_contract(json.dumps(raw))


class _FakeRuntime:
    def __init__(self, source: Evidence, target_mbr: bytes) -> None:
        self.source = source
        self.target_mbr = target_mbr
        self.writes: list[tuple[str, bytes]] = []
        self.commands: list[tuple[tuple[str, ...], str | None]] = []
        self.directories: list[str] = []
        self.ownerships: list[tuple[str, int, int, int]] = []
        self.synced = 0
        self.target_root_bytes = _intent(source.disk).target.root.size_sectors * 512

    def read_bytes(self, path: str, *, limit: int | None = None) -> bytes:
        assert path == self.source.disk
        assert limit == 512
        return self.target_mbr

    def read_text(self, path: str, *, limit: int = 64 * 1024) -> str:
        if path == "/etc/fstab":
            return "proc /proc proc defaults 0 0\n"
        if path == "/etc/group":
            return "root:x:0:\ndashcam:x:991:\ndashcam-storage:x:992:dashcam\n"
        matches = [payload for written_path, payload in self.writes if written_path == path]
        if matches:
            return matches[-1].decode()
        raise AssertionError(path)

    def exists(self, path: str) -> bool:
        return any(written_path == path for written_path, _payload in self.writes)

    def atomic_write(self, path: str, data: bytes, mode: int = 0o600) -> None:
        self.writes.append((path, data))

    def mkdir(self, path: str, mode: int = 0o750) -> None:
        self.directories.append(path)

    def set_owner(self, path: str, uid: int, gid: int, mode: int) -> None:
        self.ownerships.append((path, uid, gid, mode))

    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        self.commands.append((argv, stdin))
        if argv[0] == "/usr/sbin/sfdisk" and argv[1] == "--dump":
            return "label: dos\n"
        if argv == ("/usr/bin/id", "-u", "dashcam"):
            return "991\n"
        if argv == ("/usr/bin/id", "-u", "root"):
            return "0\n"
        if argv[:3] == ("/usr/sbin/blkid", "-o", "export"):
            return "TYPE=exfat\nLABEL=DASHCAM\nUUID=ABCD-1234\n"
        if argv[:2] == ("/usr/sbin/wipefs", "--json"):
            return json.dumps(
                {
                    "signatures": [
                        {"offset": "0x3", "type": "exfat", "label": "DASHCAM"},
                        {"offset": "0x1fe", "type": "dos"},
                    ]
                }
            )
        if argv[:2] == ("/usr/sbin/dumpe2fs", "-h"):
            return (
                "Filesystem magic number:  0xEF53\n"
                f"Block count:              {self.target_root_bytes // 4096}\n"
                "Block size:               4096\n"
            )
        if argv[:4] == ("/usr/bin/findmnt", "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET"):
            target = argv[4]
            if target == "/boot/firmware":
                source = self.source.boot_mounted_source
                filesystem = "vfat"
                uuid = self.source.boot_uuid
            else:
                source = partition_path(self.source.disk, 3)
                filesystem = "exfat"
                uuid = "ABCD-1234"
            return json.dumps(
                {
                    "filesystems": [
                        {
                            "source": source,
                            "fstype": filesystem,
                            "uuid": uuid,
                            "target": target,
                        }
                    ]
                }
            )
        return ""

    def sync(self) -> None:
        self.synced += 1


def test_executor_persists_write_started_before_one_sfdisk_and_commits_readback() -> None:
    evidence = _source_evidence()
    intent = _intent()
    parts = (BOOT, intent.target.root, intent.target.data)
    runtime = _FakeRuntime(evidence, _mbr(parts))

    result = execute_stage_a(evidence, None, runtime)

    commands = [command for command, _stdin in runtime.commands]
    assert (
        sum(
            command[:1] == ("/usr/sbin/sfdisk",) and "--no-reread" in command
            for command in commands
        )
        == 1
    )
    state_payloads = [
        journal_from_json(payload.decode())
        for path, payload in runtime.writes
        if path == STATE_PATH
    ]
    assert [item.phase for item in state_payloads] == [
        Phase.STAGE_A_INTENT,
        Phase.TABLE_WRITE_STARTED,
        Phase.TABLE_COMMITTED,
    ]
    assert runtime.synced == 1
    assert commands[-1] == ("/usr/bin/systemctl", "reboot")
    assert result.journal is not None and result.journal.phase is Phase.TABLE_COMMITTED


class _FailingRebootRuntime(_FakeRuntime):
    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        result = super().run(argv, stdin=stdin, timeout=timeout)
        if argv == ("/usr/bin/systemctl", "reboot"):
            raise RuntimeError("injected reboot orchestration failure")
        return result


class _ChangedBootMountRuntime(_FakeRuntime):
    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        if (
            argv[:4] == ("/usr/bin/findmnt", "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET")
            and argv[4] == "/boot/firmware"
        ):
            return json.dumps(
                {
                    "filesystems": [
                        {
                            "source": partition_path(self.source.disk, 3),
                            "fstype": "vfat",
                            "uuid": self.source.boot_uuid,
                            "target": "/boot/firmware",
                        }
                    ]
                }
            )
        return super().run(argv, stdin=stdin, timeout=timeout)


def test_stage_a_revalidates_live_boot_mount_before_any_backup_or_table_write() -> None:
    evidence = _source_evidence()
    intent = _intent()
    target_mbr = _mbr((BOOT, intent.target.root, intent.target.data))
    runtime = _ChangedBootMountRuntime(evidence, target_mbr)

    with pytest.raises(Refusal, match="mount changed"):
        execute_stage_a(evidence, None, runtime)

    assert not runtime.writes
    assert all(command[0] != "/usr/sbin/sfdisk" for command, _stdin in runtime.commands)


def test_reboot_failure_preserves_durable_table_commit_without_refusal_latch() -> None:
    evidence = _source_evidence()
    intent = _intent()
    target_mbr = _mbr((BOOT, intent.target.root, intent.target.data))
    runtime = _FailingRebootRuntime(evidence, target_mbr)

    with pytest.raises(RuntimeError, match="reboot orchestration"):
        execute_stage_a(evidence, None, runtime)

    states = [
        journal_from_json(payload.decode())
        for path, payload in runtime.writes
        if path == STATE_PATH
    ]
    assert states[-1].phase is Phase.TABLE_COMMITTED
    assert all(state.phase is not Phase.REFUSED for state in states)


def test_stage_b_executor_completes_bounded_sequence_with_completion_write_last() -> None:
    evidence, journal = _target_evidence()
    runtime = _FakeRuntime(evidence, evidence.mbr)

    result = execute_stage_b(evidence, journal, runtime)

    commands = [command for command, _stdin in runtime.commands]
    assert ("/usr/sbin/resize2fs", evidence.root_partition) in commands
    assert ("/usr/sbin/dumpe2fs", "-h", evidence.root_partition) in commands
    assert all("FSSIZE" not in command for command in commands)
    assert (
        "/usr/sbin/mkfs.exfat",
        "-n",
        "DASHCAM",
        journal.data_partition,
    ) in commands
    assert commands.count(("/usr/sbin/mkfs.exfat", "-n", "DASHCAM", journal.data_partition)) == 1
    assert ("/usr/bin/mount", "/srv/dashcam") in commands
    for required in ("pending", "clips", "protected", "quarantine"):
        assert f"/srv/dashcam/{required}" in runtime.directories
    env_index, env_payload = next(
        (index, payload) for index, (path, payload) in enumerate(runtime.writes) if path == ENV_PATH
    )
    configured_index = next(
        index
        for index, (path, payload) in enumerate(runtime.writes)
        if path == STATE_PATH and journal_from_json(payload.decode()).phase is Phase.CONFIGURED
    )
    assert env_index < configured_index < len(runtime.writes) - 1
    assert runtime.ownerships == [(ENV_PATH, 0, 992, 0o640)]
    identity = storage_identity_from_env(env_payload)
    preflight_policy = policy_from_identity(default_config(), identity)
    assert identity.cid == journal.cid
    assert identity.source_mbr_sha256 == journal.source_mbr_sha256
    assert identity.root_end_sector == journal.target.root.end_sector
    assert identity.data_start_sector == journal.target.data.start_sector
    assert identity.data_end_sector == journal.target.data.end_sector
    assert identity.minimum_capacity_bytes == CapacityPolicy().minimum_data_bytes
    assert preflight_policy.minimum_capacity_bytes == CapacityPolicy().minimum_data_bytes
    fstab_payload = next(payload for path, payload in runtime.writes if path == "/etc/fstab")
    assert b"uid=991,gid=992" in fstab_payload
    assert runtime.writes[-1][0] == COMPLETE_PATH
    assert result.journal is not None and result.journal.phase is Phase.COMPLETE


class _FailingMkfsRuntime(_FakeRuntime):
    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        if argv[:1] == ("/usr/sbin/mkfs.exfat",):
            raise RuntimeError("injected mkfs failure")
        return super().run(argv, stdin=stdin, timeout=timeout)


class _BadResizeObservationRuntime(_FakeRuntime):
    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        if argv[:2] == ("/usr/sbin/dumpe2fs", "-h"):
            return (
                "Filesystem magic number:  0xEF53\n"
                f"Block count:              {self.target_root_bytes // 4096 - 1}\n"
                "Block size:               4096\n"
            )
        return super().run(argv, stdin=stdin, timeout=timeout)


class _ConflictingFstabRuntime(_FakeRuntime):
    def read_text(self, path: str, *, limit: int = 64 * 1024) -> str:
        if path == "/etc/fstab":
            return "UUID=FOREIGN /srv/dashcam exfat defaults 0 0\n"
        return super().read_text(path, limit=limit)


class _FailingOwnershipRuntime(_FakeRuntime):
    def set_owner(self, path: str, uid: int, gid: int, mode: int) -> None:
        raise RuntimeError("injected handoff ownership failure")


def test_resize_exit_zero_without_exact_size_never_advances_root_resized() -> None:
    evidence, journal = _target_evidence()
    runtime = _BadResizeObservationRuntime(evidence, evidence.mbr)

    with pytest.raises(Refusal, match="exact ext4 target size"):
        execute_stage_b(evidence, journal, runtime)

    states = [
        journal_from_json(payload.decode())
        for path, payload in runtime.writes
        if path == STATE_PATH
    ]
    assert states[-1].phase is Phase.REFUSED
    assert all(state.phase is not Phase.ROOT_RESIZED for state in states)


def test_ext4_total_geometry_does_not_use_lsblk_usable_bytes() -> None:
    expected = 6 * 1024**3
    usable_bytes_reported_by_lsblk = 6_304_432_128

    observed = _parse_dumpe2fs_header(
        "Filesystem volume name:   <none>\n"
        "Filesystem magic number:  0xEF53\n"
        "Block count:              1572864\n"
        "Block size:               4096\n"
    )

    assert usable_bytes_reported_by_lsblk != expected
    assert observed == expected


@pytest.mark.parametrize(
    "header",
    (
        "Filesystem magic number:  0xEF53\nBlock count: 1572864\n",
        "Filesystem magic number:  0x1234\nBlock count: 1572864\nBlock size: 4096\n",
        "Filesystem magic number:  0xEF53\nBlock count: nope\nBlock size: 4096\n",
        (
            "Filesystem magic number:  0xEF53\n"
            "Block count: 1572864\n"
            "Block size: 4096\n"
            "Block size: 4096\n"
        ),
    ),
)
def test_ext4_total_geometry_rejects_ambiguous_or_malformed_headers(
    header: str,
) -> None:
    with pytest.raises(BootstrapError, match="dumpe2fs"):
        _parse_dumpe2fs_header(header)


def test_foreign_fstab_owner_is_latched_without_mount_or_overwrite() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.DATA_FORMATTED,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    evidence = replace(
        evidence,
        root_filesystem_bytes=journal.target.root.size_sectors * journal.target.sector_size,
    )
    runtime = _ConflictingFstabRuntime(evidence, evidence.mbr)

    with pytest.raises(Refusal, match="foreign fstab"):
        execute_stage_b(evidence, journal, runtime)

    assert all(command[0] != "/usr/bin/mount" for command, _stdin in runtime.commands)
    assert all(path != "/etc/fstab" for path, _payload in runtime.writes)
    state = journal_from_json(
        next(payload for path, payload in reversed(runtime.writes) if path == STATE_PATH).decode()
    )
    assert state.phase is Phase.REFUSED


def test_handoff_ownership_failure_never_advances_configured_or_completion() -> None:
    evidence, journal = _target_evidence(
        phase=Phase.DATA_FORMATTED,
        filesystem="exfat",
        label="DASHCAM",
        uuid="ABCD-1234",
    )
    journal = replace(journal, data_uuid="ABCD-1234")
    evidence = replace(
        evidence,
        root_filesystem_bytes=journal.target.root.size_sectors * journal.target.sector_size,
    )
    runtime = _FailingOwnershipRuntime(evidence, evidence.mbr)

    with pytest.raises(RuntimeError, match="ownership failure"):
        execute_stage_b(evidence, journal, runtime)

    states = [
        journal_from_json(payload.decode())
        for path, payload in runtime.writes
        if path == STATE_PATH
    ]
    assert states[-1].phase is Phase.REFUSED
    assert all(state.phase is not Phase.CONFIGURED for state in states)
    assert all(path != COMPLETE_PATH for path, _payload in runtime.writes)


def test_stage_b_command_failure_latches_and_does_not_retry_format() -> None:
    evidence, journal = _target_evidence()
    runtime = _FailingMkfsRuntime(evidence, evidence.mbr)

    with pytest.raises(RuntimeError, match="injected mkfs failure"):
        execute_stage_b(evidence, journal, runtime)

    state = journal_from_json(
        next(payload for path, payload in reversed(runtime.writes) if path == STATE_PATH).decode()
    )
    assert state.phase is Phase.REFUSED
    assert state.refusal_code == RefusalCode.EXECUTION_FAILED
    assert sum(command[0] == "/usr/sbin/mkfs.exfat" for command, _stdin in runtime.commands) == 0


def test_units_are_post_root_network_independent_and_before_recorder() -> None:
    root = Path(__file__).resolve().parents[2]
    unit_dir = root / "deploy" / "bootstrap" / "storage"
    stage_a = (unit_dir / "dashcam-bootstrap-stage-a.service").read_text()
    stage_b = (unit_dir / "dashcam-bootstrap-stage-b.service").read_text()
    combined = stage_a + stage_b
    assert "After=local-fs.target" in stage_a
    assert "Before=" in stage_a and "dashcamd.service" in stage_a
    assert "Before=" in stage_b and "dashcamd.service" in stage_b
    assert "network-online.target" not in combined
    assert "NetworkManager" not in combined
    assert "FailureAction" not in combined
    assert "--stage a" in stage_a and "--stage b" in stage_b
    assert "/opt/dashcam/venv/bin/python" in stage_a
    assert "/opt/dashcam/venv/bin/python" in stage_b
    assert "After=local-fs.target cloud-final.service" in stage_a
    assert "Wants=local-fs.target cloud-final.service" in stage_a
    assert "ConditionPathExists=/var/lib/dashcam/provisioning/bootstrap-v1.json" in stage_b
    assert "initramfs" not in combined.lower()


def test_posix_runtime_ownership_operation_is_closed_to_handoff_path(tmp_path: Path) -> None:
    target = tmp_path / "foreign.env"
    target.write_text("foreign")

    with pytest.raises(BootstrapError, match="outside the storage handoff contract"):
        PosixRuntime().set_owner(str(target), 0, 992, 0o640)
    with pytest.raises(BootstrapError, match="outside the storage handoff contract"):
        PosixRuntime().set_owner(ENV_PATH, 0, 992, 0o600)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and fsync semantics")
def test_posix_runtime_rejects_symlink_write_read_and_directory_targets(
    tmp_path: Path,
) -> None:
    runtime = PosixRuntime()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "state.json"
    real_file.write_text("{}")
    file_link = tmp_path / "state-link"
    file_link.symlink_to(real_file)
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(BootstrapError, match=r"symbolic-link|unsafe"):
        runtime.atomic_write(str(file_link), b"replacement")
    with pytest.raises(BootstrapError, match=r"symbolic-link|unsafe"):
        runtime.read_text(str(file_link))
    with pytest.raises(BootstrapError, match=r"symbolic-link|unsafe"):
        runtime.mkdir(str(directory_link / "child"))
    assert real_file.read_text() == "{}"
