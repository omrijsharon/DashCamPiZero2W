from __future__ import annotations

import hashlib
import json

from dashcam.config import default_config
from dashcam.provisioning.bootstrap import (
    Geometry,
    Journal,
    Partition,
    Phase,
    _sentinel_mapping,
)
from dashcam.storage.preflight import (
    GIB,
    ProbeFile,
    policy_from_identity,
    run_storage_preflight,
    storage_identity_from_env,
)


class DurableProbe:
    def write(self, payload: bytes) -> int:
        return len(payload)

    def fsync(self) -> None:
        return None

    def close(self) -> None:
        return None


class ProbeFilesystem:
    def create_exclusive(self, recording_root: str, relative_name: str) -> ProbeFile:
        assert recording_root == "/srv/dashcam"
        assert relative_name == ".dashcam-preflight-v1.tmp"
        return DurableProbe()

    def unlink(self, recording_root: str, relative_name: str) -> None:
        assert recording_root == "/srv/dashcam"
        assert relative_name == ".dashcam-preflight-v1.tmp"


def test_bootstrap_canonical_sentinel_and_rootfs_handoff_activate_preflight() -> None:
    fingerprint = hashlib.sha256(b"source-mbr").hexdigest()
    root = Partition(number=2, start_sector=1_064_960, size_sectors=12_582_912, type_code=0x83)
    data = Partition(
        number=3,
        start_sector=root.end_sector + 1,
        size_sectors=47_000_000,
        type_code=0x07,
    )
    geometry = Geometry(total_sectors=61_440_000, sector_size=512, root=root, data=data)
    journal = Journal(
        schema_version=1,
        phase=Phase.CONFIGURED,
        disk="/dev/mmcblk0",
        root_partition="/dev/mmcblk0p2",
        data_partition="/dev/mmcblk0p3",
        cid="fe34325344000000200000031a0192d1",
        size_bytes=31_457_280_000,
        stage_a_boot_id="11111111-1111-4111-8111-111111111111",
        source_mbr_sha256=fingerprint,
        target=geometry,
        committed_mbr_sha256=hashlib.sha256(b"target-mbr").hexdigest(),
        data_uuid="A1B2-C3D4",
    )
    canonical_sentinel = json.loads(
        json.dumps(_sentinel_mapping(journal), sort_keys=True, separators=(",", ":")) + "\n"
    )
    identity = storage_identity_from_env(
        (
            "DASHCAM_STORAGE_SCHEMA_VERSION=1\n"
            "DASHCAM_STORAGE_LAYOUT_VERSION=1\n"
            "DASHCAM_STORAGE_MOUNT=/srv/dashcam\n"
            "DASHCAM_STORAGE_UUID=A1B2-C3D4\n"
            f"DASHCAM_STORAGE_CID={journal.cid}\n"
            f"DASHCAM_STORAGE_SOURCE_MBR_SHA256={fingerprint}\n"
            f"DASHCAM_STORAGE_ROOT_END_SECTOR={root.end_sector}\n"
            f"DASHCAM_STORAGE_DATA_START_SECTOR={data.start_sector}\n"
            f"DASHCAM_STORAGE_DATA_END_SECTOR={data.end_sector}\n"
            f"DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES={20 * GIB}\n"
        ).encode()
    )
    policy = policy_from_identity(default_config(), identity)
    facts = {
        "mount": {
            "target": "/srv/dashcam",
            "mounted": True,
            "source": "/dev/mmcblk0p3",
            "filesystem": "exfat",
            "label": "DASHCAM",
            "uuid": "A1B2-C3D4",
            "mount_options": ["rw", "noatime", "noexec"],
            "device_id": "179:3",
            "os_root_device_id": "179:2",
        },
        "space": {"capacity_bytes": 22 * GIB, "free_bytes": 10 * GIB},
        "sentinel": canonical_sentinel,
    }

    result = run_storage_preflight(
        facts,
        policy=policy,
        filesystem=ProbeFilesystem(),
    )

    assert result.ready
    assert result.reasons == ()
