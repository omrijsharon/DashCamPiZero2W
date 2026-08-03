from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from dashcam.overlay import OVERLAY_1080P_LAYOUT
from dashcam.overlay.native_nv12 import (
    NV12_BUFFER_SIZE,
    NV12_UV_OFFSET,
    OVERLAY_BACKGROUND_LUMA,
    OVERLAY_FOREGROUND_LUMA,
    NativeNv12OverlayCore,
    NativeOverlayContractError,
    Nv12FrameLayout,
    render_luma_bitmap,
    validate_native_overlay_text,
)


def exact_layout() -> Nv12FrameLayout:
    return Nv12FrameLayout(
        width=1920,
        height=1080,
        format="NV12",
        buffer_size=NV12_BUFFER_SIZE,
        y_stride=1920,
        y_offset=0,
        uv_stride=1920,
        uv_offset=NV12_UV_OFFSET,
    )


class FakeBuffer:
    def __init__(self, *, size: int = NV12_BUFFER_SIZE) -> None:
        self.data = bytearray([128]) * size
        self.fills: list[tuple[int, int]] = []

    def get_size(self) -> int:
        return len(self.data)

    def fill(self, offset: int, data: bytes) -> int:
        self.data[offset : offset + len(data)] = data
        self.fills.append((offset, len(data)))
        return len(data)


class ShortWriteBuffer(FakeBuffer):
    def fill(self, offset: int, data: bytes) -> int:
        if len(self.fills) == 2:
            self.fills.append((offset, len(data) - 1))
            return len(data) - 1
        return super().fill(offset, data)


class FailingBuffer(FakeBuffer):
    def fill(self, offset: int, data: bytes) -> int:
        raise OSError("write failed\nwith untrusted detail")


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
    second = render_luma_bitmap(text)

    assert first == second
    assert len(first) == 1152 * 64
    assert set(first) == {OVERLAY_BACKGROUND_LUMA, OVERLAY_FOREGROUND_LUMA}
    assert first.count(OVERLAY_FOREGROUND_LUMA) > 1_000
    assert first.count(OVERLAY_BACKGROUND_LUMA) > first.count(OVERLAY_FOREGROUND_LUMA)


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
        {"width": 1280},
        {"height": 720},
        {"format": "I420"},
        {"buffer_size": NV12_BUFFER_SIZE + 1},
        {"y_stride": 2048},
        {"y_offset": 16},
        {"uv_stride": 2048},
        {"uv_offset": NV12_UV_OFFSET + 1},
    ],
)
def test_caps_contract_refuses_every_layout_drift(changed: dict[str, object]) -> None:
    core = NativeNv12OverlayCore()
    with pytest.raises(NativeOverlayContractError, match="tightly packed"):
        core.configure_layout(replace(exact_layout(), **cast(Any, changed)))
    snapshot = core.snapshot()
    assert snapshot.state == "UNCONFIGURED"
    assert snapshot.caps_accepted is False
    assert snapshot.last_error == "overlay requires tightly packed 1920x1080 NV12"


def test_transform_writes_only_the_measured_luma_region_and_counts_exact_bytes() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text("TIME UNSYNCED\nGPS INVALID")
    buffer = FakeBuffer()
    before_chroma = bytes(buffer.data[NV12_UV_OFFSET:])

    assert core.transform(buffer) is True

    assert len(buffer.fills) == 64
    assert buffer.fills[0] == (40 * 1920 + 40, 1152)
    assert buffer.fills[-1] == ((40 + 63) * 1920 + 40, 1152)
    assert bytes(buffer.data[NV12_UV_OFFSET:]) == before_chroma
    assert buffer.data[0] == 128
    assert buffer.data[40 * 1920 + 40] == OVERLAY_BACKGROUND_LUMA
    snapshot = core.snapshot()
    assert snapshot.state == "ACTIVE"
    assert snapshot.frames_seen == 1
    assert snapshot.frames_rendered == 1
    assert snapshot.frames_passthrough == 0
    assert snapshot.bytes_written == 1152 * 64
    assert snapshot.transform_failures == 0


def test_silent_overlay_and_deduplicated_updates_do_no_frame_writes() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text(None)
    core.set_text(None)
    buffer = FakeBuffer()

    assert core.transform(buffer) is False
    snapshot = core.snapshot()
    assert snapshot.state == "SILENT"
    assert snapshot.updates == 1
    assert snapshot.frames_seen == 1
    assert snapshot.frames_rendered == 0
    assert snapshot.frames_passthrough == 1
    assert buffer.fills == []


def test_update_refusal_retains_the_last_valid_cached_bitmap() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text("REC")
    with pytest.raises(NativeOverlayContractError, match="unsupported glyphs"):
        core.set_text("REC!")

    buffer = FakeBuffer()
    assert core.transform(buffer) is True
    snapshot = core.snapshot()
    assert snapshot.state == "ACTIVE"
    assert snapshot.updates == 1
    assert snapshot.update_rejections == 1
    assert snapshot.frames_rendered == 1


def test_wrong_buffer_size_isolates_without_writing_or_raising() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text("REC")
    wrong = FakeBuffer(size=NV12_BUFFER_SIZE - 1)

    assert core.transform(wrong) is False
    assert wrong.fills == []
    snapshot = core.snapshot()
    assert snapshot.state == "ISOLATED"
    assert snapshot.buffer_size_mismatches == 1
    assert snapshot.transform_failures == 1
    assert snapshot.frames_passthrough == 1


def test_short_write_latches_isolation_and_all_later_frames_pass_through() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text("REC")
    first = ShortWriteBuffer()

    assert core.transform(first) is False
    after_failure = core.snapshot()
    assert after_failure.state == "ISOLATED"
    assert after_failure.short_writes == 1
    assert after_failure.transform_failures == 1
    assert after_failure.bytes_written == 2 * 1152

    later = FakeBuffer()
    assert core.transform(later) is False
    assert later.fills == []
    final = core.snapshot()
    assert final.frames_seen == 2
    assert final.frames_rendered == 0
    assert final.frames_passthrough == 2


def test_transform_exception_is_bounded_sanitized_and_isolated() -> None:
    core = NativeNv12OverlayCore()
    core.configure_layout(exact_layout())
    core.set_text("REC")

    assert core.transform(FailingBuffer()) is False
    snapshot = core.snapshot()
    assert snapshot.state == "ISOLATED"
    assert snapshot.last_error == "write failed with untrusted detail"
    assert len(snapshot.last_error) <= 256


def test_transform_before_caps_is_a_negotiation_error() -> None:
    core = NativeNv12OverlayCore()
    core.set_text("REC")
    with pytest.raises(NativeOverlayContractError, match="before exact caps"):
        core.transform(FakeBuffer())
