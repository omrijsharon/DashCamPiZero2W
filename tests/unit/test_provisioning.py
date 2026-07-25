from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from dashcam.provisioning.layout import (
    DeviceIdentity,
    DeviceObservation,
    LayoutSpec,
    LayoutState,
    fingerprint_partition_table,
    load_layout_toml,
    mbr_partuuid,
    observation_from_mapping,
)
from dashcam.provisioning.planner import (
    ActionKind,
    PlannerRefusalCode,
    ProvisioningRefused,
    author_provisioning_plan,
    confirmation_phrase,
)

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "provisioning"


def _spec() -> LayoutSpec:
    return load_layout_toml((ROOT / "deploy" / "storage" / "layout-v1.toml").read_bytes())


def _observation(name: str = "source-ready.json") -> DeviceObservation:
    decoded = json.loads((FIXTURES / name).read_bytes())
    return observation_from_mapping(cast(dict[str, object], decoded))


def _with_mbr_disk_id(observed: DeviceObservation, mbr_disk_id: str) -> DeviceObservation:
    partitions = tuple(
        replace(partition, partuuid=mbr_partuuid(mbr_disk_id, partition.number))
        for partition in observed.partitions
    )
    fingerprint = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id=mbr_disk_id,
        sector_size_bytes=observed.sector_size_bytes,
        partitions=partitions,
    )
    return replace(
        observed,
        identity=replace(observed.identity, partition_table_fingerprint=fingerprint),
        mbr_disk_id=mbr_disk_id,
        partitions=partitions,
    )


def test_dry_run_never_calls_executor_and_orders_backup_before_mutation() -> None:
    observed = _observation()
    calls: list[tuple[str, ...]] = []

    plan = author_provisioning_plan(
        spec=_spec(),
        observations=[observed],
        expected_identity=observed.identity,
        executor=calls.append,
    )

    assert calls == []
    assert plan.dry_run
    assert not plan.execution_supported
    assert plan.actions[0].kind is ActionKind.BACKUP_PARTITION_TABLE
    assert plan.actions[0].argv == ("sfdisk", "--dump", "/dev/mmcblk9")
    assert plan.actions[0].stdout_path is not None
    assert plan.actions[0].stdout_path.endswith(".sfdisk")
    assert plan.actions[1].kind is ActionKind.VALIDATE_PARTITION_TABLE_BACKUP
    first_destructive = next(action for action in plan.actions if action.destructive)
    assert first_destructive.sequence > plan.actions[1].sequence
    assert all(isinstance(action.argv, tuple) for action in plan.actions)
    assert all("sh" not in action.argv[:1] for action in plan.actions)


def test_plan_is_deterministic_and_has_explicit_bounds() -> None:
    observed = _observation()
    one = author_provisioning_plan(
        spec=_spec(), observations=[observed], expected_identity=observed.identity
    )
    two = author_provisioning_plan(
        spec=_spec(), observations=[observed], expected_identity=observed.identity
    )
    assert one.to_dict() == two.to_dict()
    partition_action = next(
        action for action in one.actions if action.kind is ActionKind.WRITE_PARTITION_TABLE
    )
    assert partition_action.argv == ("sfdisk", "--no-reread", "--force", "/dev/mmcblk9")
    mount_action = next(
        action for action in one.actions if action.kind is ActionKind.CONFIGURE_UUID_MOUNT
    )
    assert mount_action.argv[-1] == (
        "noatime,nosuid,nodev,noexec,uid=${DASHCAM_UID},gid=${DASHCAM_STORAGE_GID},umask=0007"
    )
    assert partition_action.stdin_text is not None
    assert "label: dos" in partition_action.stdin_text
    assert "label-id: 0x4f2c9ea0" in partition_action.stdin_text
    assert "/dev/mmcblk9p1 : start=16384, size=1048576, type=0c" in partition_action.stdin_text
    assert "/dev/mmcblk9p2 : start=1064960, size=12582912, type=83" in partition_action.stdin_text
    assert "/dev/mmcblk9p3 : start=13647872, size=47790080, type=07" in partition_action.stdin_text
    serialized = json.dumps(one.to_dict(), sort_keys=True).lower()
    assert "gpt" not in serialized
    assert "guid" not in serialized
    assert "observed_root_partition" not in serialized
    marker = next(action for action in one.actions if action.kind is ActionKind.WRITE_STATE_MARKER)
    marker_payload = json.loads(marker.argv[-1])
    assert marker_payload["mbr_disk_id"] == "0x4f2c9ea0"
    assert marker_payload["boot_partuuid"] == "4f2c9ea0-01"
    assert marker_payload["root_partuuid"] == "4f2c9ea0-02"
    assert marker_payload["data_partuuid"] == "4f2c9ea0-03"
    assert marker_payload["boot_filesystem_uuid"] == "89F4-4546"
    assert marker_payload["root_filesystem_uuid"] == "e9ef4083-101b-46b4-b87d-de84fe1169f8"
    assert "MBR=0x4f2c9ea0" in one.confirmation_phrase


def test_plan_preserves_an_imager_randomized_mbr_id_and_derives_partuuids() -> None:
    observed = _with_mbr_disk_id(_observation(), "0x624667df")
    plan = author_provisioning_plan(
        spec=_spec(),
        observations=[observed],
        expected_identity=observed.identity,
    )
    write = next(
        action for action in plan.actions if action.kind is ActionKind.WRITE_PARTITION_TABLE
    )
    marker = next(action for action in plan.actions if action.kind is ActionKind.WRITE_STATE_MARKER)
    payload = json.loads(marker.argv[-1])
    assert write.stdin_text is not None
    assert "label-id: 0x624667df" in write.stdin_text
    assert payload["mbr_disk_id"] == "0x624667df"
    assert payload["boot_partuuid"] == "624667df-01"
    assert payload["root_partuuid"] == "624667df-02"
    assert payload["data_partuuid"] == "624667df-03"
    assert "MBR=0x624667df" in plan.confirmation_phrase


def test_already_provisioned_layout_plans_no_actions() -> None:
    observed = _observation("provisioned.json")
    plan = author_provisioning_plan(
        spec=_spec(), observations=[observed], expected_identity=observed.identity
    )
    assert plan.verification.state is LayoutState.ALREADY_PROVISIONED
    assert plan.actions == ()


def test_ambiguous_candidates_are_refused() -> None:
    observed = _observation()
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed, observed],
            expected_identity=observed.identity,
        )
    assert caught.value.code is PlannerRefusalCode.AMBIGUOUS_DEVICE


def test_missing_expected_identity_is_refused() -> None:
    observed = _observation()
    other = replace(observed.identity, serial="different-card")
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(spec=_spec(), observations=[observed], expected_identity=other)
    assert caught.value.code is PlannerRefusalCode.IDENTITY_MISMATCH


def test_identity_change_between_observations_is_refused() -> None:
    observed = _observation()
    changed = replace(observed, identity=replace(observed.identity, serial="replacement-card"))
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed],
            expected_identity=observed.identity,
            recheck=changed,
        )
    assert caught.value.code is PlannerRefusalCode.IDENTITY_CHANGED


def test_partition_table_identity_change_between_observations_is_refused() -> None:
    observed = _observation()
    changed = replace(
        observed,
        identity=replace(
            observed.identity,
            partition_table_fingerprint="b" * 64,
        ),
        mbr_disk_id="0x12345678",
    )
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed],
            expected_identity=observed.identity,
            recheck=changed,
        )
    assert caught.value.code is PlannerRefusalCode.IDENTITY_CHANGED


def test_layout_refusal_is_propagated() -> None:
    observed = replace(_observation(), is_system_disk=True)
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed],
            expected_identity=observed.identity,
        )
    assert caught.value.code is PlannerRefusalCode.LAYOUT_REFUSED
    assert "system_disk" in str(caught.value)


def test_non_dry_run_without_exact_confirmation_is_refused_without_execution() -> None:
    observed = _observation()
    calls: list[tuple[str, ...]] = []
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed],
            expected_identity=observed.identity,
            dry_run=False,
            executor=calls.append,
        )
    assert caught.value.code is PlannerRefusalCode.CONFIRMATION_REQUIRED
    assert calls == []


def test_even_confirmed_execution_is_disabled_and_executor_is_not_called() -> None:
    observed = _observation()
    calls: list[tuple[str, ...]] = []
    with pytest.raises(ProvisioningRefused) as caught:
        author_provisioning_plan(
            spec=_spec(),
            observations=[observed],
            expected_identity=observed.identity,
            dry_run=False,
            typed_confirmation=confirmation_phrase(observed.identity, observed.mbr_disk_id),
            executor=calls.append,
        )
    assert caught.value.code is PlannerRefusalCode.EXECUTION_DISABLED
    assert calls == []


def test_confirmation_binds_path_serial_size_and_table_fingerprint() -> None:
    identity = DeviceIdentity(
        "/dev/test0",
        "serial-one",
        32_000_000_000,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    phrase = confirmation_phrase(identity)
    assert identity.resolved_path in phrase
    assert identity.serial in phrase
    assert str(identity.size_bytes) in phrase
    assert identity.partition_table_fingerprint in phrase
