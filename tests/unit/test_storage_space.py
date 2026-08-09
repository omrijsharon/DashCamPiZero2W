from __future__ import annotations

import errno
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from dashcam.catalog.database import RetentionThresholdLatch
from dashcam.storage.retention import RetentionMode, StorageThresholds
from dashcam.storage.space import (
    FilesystemSpaceObservation,
    LinuxSpaceObserver,
    SpaceObservationFault,
    StorageSpaceMonitor,
    StorageSpaceSnapshot,
)


@dataclass
class Store:
    latch: RetentionThresholdLatch | None = None
    fail_load: bool = False
    fail_store: bool = False
    stores: int = 0
    history: list[RetentionThresholdLatch] = field(default_factory=list)

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None:
        if self.fail_load:
            raise OSError("load failed")
        return self.latch

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None:
        if self.fail_store:
            raise OSError("store failed")
        self.latch = latch
        self.stores += 1
        self.history.append(latch)


class Observer:
    def __init__(self, *values: object) -> None:
        self.values = list(values)

    def __call__(self) -> FilesystemSpaceObservation:
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


def monitor(observer: Observer, store: Store | None = None) -> StorageSpaceMonitor:
    return StorageSpaceMonitor(
        volume_uuid="7EED-3EA7",
        expected_device_id="179:3",
        expected_capacity_bytes=1_000,
        thresholds=StorageThresholds(15, 20, 100, 25),
        observer=observer,
        latch_store=store or Store(),
    )


def sample(
    free_bytes: int,
    *,
    capacity: int = 1_000,
    device: str = "179:3",
) -> FilesystemSpaceObservation:
    return FilesystemSpaceObservation(device, capacity, free_bytes)


def latch(reclaim_latched: bool, **changes: object) -> RetentionThresholdLatch:
    values: dict[str, object] = {
        "volume_uuid": "7EED-3EA7",
        "capacity_bytes": 1_000,
        "reclaim_latched": reclaim_latched,
    }
    values.update(changes)
    return RetentionThresholdLatch(**values)  # type: ignore[arg-type]


def test_exact_boundaries_persist_hysteresis_and_emergency_is_one_sample_condition() -> None:
    store = Store()
    observed = Observer(
        sample(200),
        sample(150),
        sample(149),
        sample(25),
        sample(24),
        sample(25),
        sample(199),
        sample(200),
    )
    subject = monitor(observed, store)

    assert subject.observe().mode is RetentionMode.NORMAL
    assert subject.observe().mode is RetentionMode.NORMAL
    assert subject.observe().mode is RetentionMode.RECLAIMING
    at_emergency = subject.observe()
    assert at_emergency.mode is RetentionMode.RECLAIMING
    emergency = subject.observe()
    assert emergency.mode is RetentionMode.EMERGENCY
    assert emergency.stop_required
    recovered_condition = subject.observe()
    assert recovered_condition.mode is RetentionMode.RECLAIMING
    assert not recovered_condition.stop_required
    assert subject.observe().mode is RetentionMode.RECLAIMING
    assert subject.observe().mode is RetentionMode.NORMAL
    assert store.latch == latch(False)


def test_restart_in_hysteresis_band_uses_durable_latch_or_absent_normal_default() -> None:
    missing = monitor(Observer(sample(175)), Store()).observe()
    assert missing.mode is RetentionMode.NORMAL
    assert missing.fault is None

    reclaiming = monitor(Observer(sample(175)), Store(latch(True))).observe()
    normal = monitor(Observer(sample(175)), Store(latch(False))).observe()
    assert reclaiming.mode is RetentionMode.RECLAIMING
    assert normal.mode is RetentionMode.NORMAL


def test_current_valid_threshold_configuration_applies_to_durable_reclaim_latch() -> None:
    store = Store(latch(True))
    subject = StorageSpaceMonitor(
        volume_uuid="7EED-3EA7",
        expected_device_id="179:3",
        expected_capacity_bytes=1_000,
        thresholds=StorageThresholds(10, 30, 100, 25),
        observer=Observer(sample(250), sample(300)),
        latch_store=store,
    )

    assert subject.observe().mode is RetentionMode.RECLAIMING
    assert subject.observe().mode is RetentionMode.NORMAL
    assert store.latch == latch(False)


def test_snapshot_reports_monotonic_age_and_never_serializes_full_uuid() -> None:
    now = [100]
    subject = StorageSpaceMonitor(
        volume_uuid="7EED-3EA7",
        expected_device_id="179:3",
        expected_capacity_bytes=1_000,
        thresholds=StorageThresholds(15, 20, 100, 25),
        observer=Observer(sample(500)),
        latch_store=Store(),
        monotonic_ns=lambda: now[0],
    )

    subject.observe()
    now[0] = 175
    public = subject.snapshot.as_dict()

    assert public["sample_age_ns"] == 75
    assert public["volume_uuid_suffix"] == "3EA7"
    assert "7EED-3EA7" not in repr(public)


@pytest.mark.parametrize(
    "stored",
    [
        latch(False, volume_uuid="FOREIGN"),
        latch(False, capacity_bytes=2_000),
    ],
)
def test_durable_latch_binding_drift_refuses_without_overwrite(
    stored: RetentionThresholdLatch,
) -> None:
    store = Store(stored)
    status = monitor(Observer(sample(500)), store).observe()

    assert status.fault is SpaceObservationFault.LATCH_BINDING_MISMATCH
    assert status.stop_required
    assert store.latch is stored
    assert store.stores == 0


@pytest.mark.parametrize(
    ("observation", "fault"),
    [
        (sample(500, device="8:1"), SpaceObservationFault.IDENTITY_DRIFT),
        (sample(500, capacity=999), SpaceObservationFault.CAPACITY_DRIFT),
    ],
)
def test_live_identity_or_capacity_drift_latches_refusal(
    observation: FilesystemSpaceObservation,
    fault: SpaceObservationFault,
) -> None:
    observer = Observer(observation, sample(500))
    subject = monitor(observer)

    assert subject.observe().fault is fault
    assert subject.observe().fault is fault
    assert len(observer.values) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        object(),
        FilesystemSpaceObservation("179:3", 0, 0),
        FilesystemSpaceObservation("179:3", 1_000, 1_001),
        FilesystemSpaceObservation("bad", 1_000, 500),
        FilesystemSpaceObservation("179:3", True, 0),
    ],
)
def test_invalid_stat_results_use_bounded_stale_budget(invalid: object) -> None:
    subject = monitor(Observer(invalid, invalid, invalid))

    assert subject.observe().fault is SpaceObservationFault.INVALID_OBSERVATION
    assert not subject.snapshot.stop_required
    assert subject.observe().fault is SpaceObservationFault.INVALID_OBSERVATION
    expired = subject.observe()
    assert expired.fault is SpaceObservationFault.OBSERVATION_STALE
    assert expired.stop_required


def test_observation_exceptions_are_contained_and_a_valid_retry_recovers() -> None:
    subject = monitor(Observer(OSError("stat failed"), sample(500)))

    failed = subject.observe()
    assert failed.fault is SpaceObservationFault.OBSERVATION_FAILED
    assert failed.stale
    assert not failed.stop_required
    recovered = subject.observe()
    assert recovered.fault is None
    assert recovered.mode is RetentionMode.NORMAL
    assert recovered.consecutive_observation_failures == 0


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EDQUOT])
def test_equivalent_no_space_write_persists_latch_and_requests_stop(number: int) -> None:
    store = Store(latch(False))
    subject = monitor(Observer(sample(500)), store)
    assert subject.observe().mode is RetentionMode.NORMAL

    assert subject.note_write_error(OSError(number, "full"))
    status = subject.snapshot
    assert status.mode is RetentionMode.EMERGENCY
    assert status.fault is SpaceObservationFault.NO_SPACE_WRITE
    assert status.trigger == "NO_SPACE_WRITE"
    assert status.stop_required
    assert store.latch == latch(True)


def test_unrelated_write_error_does_not_change_status() -> None:
    subject = monitor(Observer(sample(500)))
    before = subject.observe()

    assert not subject.note_write_error(PermissionError(errno.EACCES, "denied"))
    assert subject.snapshot.sequence == before.sequence
    assert subject.snapshot.mode is before.mode
    assert subject.snapshot.fault is before.fault


def test_threshold_status_is_advisory_and_never_calls_clip_or_camera_apis() -> None:
    class GuardedStore(Store):
        def __getattr__(self, name: str) -> object:
            if any(token in name for token in ("clip", "delete", "camera")):
                raise AssertionError(f"unexpected mutation API: {name}")
            raise AttributeError(name)

    status = monitor(Observer(sample(149)), GuardedStore()).observe()

    assert status.mode is RetentionMode.RECLAIMING
    assert status.directive is not None
    assert status.directive.requested_reclaim_bytes == 51
    assert not status.directive.protected_deletion_allowed
    assert not status.reclaimer_enabled
    assert not status.stop_required


def test_linux_observer_uses_bavail_blocks_and_one_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr("dashcam.storage.space.os.open", lambda path, flags: 7)
    monkeypatch.setattr(
        "dashcam.storage.space.os.fstat",
        lambda descriptor: SimpleNamespace(st_mode=0o040755, st_dev=0x0000B303),
    )
    monkeypatch.setattr(
        "dashcam.storage.space.os.fstatvfs",
        lambda descriptor: SimpleNamespace(f_blocks=1_000, f_bavail=123, f_frsize=4_096),
        raising=False,
    )
    monkeypatch.setattr(
        "dashcam.storage.space.os.close",
        lambda descriptor: calls.append(("close", descriptor)),
    )
    monkeypatch.setattr("dashcam.storage.space.os.major", lambda device: 179, raising=False)
    monkeypatch.setattr("dashcam.storage.space.os.minor", lambda device: 3, raising=False)

    result = LinuxSpaceObserver(__import__("pathlib").Path("/srv/dashcam"))()

    assert result == FilesystemSpaceObservation("179:3", 4_096_000, 503_808)
    assert calls == [("close", 7)]


def test_observation_and_no_space_transition_are_single_owner_ordered() -> None:
    entered = threading.Event()
    release = threading.Event()
    no_space_done = threading.Event()
    store = Store(latch(False))

    def blocked_observer() -> FilesystemSpaceObservation:
        entered.set()
        assert release.wait(timeout=1)
        return sample(149)

    subject = StorageSpaceMonitor(
        volume_uuid="7EED-3EA7",
        expected_device_id="179:3",
        expected_capacity_bytes=1_000,
        thresholds=StorageThresholds(15, 20, 100, 25),
        observer=blocked_observer,
        latch_store=store,
    )
    observed: list[StorageSpaceSnapshot] = []

    def record_no_space() -> None:
        subject.note_no_space_write()
        no_space_done.set()

    observing = threading.Thread(target=lambda: observed.append(subject.observe()))
    no_space = threading.Thread(target=record_no_space)

    observing.start()
    assert entered.wait(timeout=1)
    no_space.start()
    assert not no_space_done.wait(timeout=0.05)
    release.set()
    observing.join(timeout=1)
    no_space.join(timeout=1)

    assert observed and observed[0].mode is RetentionMode.RECLAIMING
    assert no_space_done.is_set()
    assert [item.reclaim_latched for item in store.history] == [True, True]
    assert subject.snapshot.mode is RetentionMode.EMERGENCY
    assert subject.snapshot.sequence == 2
