from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

import dashcam.overlay.native_nv12 as native_nv12
from dashcam.overlay import OVERLAY_1080P_LAYOUT
from dashcam.overlay.native_nv12 import (
    DMABUF_SYNC_END_WRITE,
    DMABUF_SYNC_START_WRITE,
    MAX_DMABUF_MAPPINGS,
    NV12_BUFFER_SIZE,
    NV12_UV_OFFSET,
    OVERLAY_BACKGROUND_LUMA,
    OVERLAY_FOREGROUND_LUMA,
    DmabufMemoryGeometry,
    GstDmabufOverlayRenderer,
    NativeDmabufFrame,
    NativeNv12OverlayCore,
    NativeOverlayContractError,
    render_luma_bitmap,
    validate_native_overlay_text,
)


def exact_frame(fd: int = 10, *, allocator: str = "libcameraallocator0") -> NativeDmabufFrame:
    return NativeDmabufFrame(
        caps_features="memory:SystemMemory",
        caps_media_type="video/x-raw",
        caps_width=1920,
        caps_height=1080,
        caps_format="NV12",
        caps_framerate="30/1",
        buffer_size=NV12_BUFFER_SIZE,
        buffer_memory_count=2,
        buffer_all_memory_writable=False,
        memories=(
            DmabufMemoryGeometry(
                allocator_name=allocator,
                is_dmabuf=True,
                is_fd_memory=True,
                fd=fd,
                size=2_073_600,
                offset=0,
                maxsize=2_073_600,
            ),
            DmabufMemoryGeometry(
                allocator_name=allocator,
                is_dmabuf=True,
                is_fd_memory=True,
                fd=fd,
                size=1_036_800,
                offset=2_073_600,
                maxsize=3_110_400,
            ),
        ),
        video_meta_width=1920,
        video_meta_height=1080,
        video_meta_planes=2,
        video_meta_offsets=(0, 2_073_600, 0, 0),
        video_meta_strides=(1920, 1920, 0, 0),
    )


class FakeMapping:
    def __init__(self, *, fail_write_at: int | None = None) -> None:
        self.data = bytearray([128]) * NV12_BUFFER_SIZE
        self.writes: list[tuple[int, int]] = []
        self.fail_write_at = fail_write_at
        self.closed = False

    def __setitem__(self, key: slice, value: bytes) -> None:
        if self.fail_write_at == len(self.writes):
            raise OSError("mapped write failed\nwith untrusted detail")
        assert key.start is not None and key.stop is not None
        self.data[key] = value
        self.writes.append((key.start, key.stop - key.start))

    def close(self) -> None:
        self.closed = True


class FakeAccess:
    def __init__(self) -> None:
        self.identities: dict[int, tuple[int, int]] = {}
        self.duplicates: list[tuple[int, int]] = []
        self.mappings: dict[int, FakeMapping] = {}
        self.syncs: list[tuple[int, int]] = []
        self.closed_fds: list[int] = []
        self.fail_sync_flag: int | None = None
        self.fail_write_at: int | None = None
        self.change_duplicate_identity = False

    def identity(self, fd: int) -> tuple[int, int]:
        return self.identities.setdefault(fd, (7, fd % 1000))

    def duplicate(self, fd: int) -> int:
        duplicate = fd + 1000
        self.duplicates.append((fd, duplicate))
        source_identity = self.identity(fd)
        self.identities[duplicate] = (
            (source_identity[0], source_identity[1] + 1)
            if self.change_duplicate_identity
            else source_identity
        )
        return duplicate

    def map_shared(self, fd: int, length: int) -> FakeMapping:
        assert length == NV12_BUFFER_SIZE
        mapped = FakeMapping(fail_write_at=self.fail_write_at)
        self.mappings[fd] = mapped
        return mapped

    def sync(self, fd: int, flags: int) -> None:
        self.syncs.append((fd, flags))
        if flags == self.fail_sync_flag:
            raise OSError(f"sync {flags} failed")

    def close_fd(self, fd: int) -> None:
        self.closed_fds.append(fd)


def core_and_access(
    *,
    max_mappings: int = MAX_DMABUF_MAPPINGS,
) -> tuple[NativeNv12OverlayCore, FakeAccess]:
    access = FakeAccess()
    return (
        NativeNv12OverlayCore(access=access, max_mappings=max_mappings),
        access,
    )


def test_measured_native_region_and_bitmap_are_fixed_opaque_and_deterministic() -> None:
    layout = OVERLAY_1080P_LAYOUT
    assert (
        layout.region_width_px,
        layout.region_height_px,
        layout.glyph_width_px,
        layout.line_height_px,
    ) == (1152, 64, 12, 32)
    text = "2026-08-03 21:27:04 +03:00  REC\n31.76832, 35.21371   54 km/h   ALT 782 m   SAT 11"

    first = render_luma_bitmap(text)

    assert first == render_luma_bitmap(text)
    assert len(first) == 1152 * 64
    assert set(first) == {OVERLAY_BACKGROUND_LUMA, OVERLAY_FOREGROUND_LUMA}
    assert first.count(OVERLAY_FOREGROUND_LUMA) > 1_000


@pytest.mark.parametrize(
    "text",
    [
        "TIME UNSYNCED\nGPS INVALID",
        "2026-08-03 21:27:04 +03:00  REC\nGPS LOST",
        "-31.76832, -35.21371   54 mph   HDOP 1.2",
        None,
    ],
)
def test_renderer_accepts_every_current_formatter_glyph(text: str | None) -> None:
    validate_native_overlay_text(text, OVERLAY_1080P_LAYOUT)


@pytest.mark.parametrize(
    "text,match",
    [
        ("REC!", "unsupported glyphs"),
        ("one\ntwo\nthree", "two-line"),
        ("x" * 97, "two-line"),
    ],
)
def test_renderer_refuses_unrepresentable_or_unbounded_payloads(
    text: str,
    match: str,
) -> None:
    with pytest.raises(NativeOverlayContractError, match=match):
        validate_native_overlay_text(text, OVERLAY_1080P_LAYOUT)


@pytest.mark.parametrize(
    "changed",
    [
        {"caps_features": "memory:DMABuf"},
        {"caps_media_type": "video/x-bayer"},
        {"caps_width": 1280},
        {"caps_height": 720},
        {"caps_format": "I420"},
        {"caps_framerate": "30000/1001"},
        {"buffer_size": NV12_BUFFER_SIZE + 1},
        {"buffer_memory_count": 1},
        {"buffer_all_memory_writable": True},
        {"video_meta_width": 1280},
        {"video_meta_height": 720},
        {"video_meta_planes": 1},
        {"video_meta_offsets": (1, NV12_UV_OFFSET, 0, 0)},
        {"video_meta_strides": (2048, 1920, 0, 0)},
    ],
)
def test_every_caps_buffer_and_video_meta_drift_latches_passthrough(
    changed: dict[str, object],
) -> None:
    core, access = core_and_access()
    core.set_text("REC")

    assert core.render(replace(exact_frame(), **cast(Any, changed))) is False

    snapshot = core.snapshot()
    assert snapshot.state == "ISOLATED"
    assert snapshot.contract_mismatches == 1
    assert snapshot.transform_failures == 1
    assert snapshot.frames_passthrough == 1
    assert access.duplicates == []


@pytest.mark.parametrize(
    "allocator",
    ["libcameraallocator0", "libcameraallocator1", "libcameraallocator999"],
)
def test_bounded_allocator_recovery_instances_are_accepted(allocator: str) -> None:
    core, _ = core_and_access()
    core.set_text("REC")
    assert core.render(exact_frame(allocator=allocator)) is True


@pytest.mark.parametrize(
    "allocator",
    [
        "libcameraallocator",
        "libcameraallocator00",
        "libcameraallocator1000",
        "otherallocator0",
        "libcameraallocator-1",
    ],
)
def test_allocator_name_shape_is_strict_and_bounded(allocator: str) -> None:
    core, _ = core_and_access()
    core.set_text("REC")
    assert core.render(exact_frame(allocator=allocator)) is False
    assert core.snapshot().state == "ISOLATED"


def test_render_maps_once_reuses_identity_syncs_and_writes_only_luma_region() -> None:
    core, access = core_and_access()
    core.set_text("TIME UNSYNCED\nGPS INVALID")

    assert core.render(exact_frame()) is True
    assert core.render(exact_frame()) is True

    assert access.duplicates == [(10, 1010)]
    mapped = access.mappings[1010]
    assert len(mapped.writes) == 128
    assert mapped.writes[0] == (40 * 1920 + 40, 1152)
    assert mapped.writes[63] == ((40 + 63) * 1920 + 40, 1152)
    assert mapped.data[NV12_UV_OFFSET:] == bytes([128]) * (NV12_BUFFER_SIZE - NV12_UV_OFFSET)
    assert access.syncs == [
        (1010, DMABUF_SYNC_START_WRITE),
        (1010, DMABUF_SYNC_END_WRITE),
        (1010, DMABUF_SYNC_START_WRITE),
        (1010, DMABUF_SYNC_END_WRITE),
    ]
    snapshot = core.snapshot()
    assert snapshot.state == "ACTIVE"
    assert snapshot.frames_rendered == 2
    assert snapshot.bytes_written == 2 * 1152 * 64
    assert snapshot.mappings_cached == snapshot.mappings_created == 1
    assert snapshot.sync_starts == snapshot.sync_ends == 2
    assert snapshot.render_latency_samples == 2
    assert snapshot.render_latency_last_ns is not None
    assert snapshot.render_latency_max_ns >= snapshot.render_latency_last_ns >= 0
    assert isinstance(snapshot.render_latency_bucket_bounds_ns, list)
    assert isinstance(snapshot.render_latency_bucket_counts, list)
    assert sum(snapshot.render_latency_bucket_counts) == 2


def test_silent_overlay_is_true_passthrough_without_validation_or_mapping() -> None:
    core, access = core_and_access()
    core.set_text(None)
    invalid = replace(exact_frame(), caps_format="I420")

    assert core.render(invalid) is False

    snapshot = core.snapshot()
    assert snapshot.state == "SILENT"
    assert snapshot.frames_seen == snapshot.frames_passthrough == 1
    assert snapshot.contract_mismatches == 0
    assert snapshot.render_latency_samples == 0
    assert access.duplicates == []


def test_probe_skips_all_gi_extraction_when_disabled_or_already_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Enum:
        OK = object()

    class Gst:
        PadProbeReturn = Enum

    core, _ = core_and_access()
    renderer = GstDmabufOverlayRenderer(Gst(), object(), object(), core=core)
    extractions = 0

    def forbidden_extraction(*_args: object) -> NativeDmabufFrame:
        nonlocal extractions
        extractions += 1
        raise AssertionError("disabled probe performed GI extraction")

    monkeypatch.setattr(native_nv12, "_extract_dmabuf_frame", forbidden_extraction)
    core.set_text(None)
    assert renderer._probe(object(), object()) is Enum.OK
    core.isolate("already isolated")
    assert renderer._probe(object(), object()) is Enum.OK

    assert extractions == 0
    snapshot = core.snapshot()
    assert snapshot.frames_seen == snapshot.frames_passthrough == 3


def test_update_refusal_retains_last_valid_bitmap() -> None:
    core, _ = core_and_access()
    core.set_text("REC")
    with pytest.raises(NativeOverlayContractError, match="unsupported glyphs"):
        core.set_text("REC!")
    assert core.render(exact_frame()) is True
    assert core.snapshot().update_rejections == 1


def test_mapping_cache_bound_latches_without_mapping_ninth_identity() -> None:
    core, access = core_and_access(max_mappings=2)
    core.set_text("REC")
    assert core.render(exact_frame(10)) is True
    assert core.render(exact_frame(11)) is True

    assert core.render(exact_frame(12)) is False

    snapshot = core.snapshot()
    assert snapshot.mapping_limit_rejections == 1
    assert snapshot.mappings_cached == 2
    assert len(access.duplicates) == 2


def test_duplicate_identity_drift_closes_temporary_resources_and_isolates() -> None:
    core, access = core_and_access()
    access.change_duplicate_identity = True
    core.set_text("REC")

    assert core.render(exact_frame()) is False

    assert access.closed_fds == [1010]
    assert access.mappings == {}
    assert core.snapshot().contract_mismatches == 1


def test_sync_start_failure_isolates_without_write_or_unmatched_end() -> None:
    core, access = core_and_access()
    access.fail_sync_flag = DMABUF_SYNC_START_WRITE
    core.set_text("REC")

    assert core.render(exact_frame()) is False

    assert access.syncs == [(1010, DMABUF_SYNC_START_WRITE)]
    assert access.mappings[1010].writes == []
    snapshot = core.snapshot()
    assert snapshot.sync_starts == snapshot.sync_ends == 0
    assert snapshot.sync_failures == 1


def test_write_failure_still_ends_sync_then_latches_all_future_passthrough() -> None:
    core, access = core_and_access()
    access.fail_write_at = 2
    core.set_text("REC")

    assert core.render(exact_frame()) is False
    assert access.syncs == [
        (1010, DMABUF_SYNC_START_WRITE),
        (1010, DMABUF_SYNC_END_WRITE),
    ]
    assert core.render(exact_frame()) is False

    snapshot = core.snapshot()
    assert snapshot.sync_starts == snapshot.sync_ends == 1
    assert snapshot.transform_failures == 1
    assert snapshot.frames_passthrough == 2
    assert snapshot.bytes_written == 2 * 1152
    assert snapshot.last_error == "mapped write failed with untrusted detail"


def test_sync_end_failure_is_bounded_and_isolates_completed_write() -> None:
    core, access = core_and_access()
    access.fail_sync_flag = DMABUF_SYNC_END_WRITE
    core.set_text("REC")

    assert core.render(exact_frame()) is False

    snapshot = core.snapshot()
    assert snapshot.frames_rendered == 0
    assert snapshot.bytes_written == 1152 * 64
    assert snapshot.sync_starts == 1
    assert snapshot.sync_ends == 0
    assert snapshot.sync_failures == 1


def test_close_releases_every_mapping_and_descriptor_once_and_refuses_updates() -> None:
    core, access = core_and_access()
    core.set_text("REC")
    assert core.render(exact_frame(10)) is True
    assert core.render(exact_frame(11)) is True

    core.close()
    core.close()

    assert sorted(access.closed_fds) == [1010, 1011]
    assert all(mapped.closed for mapped in access.mappings.values())
    snapshot = core.snapshot()
    assert snapshot.mappings_cached == 0
    assert snapshot.mappings_closed == 2
    with pytest.raises(NativeOverlayContractError, match="closed"):
        core.set_text("GPS INVALID")
