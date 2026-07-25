from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from dashcam.provisioning.layout import (
    DeviceObservation,
    LayoutError,
    LayoutSpec,
    LayoutState,
    PartitionObservation,
    RefusalCode,
    compute_layout,
    fingerprint_partition_table,
    load_layout_toml,
    mbr_partuuid,
    observation_from_mapping,
    verify_layout,
)

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "provisioning"


def _spec() -> LayoutSpec:
    return load_layout_toml((ROOT / "deploy" / "storage" / "layout-v1.toml").read_bytes())


def _observation(name: str = "source-ready.json") -> DeviceObservation:
    decoded = json.loads((FIXTURES / name).read_bytes())
    return observation_from_mapping(cast(dict[str, object], decoded))


def _codes(observed: DeviceObservation) -> set[RefusalCode]:
    return set(verify_layout(_spec(), observed).refusal_codes)


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


def test_declarative_layout_targets_six_gib_root_and_remainder() -> None:
    spec = _spec()
    observed = _observation()
    computed = compute_layout(spec, observed)

    assert spec.root_target_gib == 6.0
    assert spec.table_type == "dos"
    assert computed.root_start_sector == observed.partitions[1].start_sector
    assert observed.partitions[1].end_sector == 5_226_495
    assert computed.root_size_sectors == 6 * 1024**3 // 512
    assert computed.data_start_sector % spec.alignment_sectors == 0
    assert computed.data_end_sector < observed.total_sectors
    assert computed.data_size_sectors >= spec.minimum_data_sectors
    assert computed.root_start_sector == 1_064_960
    assert computed.root_end_sector == 13_647_871
    assert computed.data_start_sector == 13_647_872
    assert computed.data_end_sector == 61_437_951
    assert observed.total_sectors - computed.data_end_sector - 1 == 2_048


def test_selected_image_partition_identities_are_explicit() -> None:
    spec = _spec()
    assert (
        spec.boot.source_start_sector,
        spec.boot.source_size_sectors,
        spec.boot.partition_type,
        spec.boot.bootable,
        spec.boot.filesystem_uuid,
    ) == (16_384, 1_048_576, "0x0c", False, "89F4-4546")
    assert (
        spec.root.source_start_sector,
        spec.root.partition_type,
        spec.root.bootable,
        spec.root.filesystem_uuid,
    ) == (
        1_064_960,
        "0x83",
        False,
        "e9ef4083-101b-46b4-b87d-de84fe1169f8",
    )
    assert (
        spec.data.source_start_sector,
        spec.data.partition_type,
        spec.data.bootable,
    ) == (None, "0x07", False)


def test_source_layout_is_verified_read_only() -> None:
    observed = _observation()
    before = observed
    report = verify_layout(_spec(), observed)

    assert report.state is LayoutState.SOURCE_READY
    assert report.accepted
    assert observed == before


def test_matching_completed_layout_is_idempotently_recognized() -> None:
    report = verify_layout(_spec(), _observation("provisioned.json"))
    assert report.state is LayoutState.ALREADY_PROVISIONED
    assert report.accepted


_PRIMARY_REFUSALS: tuple[
    tuple[Callable[[DeviceObservation], DeviceObservation], RefusalCode], ...
] = (
    (
        lambda observed: replace(observed, device_path_is_resolved=False),
        RefusalCode.UNRESOLVED_DEVICE,
    ),
    (
        lambda observed: replace(observed, is_system_disk=True),
        RefusalCode.SYSTEM_DISK,
    ),
    (
        lambda observed: replace(observed, is_root_disk=True),
        RefusalCode.ROOT_DISK,
    ),
    (
        lambda observed: replace(observed, table_type="gpt"),
        RefusalCode.WRONG_TABLE_TYPE,
    ),
    (
        lambda observed: replace(observed, unpartitioned_data_signatures=("ext4-superblock",)),
        RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
    ),
)


@pytest.mark.parametrize(
    ("change", "code"),
    _PRIMARY_REFUSALS,
)
def test_primary_device_refusals(
    change: Callable[[DeviceObservation], DeviceObservation], code: RefusalCode
) -> None:
    assert code in _codes(change(_observation()))


def test_undersized_media_is_refused() -> None:
    observed = _observation()
    total_sectors = 20 * 1024**3 // observed.sector_size_bytes
    identity = replace(observed.identity, size_bytes=total_sectors * observed.sector_size_bytes)
    resized = replace(observed, identity=identity, total_sectors=total_sectors)
    assert RefusalCode.UNDERSIZED_MEDIA in _codes(resized)


def test_nominal_64gb_card_uses_the_aligned_remainder() -> None:
    observed = _observation()
    total_sectors = 125_000_000
    identity = replace(observed.identity, size_bytes=total_sectors * 512)
    larger = replace(observed, identity=identity, total_sectors=total_sectors)
    computed = compute_layout(_spec(), larger)
    assert computed.root_end_sector == 13_647_871
    assert computed.data_start_sector == 13_647_872
    assert computed.data_end_sector == 124_997_631
    assert total_sectors - computed.data_end_sector - 1 >= 2_048
    assert (computed.data_end_sector + 1) % _spec().alignment_sectors == 0


def test_duplicate_partition_number_is_ambiguous() -> None:
    observed = _observation()
    duplicated = replace(observed, partitions=(*observed.partitions, observed.partitions[-1]))
    assert RefusalCode.AMBIGUOUS_LAYOUT in _codes(duplicated)


def test_unexpected_partition_is_refused() -> None:
    observed = _observation()
    unexpected = PartitionObservation(4, 20_000_000, 21_000_000, None, None, None)
    assert RefusalCode.UNEXPECTED_PARTITION in _codes(
        replace(observed, partitions=(*observed.partitions, unexpected))
    )


def test_overlapping_partition_is_refused() -> None:
    observed = _observation()
    root = replace(observed.partitions[1], start_sector=1_000_000)
    assert RefusalCode.PARTITION_OVERLAP in _codes(
        replace(observed, partitions=(observed.partitions[0], root))
    )


def test_misaligned_root_is_refused() -> None:
    observed = _observation()
    root = replace(observed.partitions[1], start_sector=observed.partitions[1].start_sector + 1)
    assert RefusalCode.MISALIGNED_PARTITION in _codes(
        replace(observed, partitions=(observed.partitions[0], root))
    )


def test_root_larger_than_target_is_never_shrunk() -> None:
    observed = _observation()
    root = replace(
        observed.partitions[1],
        end_sector=observed.partitions[1].start_sector + _spec().root_target_sectors,
    )
    assert RefusalCode.ROOT_ALREADY_TOO_LARGE in _codes(
        replace(observed, partitions=(observed.partitions[0], root))
    )


def test_unexpected_mount_is_refused() -> None:
    observed = _observation()
    root = replace(observed.partitions[1], mount_points=("/mnt/other",))
    assert RefusalCode.UNEXPECTED_MOUNT in _codes(
        replace(observed, partitions=(observed.partitions[0], root))
    )


def test_even_expected_source_mounts_are_refused_for_plan_authoring() -> None:
    observed = _observation()
    boot = replace(observed.partitions[0], mount_points=("/boot/firmware",))
    assert RefusalCode.UNEXPECTED_MOUNT in _codes(
        replace(observed, partitions=(boot, observed.partitions[1]))
    )


def test_existing_partition_three_is_not_treated_as_blank_media() -> None:
    observed = _observation()
    foreign = PartitionObservation(
        3,
        13_639_680,
        62_496_767,
        "ntfs",
        "PHOTOS",
        "FOREIGN-UUID",
        (),
        True,
        "0x07",
        False,
        "624667df-03",
    )
    codes = _codes(replace(observed, partitions=(*observed.partitions, foreign)))
    assert RefusalCode.EXISTING_FILESYSTEM_OR_DATA in codes


def test_changed_or_missing_idempotency_identity_is_refused() -> None:
    observed = replace(_observation("provisioned.json"), volume_sentinel_serial="another-card")
    assert RefusalCode.INVALID_PROVISIONING_MARKER in _codes(observed)


def test_too_little_remaining_space_is_refused() -> None:
    observed = _observation()
    total_sectors = 15_000_000
    identity = replace(observed.identity, size_bytes=total_sectors * 512)
    codes = _codes(replace(observed, total_sectors=total_sectors, identity=identity))
    assert RefusalCode.INSUFFICIENT_DATA_SPACE in codes


def test_partition_table_fingerprint_is_order_independent() -> None:
    observed = _observation()
    first = fingerprint_partition_table(
        table_type="dos",
        mbr_disk_id="0x4f2c9ea0",
        sector_size_bytes=512,
        partitions=observed.partitions,
    )
    second = fingerprint_partition_table(
        table_type="dos",
        mbr_disk_id="0x4f2c9ea0",
        sector_size_bytes=512,
        partitions=tuple(reversed(observed.partitions)),
    )
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("partition_index", "change"),
    (
        (0, lambda part: replace(part, partition_type=None)),
        (0, lambda part: replace(part, partition_type="0x0e")),
        (0, lambda part: replace(part, bootable=None)),
        (0, lambda part: replace(part, bootable=True)),
        (0, lambda part: replace(part, partuuid=None)),
        (0, lambda part: replace(part, partuuid="4f2c9ea0-02")),
        (0, lambda part: replace(part, uuid=None)),
        (0, lambda part: replace(part, uuid="FFFF-FFFF")),
        (1, lambda part: replace(part, partition_type="0x8e")),
        (1, lambda part: replace(part, partuuid="4f2c9ea0-03")),
        (1, lambda part: replace(part, uuid="00000000-0000-0000-0000-000000000000")),
    ),
)
def test_missing_or_changed_boot_root_identity_is_refused(
    partition_index: int,
    change: Callable[[PartitionObservation], PartitionObservation],
) -> None:
    observed = _observation()
    changed_partition = change(observed.partitions[partition_index])
    partitions = list(observed.partitions)
    partitions[partition_index] = changed_partition
    changed = replace(observed, partitions=tuple(partitions))
    assert RefusalCode.PARTITION_IDENTITY_MISMATCH in _codes(changed)


def test_missing_or_changed_mbr_disk_identity_is_refused() -> None:
    observed = _observation()
    assert RefusalCode.PARTITION_IDENTITY_MISMATCH in _codes(replace(observed, mbr_disk_id=None))
    assert RefusalCode.PARTITION_IDENTITY_MISMATCH in _codes(
        replace(observed, mbr_disk_id="0x12345678")
    )


@pytest.mark.parametrize("mbr_disk_id", ("0x4f2c9ea0", "0x624667df"))
def test_valid_observed_mbr_ids_and_derived_partuuids_are_accepted(
    mbr_disk_id: str,
) -> None:
    observed = _with_mbr_disk_id(_observation(), mbr_disk_id)
    assert observed.partitions[0].partuuid == f"{mbr_disk_id[2:]}-01"
    assert observed.partitions[1].partuuid == f"{mbr_disk_id[2:]}-02"
    assert verify_layout(_spec(), observed).state is LayoutState.SOURCE_READY


def test_partuuid_that_does_not_derive_from_observed_mbr_id_is_refused() -> None:
    observed = _with_mbr_disk_id(_observation(), "0x624667df")
    bad_boot = replace(observed.partitions[0], partuuid="4f2c9ea0-01")
    partitions = (bad_boot, observed.partitions[1])
    fingerprint = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id=cast(str, observed.mbr_disk_id),
        sector_size_bytes=observed.sector_size_bytes,
        partitions=partitions,
    )
    changed = replace(
        observed,
        identity=replace(observed.identity, partition_table_fingerprint=fingerprint),
        partitions=partitions,
    )
    assert RefusalCode.PARTITION_IDENTITY_MISMATCH in _codes(changed)


def test_fingerprint_covers_mbr_identity_and_partition_identity() -> None:
    observed = _observation()
    changed_type = replace(observed.partitions[0], partition_type="0x0e")
    changed_fingerprint = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id=cast(str, observed.mbr_disk_id),
        sector_size_bytes=observed.sector_size_bytes,
        partitions=(changed_type, observed.partitions[1]),
    )
    assert changed_fingerprint != observed.identity.partition_table_fingerprint
    changed_disk_id = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id="0x12345678",
        sector_size_bytes=observed.sector_size_bytes,
        partitions=observed.partitions,
    )
    assert changed_disk_id != observed.identity.partition_table_fingerprint


def test_current_mounted_expanded_root_card_remains_refused() -> None:
    report = verify_layout(_spec(), _observation("current-live-expanded.json"))
    assert not report.accepted
    assert {
        RefusalCode.SYSTEM_DISK,
        RefusalCode.ROOT_DISK,
        RefusalCode.ROOT_ALREADY_TOO_LARGE,
        RefusalCode.UNEXPECTED_MOUNT,
    }.issubset(set(report.refusal_codes))


def test_source_root_must_match_the_exact_official_image_size() -> None:
    observed = _observation()
    root = observed.partitions[1]
    changed_root = replace(root, end_sector=root.end_sector - 2048)
    changed = replace(observed, partitions=(observed.partitions[0], changed_root))
    changed = replace(
        changed,
        identity=replace(
            changed.identity,
            partition_table_fingerprint=fingerprint_partition_table(
                table_type=changed.table_type,
                mbr_disk_id=changed.mbr_disk_id or "",
                sector_size_bytes=changed.sector_size_bytes,
                partitions=changed.partitions,
            ),
        ),
    )

    report = verify_layout(_spec(), changed)

    assert report.state is LayoutState.REFUSED
    assert RefusalCode.PARTITION_IDENTITY_MISMATCH in report.refusal_codes


def test_layout_parser_rejects_unknown_keys_and_unbounded_input() -> None:
    payload = (ROOT / "deploy" / "storage" / "layout-v1.toml").read_bytes()
    with pytest.raises(LayoutError, match="unknown key"):
        load_layout_toml(payload + b"\nunknown = 1\n")
    with pytest.raises(LayoutError, match="exceeds"):
        load_layout_toml(b"x" * (64 * 1024 + 1))


def test_observation_parser_is_closed_and_geometry_bound() -> None:
    decoded = json.loads((FIXTURES / "source-ready.json").read_bytes())
    decoded["surprise"] = True
    with pytest.raises(LayoutError, match="unknown key"):
        observation_from_mapping(cast(dict[str, object], decoded))

    decoded = json.loads((FIXTURES / "source-ready.json").read_bytes())
    decoded["identity"]["size_bytes"] = 1
    with pytest.raises(LayoutError, match="does not match"):
        observation_from_mapping(cast(dict[str, object], decoded))
