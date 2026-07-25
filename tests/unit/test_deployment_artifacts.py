from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
SYSTEMD_ROOT = REPOSITORY_ROOT / "systemd"
NETWORK_TEMPLATE = (
    REPOSITORY_ROOT / "network" / "NetworkManager" / "dashcam-ap.nmconnection.template"
)


def _unit(name: str) -> str:
    return (SYSTEMD_ROOT / name).read_text(encoding="utf-8")


def test_recorder_unit_has_bounded_notify_restart_and_privilege_contract() -> None:
    unit = _unit("dashcamd.service")

    for directive in (
        "Type=notify",
        "NotifyAccess=main",
        "User=dashcam",
        "Restart=on-failure",
        "RestartSec=5s",
        "TimeoutStartSec=45s",
        "TimeoutStopSec=30s",
        "WatchdogSec=20s",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/dashcam /srv/dashcam",
    ):
        assert directive in unit
    assert "dashcam.daemon" in unit
    assert "StartLimitBurst=5" in unit
    assert "Wants=dashcam-storage-check.service" in unit
    assert "Requires=dashcam-storage-check.service" not in unit
    assert "must start in STORAGE_FAULT" in unit
    assert "must not open" in unit


def test_web_unit_cannot_open_camera_audio_or_uart_groups() -> None:
    unit = _unit("dashcam-web.service")

    assert "User=dashcam-web" in unit
    assert "SupplementaryGroups=dashcam-api" in unit
    assert "PrivateDevices=yes" in unit
    assert "CapabilityBoundingSet=\n" in unit
    for forbidden_group in ("video", "render", "dialout", "audio", "dashcam-storage"):
        assert f"SupplementaryGroups={forbidden_group}" not in unit
    assert "ReadWritePaths=/srv/dashcam" not in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitBurst=5" in unit


def test_control_socket_is_group_restricted() -> None:
    unit = _unit("dashcamd.socket")

    assert "ListenStream=/run/dashcam/control.sock" in unit
    assert "SocketGroup=dashcam-api" in unit
    assert "SocketMode=0660" in unit


def test_prepare_removal_is_bounded_narrow_and_not_boot_enabled() -> None:
    unit = _unit("dashcam-prepare-removal.service")

    assert "ExecStart=/usr/libexec/dashcam/prepare-removal" in unit
    assert "TimeoutStartSec=45s" in unit
    assert "CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_BOOT" in unit
    assert "After=dashcamd.service" in unit
    assert "Requires=dashcamd.service" in unit
    assert "[Install]" not in unit
    assert "sudo" not in unit


def test_mount_source_is_an_inert_identity_template() -> None:
    unit = _unit("srv-dashcam.mount.template")

    assert "REPLACE_WITH_VERIFIED_DASHCAM_UUID" in unit
    assert "Where=/srv/dashcam" in unit
    assert "Type=exfat" in unit
    assert "noatime,nosuid,nodev,noexec" in unit
    assert "TimeoutSec=20s" in unit
    assert "JobRunningTimeoutSec=20s" in unit


def test_ap_profile_has_no_usable_shared_secret_and_fixed_local_address() -> None:
    profile = NETWORK_TEMPLATE.read_text(encoding="utf-8")

    assert NETWORK_TEMPLATE.suffix == ".template"
    assert "psk=REPLACE_WITH_UNIQUE_DEVICE_PASSPHRASE" in profile
    assert "ssid=Dashcam-REPLACE_WITH_SHORT_DEVICE_ID" in profile
    assert "interface-name=REPLACE_WITH_PROBED_WIFI_INTERFACE" in profile
    assert "address1=192.168.50.1/24" in profile
    assert "shared-dhcp-range=192.168.50.20,192.168.50.100" in profile
    assert "method=shared" in profile


def test_draft_units_never_construct_shell_or_destructive_commands() -> None:
    forbidden = ("/bin/sh", "/bin/bash", "sudo ", "mkfs", "parted", "sfdisk", " fdisk")
    unit_files = tuple(SYSTEMD_ROOT.glob("*.service")) + tuple(SYSTEMD_ROOT.glob("*.socket"))

    assert unit_files
    for path in unit_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} contains forbidden token {token!r}"


def test_operational_docs_keep_hardware_and_destructive_gates_explicit() -> None:
    installation = (REPOSITORY_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    procedures = (REPOSITORY_ROOT / "docs" / "test-procedures.md").read_text(encoding="utf-8")
    normalized_installation = " ".join(installation.split())
    normalized_procedures = " ".join(procedures.split())

    assert "not a supported Pi installation" in normalized_installation
    assert "separate destructive gate" in normalized_installation
    assert "dry-run plan and refuses execution" in normalized_installation
    assert "does not authorize Pi access" in normalized_procedures
    assert "Local mocks cannot satisfy" in normalized_procedures


def test_windows_card_readme_warns_against_unsafe_removal_and_formatting() -> None:
    readme = (REPOSITORY_ROOT / "deploy" / "storage" / "README-WINDOWS.txt").read_text(
        encoding="utf-8"
    )

    assert "not yet been validated" in readme
    assert "Never remove" in readme
    assert "CANCEL" in readme
    assert "ext4" in readme
    assert "DASHCAM\\clips" in readme


def test_prepare_removal_contract_denies_direct_web_privilege_and_orders_writers() -> None:
    contract = (REPOSITORY_ROOT / "docs" / "prepare-removal.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    assert "CSRF" in contract
    assert "recent reauthentication" in normalized
    assert "receives no systemd, polkit, mount, or shutdown privilege" in normalized
    assert "Only the `dashcam` service identity" in normalized
    assert "fixed-argument root helper accepts no browser/user path or device input" in normalized
    assert "stops every recorder-owned writer" in normalized
    assert "re-verifies recorder readiness plus the configured mount identity" in normalized
