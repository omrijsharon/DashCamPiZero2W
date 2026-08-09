from __future__ import annotations

import importlib
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
    SystemDmabufAccess,
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
        self.write_values: list[bytes] = []
        self.fail_write_at = fail_write_at
        self.closed = False

    def __setitem__(self, key: slice, value: bytes) -> None:
        if self.fail_write_at == len(self.writes):
            raise OSError("mapped write failed\nwith untrusted detail")
        assert key.start is not None and key.stop is not None
        self.data[key] = value
        self.writes.append((key.start, key.stop - key.start))
        self.write_values.append(value)

    def close(self) -> None:
        self.closed = True


class FakeAccess:
    def __init__(self) -> None:
        self.identities: dict[int, tuple[int, int]] = {}
        self.identity_calls: list[int] = []
        self.duplicates: list[tuple[int, int]] = []
        self.mappings: dict[int, FakeMapping] = {}
        self.syncs: list[tuple[int, int]] = []
        self.closed_fds: list[int] = []
        self.fail_sync_flag: int | None = None
        self.fail_write_at: int | None = None
        self.change_duplicate_identity = False

    def identity(self, fd: int) -> tuple[int, int]:
        self.identity_calls.append(fd)
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


class FakeGiAllocator:
    def __init__(self, name: str = "libcameraallocator0") -> None:
        self.name = name


class FakeGiMemory:
    def __init__(self, plane: int, fd: int = 10) -> None:
        self.allocator: FakeGiAllocator | None = FakeGiAllocator()
        self.dmabuf = True
        self.fd_memory = True
        self.fd = fd
        self.sizes: tuple[int, ...] = (
            (2_073_600, 0, 2_073_600)
            if plane == 0
            else (1_036_800, 2_073_600, 3_110_400)
        )

    def get_sizes(self) -> tuple[int, ...]:
        return self.sizes


class FakeGiStructure:
    def __init__(self) -> None:
        self.name = "video/x-raw"
        self.values: dict[str, object] = {
            "width": 1920,
            "height": 1080,
            "format": "NV12",
            "framerate": "30/1",
        }

    def get_name(self) -> str:
        return self.name

    def get_value(self, name: str) -> object:
        return self.values[name]


class FakeGiFeatures:
    def __init__(self) -> None:
        self.text = "memory:SystemMemory"

    def to_string(self) -> str:
        return self.text


class FakeGiCaps:
    def __init__(self) -> None:
        self.size = 1
        self.structure = FakeGiStructure()
        self.features = FakeGiFeatures()

    def get_size(self) -> int:
        return self.size

    def get_structure(self, index: int) -> FakeGiStructure:
        assert index == 0
        return self.structure

    def get_features(self, index: int) -> FakeGiFeatures:
        assert index == 0
        return self.features


class FakeGiVideoMeta:
    def __init__(self) -> None:
        self.width = 1920
        self.height = 1080
        self.n_planes = 2
        self.offset: tuple[int, ...] = (0, 2_073_600, 0, 0)
        self.stride: tuple[int, ...] = (1920, 1920, 0, 0)


class FakeGiBuffer:
    def __init__(self) -> None:
        self.memory_count = 2
        self.memories = (FakeGiMemory(0), FakeGiMemory(1))
        self.size = NV12_BUFFER_SIZE
        self.all_memory_writable = False
        self.video_meta: FakeGiVideoMeta | None = FakeGiVideoMeta()

    def n_memory(self) -> int:
        return self.memory_count

    def peek_memory(self, index: int) -> FakeGiMemory:
        return self.memories[index]

    def get_size(self) -> int:
        return self.size

    def is_all_memory_writable(self) -> bool:
        return self.all_memory_writable


class FakeGiPad:
    def __init__(self) -> None:
        self.caps: FakeGiCaps | None = FakeGiCaps()
        self.probe_type: object | None = None
        self.probe_callback: object | None = None

    def get_current_caps(self) -> FakeGiCaps | None:
        return self.caps

    def add_probe(self, probe_type: object, callback: object) -> int:
        self.probe_type = probe_type
        self.probe_callback = callback
        return 1


class FakeGiCamera:
    def __init__(self, pad: FakeGiPad) -> None:
        self.pad = pad

    def get_static_pad(self, name: str) -> FakeGiPad | None:
        return self.pad if name == "src" else None


class FakeGstAllocators:
    def is_dmabuf_memory(self, memory: FakeGiMemory) -> bool:
        return memory.dmabuf

    def is_fd_memory(self, memory: FakeGiMemory) -> bool:
        return memory.fd_memory

    def dmabuf_memory_get_fd(self, memory: FakeGiMemory) -> int:
        return memory.fd


class FakeGstVideo:
    def buffer_get_video_meta(self, buffer: FakeGiBuffer) -> FakeGiVideoMeta | None:
        return buffer.video_meta


class FakeProbeInfo:
    def __init__(self, buffer: FakeGiBuffer) -> None:
        self.buffer = buffer

    def get_buffer(self) -> FakeGiBuffer:
        return self.buffer


class FakeGst:
    class PadProbeType:
        BUFFER = object()

    class PadProbeReturn:
        OK = object()


def mutate_fake_gi_contract(case: str, pad: FakeGiPad, buffer: FakeGiBuffer) -> None:
    caps = pad.caps
    assert caps is not None
    meta = buffer.video_meta
    assert meta is not None
    y_memory, uv_memory = buffer.memories
    if case == "caps_count":
        caps.size = 2
    elif case == "caps_features":
        caps.features.text = "memory:DMABuf"
    elif case == "caps_media_type":
        caps.structure.name = "video/x-bayer"
    elif case in {"width", "height", "format", "framerate"}:
        caps.structure.values[case] = {
            "width": 1280,
            "height": 720,
            "format": "I420",
            "framerate": "30000/1001",
        }[case]
    elif case == "buffer_size":
        buffer.size += 1
    elif case == "memory_count":
        buffer.memory_count = 1
    elif case == "writable":
        buffer.all_memory_writable = True
    elif case == "y_allocator":
        y_memory.allocator = FakeGiAllocator("otherallocator0")
    elif case == "uv_allocator":
        uv_memory.allocator = FakeGiAllocator("otherallocator0")
    elif case == "y_not_dmabuf":
        y_memory.dmabuf = False
    elif case == "uv_not_dmabuf":
        uv_memory.dmabuf = False
    elif case == "y_not_fd_memory":
        y_memory.fd_memory = False
    elif case == "uv_not_fd_memory":
        uv_memory.fd_memory = False
    elif case == "y_fd":
        y_memory.fd = -1
    elif case == "uv_fd":
        uv_memory.fd = 11
    elif case == "y_size":
        y_memory.sizes = (2_073_599, 0, 2_073_600)
    elif case == "y_offset":
        y_memory.sizes = (2_073_600, 1, 2_073_600)
    elif case == "y_maxsize":
        y_memory.sizes = (2_073_600, 0, 2_073_601)
    elif case == "uv_size":
        uv_memory.sizes = (1_036_799, 2_073_600, 3_110_400)
    elif case == "uv_offset":
        uv_memory.sizes = (1_036_800, 2_073_599, 3_110_400)
    elif case == "uv_maxsize":
        uv_memory.sizes = (1_036_800, 2_073_600, 3_110_399)
    elif case == "sizes_length":
        y_memory.sizes = (2_073_600, 0)
    elif case == "video_meta_missing":
        buffer.video_meta = None
    elif case == "meta_width":
        meta.width = 1280
    elif case == "meta_height":
        meta.height = 720
    elif case == "meta_planes":
        meta.n_planes = 1
    elif case == "meta_offsets":
        meta.offset = (1, 2_073_600, 0, 0)
    elif case == "meta_strides":
        meta.stride = (2048, 1920, 0, 0)
    elif case == "meta_array_length":
        meta.offset = (0, 2_073_600)
    else:  # pragma: no cover - the parameter list is closed below
        raise AssertionError(f"unknown fake GI drift: {case}")


def core_and_access(
    *,
    max_mappings: int = MAX_DMABUF_MAPPINGS,
) -> tuple[NativeNv12OverlayCore, FakeAccess]:
    access = FakeAccess()
    return (
        NativeNv12OverlayCore(access=access, max_mappings=max_mappings),
        access,
    )


def test_system_access_caches_fcntl_and_prepacked_sync_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFcntl:
        F_DUPFD_CLOEXEC = 1030

        def __init__(self) -> None:
            self.duplicates: list[tuple[int, int, int]] = []
            self.syncs: list[tuple[int, int, bytes]] = []

        def fcntl(self, fd: int, command: int, argument: int) -> int:
            self.duplicates.append((fd, command, argument))
            return fd + 1000

        def ioctl(self, fd: int, request: int, argument: bytes) -> None:
            self.syncs.append((fd, request, argument))

    fake = FakeFcntl()
    imports: list[str] = []

    def import_module(name: str) -> object:
        imports.append(name)
        return fake

    monkeypatch.setattr(importlib, "import_module", import_module)
    access = SystemDmabufAccess()

    assert access.duplicate(10) == 1010
    access.sync(1010, DMABUF_SYNC_START_WRITE)
    access.sync(1010, DMABUF_SYNC_END_WRITE)

    assert imports == ["fcntl"]
    assert fake.duplicates == [(10, 1030, 0)]
    assert [len(argument) for _, _, argument in fake.syncs] == [8, 8]
    with pytest.raises(NativeOverlayContractError, match="sync flag"):
        access.sync(1010, 3)
    assert imports == ["fcntl"]


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
    assert all(
        first is second
        for first, second in zip(
            mapped.write_values[:64],
            mapped.write_values[64:],
            strict=True,
        )
    )
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

    def forbidden_extraction(*_args: object) -> int:
        nonlocal extractions
        extractions += 1
        raise AssertionError("disabled probe performed GI extraction")

    monkeypatch.setattr(
        native_nv12,
        "_extract_validated_dmabuf_fd",
        forbidden_extraction,
    )
    core.set_text(None)
    assert renderer._probe(object(), object()) is Enum.OK
    core.isolate("already isolated")
    assert renderer._probe(object(), object()) is Enum.OK

    assert extractions == 0
    snapshot = core.snapshot()
    assert snapshot.frames_seen == snapshot.frames_passthrough == 3


def test_fused_probe_validates_live_gi_contract_and_renders() -> None:
    core, access = core_and_access()
    core.set_text("REC")
    pad = FakeGiPad()
    buffer = FakeGiBuffer()
    renderer = GstDmabufOverlayRenderer(
        FakeGst(),
        FakeGstAllocators(),
        FakeGstVideo(),
        core=core,
    )
    renderer._bind_extractors(pad)

    assert renderer._probe(pad, FakeProbeInfo(buffer)) is FakeGst.PadProbeReturn.OK

    snapshot = core.snapshot()
    assert snapshot.state == "ACTIVE"
    assert snapshot.frames_rendered == 1
    assert snapshot.contract_mismatches == 0
    assert access.duplicates == [(10, 1010)]


@pytest.mark.parametrize(
    "case",
    [
        "caps_count",
        "caps_features",
        "caps_media_type",
        "width",
        "height",
        "format",
        "framerate",
        "buffer_size",
        "memory_count",
        "writable",
        "y_allocator",
        "uv_allocator",
        "y_not_dmabuf",
        "uv_not_dmabuf",
        "y_not_fd_memory",
        "uv_not_fd_memory",
        "y_fd",
        "uv_fd",
        "y_size",
        "y_offset",
        "y_maxsize",
        "uv_size",
        "uv_offset",
        "uv_maxsize",
        "sizes_length",
        "video_meta_missing",
        "meta_width",
        "meta_height",
        "meta_planes",
        "meta_offsets",
        "meta_strides",
        "meta_array_length",
    ],
)
def test_fused_probe_refuses_every_live_gi_contract_drift_before_access(case: str) -> None:
    core, access = core_and_access()
    core.set_text("REC")
    pad = FakeGiPad()
    buffer = FakeGiBuffer()
    mutate_fake_gi_contract(case, pad, buffer)
    renderer = GstDmabufOverlayRenderer(
        FakeGst(),
        FakeGstAllocators(),
        FakeGstVideo(),
        core=core,
    )
    renderer._bind_extractors(pad)

    assert renderer._probe(pad, FakeProbeInfo(buffer)) is FakeGst.PadProbeReturn.OK

    snapshot = core.snapshot()
    assert snapshot.state == "ISOLATED"
    assert snapshot.contract_mismatches == 1
    assert snapshot.frames_passthrough == 1
    assert access.identity_calls == []
    assert access.duplicates == []
    assert access.syncs == []


def test_fused_probe_revalidates_mutated_same_objects_before_second_frame_access() -> None:
    core, access = core_and_access()
    core.set_text("REC")
    pad = FakeGiPad()
    buffer = FakeGiBuffer()
    renderer = GstDmabufOverlayRenderer(
        FakeGst(),
        FakeGstAllocators(),
        FakeGstVideo(),
        core=core,
    )
    renderer._bind_extractors(pad)

    assert renderer._probe(pad, FakeProbeInfo(buffer)) is FakeGst.PadProbeReturn.OK
    identity_calls = list(access.identity_calls)
    syncs = list(access.syncs)
    assert pad.caps is not None
    pad.caps.structure.values["format"] = "I420"

    assert renderer._probe(pad, FakeProbeInfo(buffer)) is FakeGst.PadProbeReturn.OK

    snapshot = core.snapshot()
    assert snapshot.state == "ISOLATED"
    assert snapshot.frames_seen == 2
    assert snapshot.frames_rendered == 1
    assert snapshot.frames_passthrough == 1
    assert snapshot.contract_mismatches == 1
    assert access.identity_calls == identity_calls
    assert access.syncs == syncs


def test_attach_owns_extractor_when_callback_uses_a_distinct_pad_wrapper() -> None:
    core, access = core_and_access()
    core.set_text("REC")
    attached_pad = FakeGiPad()
    renderer = GstDmabufOverlayRenderer(
        FakeGst(),
        FakeGstAllocators(),
        FakeGstVideo(),
        core=core,
    )
    renderer.attach(FakeGiCamera(attached_pad))

    assert attached_pad.probe_type is FakeGst.PadProbeType.BUFFER
    assert attached_pad.probe_callback is not None
    probe_callback = cast(Any, attached_pad.probe_callback)
    assert (
        probe_callback(object(), FakeProbeInfo(FakeGiBuffer()))
        is FakeGst.PadProbeReturn.OK
    )
    assert core.snapshot().state == "ACTIVE"
    assert access.duplicates == [(10, 1010)]


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
