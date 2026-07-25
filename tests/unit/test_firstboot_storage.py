from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from dashcam.provisioning.firstboot import (
    MAX_REBOOTS,
    MAX_RETRIES,
    ActionKind,
    CommandResult,
    FirstbootRefused,
    RefusalCode,
    RuntimeAction,
    RuntimeEvidence,
    RuntimeJournal,
    RuntimePhase,
    RuntimePlan,
    RuntimeStage,
    apply_observed_success,
    execute_plan,
    plan_next,
    record_failure,
    start_journal,
)
from dashcam.provisioning.layout import (
    DeviceObservation,
    LayoutSpec,
    PartitionObservation,
    compute_layout,
    fingerprint_partition_table,
    load_layout_toml,
    mbr_partuuid,
    observation_from_mapping,
)

ROOT = Path(__file__).parents[2]
PROVISIONING_FIXTURES = ROOT / "tests" / "fixtures" / "provisioning"
FIRSTBOOT_FIXTURES = ROOT / "tests" / "fixtures" / "firstboot"
BACKUP_DIGEST = "a" * 64
DATA_UUID = "D45C-AB12"
UID = 990
GID = 991


def _spec() -> LayoutSpec:
    return load_layout_toml((ROOT / "deploy" / "storage" / "layout-v1.toml").read_bytes())


def _source_observation() -> DeviceObservation:
    raw = json.loads((PROVISIONING_FIXTURES / "source-ready.json").read_bytes())
    observed = observation_from_mapping(cast(dict[str, object], raw))
    return replace(observed, is_system_disk=True, is_root_disk=True)


def _fingerprinted(
    observed: DeviceObservation,
    partitions: tuple[PartitionObservation, ...],
) -> DeviceObservation:
    assert observed.mbr_disk_id is not None
    fingerprint = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id=observed.mbr_disk_id,
        sector_size_bytes=observed.sector_size_bytes,
        partitions=partitions,
    )
    return replace(
        observed,
        identity=replace(observed.identity, partition_table_fingerprint=fingerprint),
        partitions=partitions,
    )


def _table_observation(source: DeviceObservation) -> DeviceObservation:
    spec = _spec()
    computed = compute_layout(spec, source)
    boot, root = source.partitions
    assert source.mbr_disk_id is not None
    target_root = replace(root, end_sector=computed.root_end_sector)
    data = PartitionObservation(
        number=3,
        start_sector=computed.data_start_sector,
        end_sector=computed.data_end_sector,
        filesystem=None,
        label=None,
        uuid=None,
        partition_type=spec.data.partition_type,
        bootable=spec.data.bootable,
        partuuid=mbr_partuuid(source.mbr_disk_id, 3),
    )
    return _fingerprinted(source, (boot, target_root, data))


def _formatted_observation(source: DeviceObservation) -> DeviceObservation:
    table = _table_observation(source)
    data = replace(
        table.partitions[2],
        filesystem="exfat",
        label="DASHCAM",
        uuid=DATA_UUID,
    )
    return _fingerprinted(table, (table.partitions[0], table.partitions[1], data))


def _evidence(
    observed: DeviceObservation,
    *,
    root_size: int | None = None,
    backup: bool = False,
    kernel_adopted: bool = False,
    root_checked: bool = False,
    signature_clean: bool = False,
    mounted: bool = False,
) -> RuntimeEvidence:
    assert observed.mbr_disk_id is not None
    root = next(partition for partition in observed.partitions if partition.number == 2)
    root_partuuid = mbr_partuuid(observed.mbr_disk_id, 2)
    data_path = f"{observed.identity.resolved_path}p3"
    options = (
        "noatime",
        "nosuid",
        "nodev",
        "noexec",
        f"uid={UID}",
        f"gid={GID}",
        "umask=0007",
    )
    return RuntimeEvidence(
        observation=observed,
        cmdline_tokens=(
            "console=serial0,115200",
            f"root=PARTUUID={root_partuuid}",
            "dashcam.bounded_provision=v1",
        ),
        partuuid_devices={
            root_partuuid: f"{observed.identity.resolved_path}p2",
        },
        fstab_root_partuuid=root_partuuid,
        fstab_boot_partuuid=mbr_partuuid(observed.mbr_disk_id, 1),
        root_filesystem_size_bytes=(root.size_sectors * 512 if root_size is None else root_size),
        backup_sha256=BACKUP_DIGEST if backup else None,
        backup_validated=backup,
        kernel_table_adopted=kernel_adopted,
        root_check_passed=root_checked,
        signature_scan_clean=signature_clean,
        signature_scan_device=data_path if signature_clean else None,
        signature_scan_table_fingerprint=(
            observed.identity.partition_table_fingerprint if signature_clean else None
        ),
        mounted_data_uuid=DATA_UUID if mounted else None,
        mounted_data_fstype="exfat" if mounted else None,
        mounted_data_source=data_path if mounted else None,
        mounted_data_options=options if mounted else (),
        dashcam_uid=UID,
        dashcam_storage_gid=GID,
    )


def _source_state() -> tuple[LayoutSpec, RuntimeEvidence, RuntimeJournal]:
    spec = _spec()
    evidence = _evidence(_source_observation())
    journal = start_journal(spec, evidence, evidence.observation.identity)
    return spec, evidence, journal


def _advance_all() -> list[tuple[RuntimeEvidence, RuntimeJournal, RuntimePlan]]:
    spec, evidence, journal = _source_state()
    snapshots: list[tuple[RuntimeEvidence, RuntimeJournal, RuntimePlan]] = []

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    evidence = replace(evidence, backup_sha256=BACKUP_DIGEST, backup_validated=True)
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    source_root_bytes = evidence.root_filesystem_size_bytes
    evidence = _evidence(
        _table_observation(_source_observation()),
        root_size=source_root_bytes,
        backup=True,
        kernel_adopted=True,
    )
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    evidence = replace(evidence, root_check_passed=True)
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    target_root_bytes = spec.root_target_sectors * 512
    evidence = replace(evidence, root_filesystem_size_bytes=target_root_bytes)
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    evidence = replace(
        evidence,
        signature_scan_clean=True,
        signature_scan_device="/dev/mmcblk9p3",
        signature_scan_table_fingerprint=(
            evidence.observation.identity.partition_table_fingerprint
        ),
    )
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    evidence = _evidence(
        _formatted_observation(_source_observation()),
        root_size=target_root_bytes,
        backup=True,
        kernel_adopted=True,
        root_checked=True,
    )
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    snapshots.append((evidence, journal, plan))
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.POST_ROOT, evidence, journal)
    snapshots.append((evidence, journal, plan))
    evidence = replace(
        _evidence(
            evidence.observation,
            root_size=target_root_bytes,
            backup=True,
            kernel_adopted=True,
            root_checked=True,
            mounted=True,
        )
    )
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.POST_ROOT, evidence, journal)
    snapshots.append((evidence, journal, plan))
    source = journal.source_identity
    sentinel_observation = replace(
        evidence.observation,
        volume_sentinel_layout_version=1,
        volume_sentinel_serial=source.serial,
        volume_sentinel_uuid=DATA_UUID,
        volume_sentinel_source_table_fingerprint=source.partition_table_fingerprint,
    )
    evidence = replace(evidence, observation=sentinel_observation)
    journal = apply_observed_success(spec, plan, evidence)

    plan = plan_next(spec, RuntimeStage.POST_ROOT, evidence, journal)
    snapshots.append((evidence, journal, plan))
    completed_observation = replace(
        sentinel_observation,
        state_marker_layout_version=1,
        state_marker_serial=source.serial,
        state_marker_uuid=DATA_UUID,
        state_marker_source_table_fingerprint=source.partition_table_fingerprint,
    )
    evidence = replace(evidence, observation=completed_observation)
    journal = apply_observed_success(spec, plan, evidence)
    complete = plan_next(spec, RuntimeStage.POST_ROOT, evidence, journal)
    snapshots.append((evidence, journal, complete))
    return snapshots


def test_two_stage_transaction_is_exact_bounded_and_idempotent() -> None:
    snapshots = _advance_all()
    transitions = [(item[1].phase, item[2].next_phase) for item in snapshots]
    assert transitions == [
        (RuntimePhase.SOURCE_VERIFIED, RuntimePhase.BACKUP_VALIDATED),
        (RuntimePhase.BACKUP_VALIDATED, RuntimePhase.TABLE_COMMITTED),
        (RuntimePhase.TABLE_COMMITTED, RuntimePhase.ROOT_CHECKED),
        (RuntimePhase.ROOT_CHECKED, RuntimePhase.ROOT_READY),
        (RuntimePhase.ROOT_READY, RuntimePhase.SIGNATURE_VERIFIED),
        (RuntimePhase.SIGNATURE_VERIFIED, RuntimePhase.DATA_FORMATTED),
        (RuntimePhase.DATA_FORMATTED, RuntimePhase.EARLY_COMPLETE),
        (RuntimePhase.EARLY_COMPLETE, RuntimePhase.VOLUME_MOUNTED),
        (RuntimePhase.VOLUME_MOUNTED, RuntimePhase.SENTINEL_DURABLE),
        (RuntimePhase.SENTINEL_DURABLE, RuntimePhase.COMPLETE),
        (RuntimePhase.COMPLETE, RuntimePhase.COMPLETE),
    ]
    assert snapshots[-1][2].complete
    all_actions = [action for _, _, plan in snapshots for action in plan.actions]
    assert len(all_actions) < 32
    for action in all_actions:
        if action.kind in {ActionKind.COMMAND, ActionKind.DURABLE_COMMAND_OUTPUT}:
            assert action.argv[0].startswith(("/usr/bin/", "/usr/sbin/"))
            assert not action.argv[0].startswith("/tmp/")
    table = snapshots[1][2]
    write = table.actions[0]
    assert write.argv == ("/usr/sbin/sfdisk", "--no-reread", "--force", "/dev/mmcblk9")
    assert write.stdin_text is not None
    assert "label-id: 0x4f2c9ea0" in write.stdin_text
    assert "size=12582912, type=83" in write.stdin_text
    assert "start=13647872, size=47790080, type=07" in write.stdin_text
    mount = snapshots[7][2].actions[1]
    assert mount.argv[-3:] == (
        "noatime,nosuid,nodev,noexec,uid=990,gid=991,umask=0007",
        f"UUID={DATA_UUID}",
        "/srv/dashcam",
    )


def test_signature_scan_is_a_separate_observed_phase_before_format() -> None:
    snapshots = _advance_all()
    scan = snapshots[4][2]
    formatting = snapshots[5][2]
    assert [action.argv[0] for action in scan.actions] == ["/usr/sbin/wipefs"]
    assert scan.next_phase is RuntimePhase.SIGNATURE_VERIFIED
    assert [action.argv[0] for action in formatting.actions] == [
        "/usr/sbin/mkfs.exfat",
        "/usr/sbin/blkid",
    ]


def test_table_phase_rechecks_only_the_persistent_table_write() -> None:
    snapshots = _advance_all()
    table_plan = snapshots[1][2]

    assert [action.argv[0] for action in table_plan.actions if action.mutates_storage] == [
        "/usr/sbin/sfdisk"
    ]
    assert table_plan.actions[-1].argv == (
        "/usr/sbin/blockdev",
        "--rereadpt",
        "/dev/mmcblk9",
    )


def test_source_root_size_must_match_the_exact_selected_image() -> None:
    spec, evidence, _ = _source_state()
    root = evidence.observation.partitions[1]
    wrong_root = replace(root, end_sector=root.end_sector - 2048)
    wrong_observation = _fingerprinted(
        evidence.observation,
        (evidence.observation.partitions[0], wrong_root),
    )
    wrong_evidence = replace(
        evidence,
        observation=wrong_observation,
        root_filesystem_size_bytes=wrong_root.size_sectors * 512,
    )

    with pytest.raises(FirstbootRefused) as caught:
        start_journal(spec, wrong_evidence, wrong_observation.identity)

    assert caught.value.code is RefusalCode.IDENTITY_CHANGED


def test_fault_after_every_phase_retries_the_same_bounded_transition() -> None:
    declared = json.loads((FIRSTBOOT_FIXTURES / "phase-faults.json").read_bytes())
    recoverable = set(cast(list[str], declared["recoverable_phases"]))
    snapshots = _advance_all()[:-1]
    assert {journal.phase.value for _, journal, _ in snapshots} == recoverable
    spec = _spec()
    for evidence, journal, plan in snapshots:
        failed = record_failure(journal, f"fault after {journal.phase.value}")
        retried = plan_next(spec, plan.stage, evidence, failed)
        assert retried.phase is plan.phase
        assert retried.next_phase is plan.next_phase
        assert retried.actions == plan.actions


def test_reconciles_table_root_format_mount_sentinel_and_marker_after_lost_journal_write() -> None:
    snapshots = _advance_all()
    spec = _spec()
    by_phase = {journal.phase: (evidence, journal, plan) for evidence, journal, plan in snapshots}

    source_journal = by_phase[RuntimePhase.BACKUP_VALIDATED][1]
    table_evidence = by_phase[RuntimePhase.TABLE_COMMITTED][0]
    assert (
        plan_next(spec, RuntimeStage.INITRAMFS, table_evidence, source_journal).phase
        is RuntimePhase.TABLE_COMMITTED
    )

    table_journal = by_phase[RuntimePhase.TABLE_COMMITTED][1]
    root_evidence = by_phase[RuntimePhase.ROOT_READY][0]
    assert (
        plan_next(spec, RuntimeStage.INITRAMFS, root_evidence, table_journal).phase
        is RuntimePhase.ROOT_READY
    )

    root_journal = by_phase[RuntimePhase.ROOT_READY][1]
    formatted_evidence = by_phase[RuntimePhase.DATA_FORMATTED][0]
    assert (
        plan_next(spec, RuntimeStage.INITRAMFS, formatted_evidence, root_journal).phase
        is RuntimePhase.DATA_FORMATTED
    )

    early_journal = by_phase[RuntimePhase.EARLY_COMPLETE][1]
    mounted_evidence = by_phase[RuntimePhase.VOLUME_MOUNTED][0]
    assert (
        plan_next(spec, RuntimeStage.POST_ROOT, mounted_evidence, early_journal).phase
        is RuntimePhase.VOLUME_MOUNTED
    )

    sentinel_evidence = by_phase[RuntimePhase.SENTINEL_DURABLE][0]
    assert (
        plan_next(spec, RuntimeStage.POST_ROOT, sentinel_evidence, early_journal).phase
        is RuntimePhase.SENTINEL_DURABLE
    )

    complete_evidence = by_phase[RuntimePhase.COMPLETE][0]
    complete = plan_next(spec, RuntimeStage.POST_ROOT, complete_evidence, early_journal)
    assert complete.complete
    assert complete.phase is RuntimePhase.COMPLETE


@pytest.mark.parametrize("field", ["serial", "size", "mbr", "table"])
def test_identity_change_is_refused_before_next_phase(field: str) -> None:
    spec, evidence, journal = _source_state()
    observed = evidence.observation
    if field == "serial":
        changed = replace(observed, identity=replace(observed.identity, serial="other-card"))
    elif field == "size":
        changed = replace(
            observed,
            identity=replace(observed.identity, size_bytes=observed.identity.size_bytes + 512),
            total_sectors=observed.total_sectors + 1,
        )
    elif field == "mbr":
        changed = replace(observed, mbr_disk_id="0x12345678")
    else:
        changed = replace(
            observed,
            identity=replace(observed.identity, partition_table_fingerprint="b" * 64),
        )
    with pytest.raises(FirstbootRefused) as caught:
        plan_next(spec, RuntimeStage.INITRAMFS, replace(evidence, observation=changed), journal)
    assert caught.value.code is RefusalCode.IDENTITY_CHANGED


def test_root_partuuid_must_resolve_to_the_observed_boot_disk() -> None:
    spec, evidence, journal = _source_state()
    wrong = replace(evidence, partuuid_devices={journal.root_partuuid: "/dev/mmcblk0p2"})
    with pytest.raises(FirstbootRefused) as caught:
        plan_next(spec, RuntimeStage.INITRAMFS, wrong, journal)
    assert caught.value.code is RefusalCode.ROOT_RESOLUTION_FAILED


def test_existing_signature_and_existing_p3_are_refused_for_new_transaction() -> None:
    spec, evidence, _ = _source_state()
    signature = replace(evidence, data_region_signatures=("ext4",))
    with pytest.raises(FirstbootRefused) as caught:
        start_journal(spec, signature, signature.observation.identity)
    assert caught.value.code is RefusalCode.EXISTING_SIGNATURE

    table = _evidence(_table_observation(_source_observation()))
    with pytest.raises(FirstbootRefused) as caught:
        start_journal(spec, table, table.observation.identity)
    assert caught.value.code is RefusalCode.LAYOUT_REFUSED


def test_signature_attestation_must_bind_current_table_and_partition() -> None:
    snapshot = next(
        item for item in _advance_all() if item[1].phase is RuntimePhase.SIGNATURE_VERIFIED
    )
    evidence, journal, _ = snapshot
    for changed in (
        replace(evidence, signature_scan_clean=False),
        replace(evidence, signature_scan_device="/dev/mmcblk0p3"),
        replace(evidence, signature_scan_table_fingerprint="b" * 64),
    ):
        with pytest.raises(FirstbootRefused) as caught:
            plan_next(_spec(), RuntimeStage.INITRAMFS, changed, journal)
        assert caught.value.code is RefusalCode.EXISTING_SIGNATURE


def test_marker_inconsistency_is_refused_and_never_treated_as_complete() -> None:
    snapshots = _advance_all()
    evidence, journal, _ = next(
        item for item in snapshots if item[1].phase is RuntimePhase.EARLY_COMPLETE
    )
    bad = replace(
        evidence.observation,
        volume_sentinel_layout_version=1,
        volume_sentinel_serial="replacement-card",
        volume_sentinel_uuid=DATA_UUID,
        volume_sentinel_source_table_fingerprint=journal.source_identity.partition_table_fingerprint,
    )
    with pytest.raises(FirstbootRefused) as caught:
        plan_next(
            _spec(),
            RuntimeStage.POST_ROOT,
            replace(evidence, observation=bad),
            journal,
        )
    assert caught.value.code is RefusalCode.MARKER_INCONSISTENT


def test_no_shrink_even_with_a_matching_card_identity_shape() -> None:
    spec, evidence, _ = _source_state()
    source = evidence.observation
    oversized_root = replace(
        source.partitions[1],
        end_sector=compute_layout(spec, source).root_end_sector + 1,
    )
    oversized = _fingerprinted(source, (source.partitions[0], oversized_root))
    oversized_evidence = replace(
        evidence,
        observation=oversized,
        root_filesystem_size_bytes=oversized_root.size_sectors * 512,
    )
    with pytest.raises(FirstbootRefused) as caught:
        start_journal(spec, oversized_evidence, oversized.identity)
    assert caught.value.code is RefusalCode.SHRINK_FORBIDDEN


def test_retry_and_reboot_are_capped_without_a_loop() -> None:
    spec, evidence, journal = _source_state()
    failed = journal
    for attempt in range(MAX_RETRIES):
        failed = record_failure(failed, f"attempt {attempt}")
    with pytest.raises(FirstbootRefused) as caught:
        plan_next(spec, RuntimeStage.INITRAMFS, evidence, failed)
    assert caught.value.code is RefusalCode.RETRY_LIMIT

    backup_plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    backup_evidence = replace(evidence, backup_sha256=BACKUP_DIGEST, backup_validated=True)
    journal = apply_observed_success(spec, backup_plan, backup_evidence)
    table_plan = plan_next(spec, RuntimeStage.INITRAMFS, backup_evidence, journal)
    table_evidence = _evidence(
        _table_observation(_source_observation()),
        root_size=evidence.root_filesystem_size_bytes,
        backup=True,
        kernel_adopted=False,
    )
    journal = apply_observed_success(spec, table_plan, table_evidence)
    reboot = plan_next(spec, RuntimeStage.INITRAMFS, table_evidence, journal)
    assert reboot.commit_before_actions
    assert reboot.journal.reboot_count == MAX_REBOOTS
    with pytest.raises(FirstbootRefused) as caught:
        plan_next(spec, RuntimeStage.INITRAMFS, table_evidence, reboot.journal)
    assert caught.value.code is RefusalCode.REBOOT_LIMIT


def test_execution_defaults_disabled_and_rechecks_identity_before_mutation() -> None:
    spec, evidence, journal = _source_state()
    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    calls: list[object] = []
    with pytest.raises(FirstbootRefused) as caught:
        execute_plan(plan)
    assert caught.value.code is RefusalCode.EXECUTION_DISABLED

    changed = replace(
        evidence,
        observation=replace(
            evidence.observation,
            identity=replace(evidence.observation.identity, serial="swapped-card"),
        ),
    )

    def successful_executor(action: RuntimeAction) -> CommandResult:
        calls.append(action)
        return CommandResult(0)

    with pytest.raises(FirstbootRefused) as caught:
        execute_plan(
            plan,
            spec=spec,
            executor=successful_executor,
            journal_store=lambda saved: calls.append(saved),
            identity_recheck=lambda: changed,
            execution_enabled=True,
        )
    assert caught.value.code is RefusalCode.IDENTITY_CHANGED
    assert calls == []


def test_executor_rejects_nonzero_result_and_never_uses_shell_strings() -> None:
    spec, evidence, journal = _source_state()
    plan = plan_next(spec, RuntimeStage.INITRAMFS, evidence, journal)
    calls: list[tuple[str, ...]] = []

    def failing_executor(action: RuntimeAction) -> CommandResult:
        calls.append(action.argv)
        return CommandResult(9)

    with pytest.raises(FirstbootRefused) as caught:
        execute_plan(
            plan,
            spec=spec,
            executor=failing_executor,
            journal_store=lambda saved: None,
            identity_recheck=lambda: evidence,
            execution_enabled=True,
        )
    assert caught.value.code is RefusalCode.COMMAND_FAILED
    assert calls == [plan.actions[0].argv]
    assert isinstance(calls[0], tuple)


def test_deploy_candidates_have_explicit_trigger_gate_and_refusal_contract() -> None:
    runtime = ROOT / "deploy" / "image" / "payload" / "runtime"
    initramfs = (runtime / "initramfs" / "dashcam-bounded-provision").read_text()
    post_root = (runtime / "post-root" / "dashcam-firstboot-storage").read_text()
    unit = (runtime / "post-root" / "dashcam-firstboot-storage.service").read_text()
    blockers = json.loads((FIRSTBOOT_FIXTURES / "runtime-blockers.json").read_bytes())
    for candidate in (initramfs, post_root):
        assert "dashcam.bounded_provision=v1" in candidate
        assert "firstboot-runtime-v1.enabled" in candidate
        assert "exit 125" in candidate
        assert "set -efu" in candidate
        assert "/usr/bin/cat /proc/cmdline" in candidate
        assert "$(cat " not in candidate
    assert "/usr/bin/python" not in initramfs
    assert "/usr/sbin/sfdisk --no-reread --force /dev/mmcblk0" in initramfs
    assert "/usr/sbin/resize2fs /dev/mmcblk0p2" in initramfs
    assert "ConditionKernelCommandLine=dashcam.bounded_provision=v1" in unit
    assert "Before=dashcamd.service" in unit
    assert blockers["execution_gate_contents"] == "EXACT_IMAGE_RUNTIME_VALIDATED=v1"
