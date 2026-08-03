"""No-copy, fixed-region renderer for libcamerasrc's exact NV12 DMABUF path.

The production renderer is a probe on ``libcamerasrc``'s source pad.  It never
asks GStreamer to make a writable buffer: it validates the exact allocation
contract measured on the reference Pi, maps the shared DMABUF once per stable
identity, and updates only the fixed luma rectangle.  The syscall seam keeps
the safety policy independently testable on non-Pi hosts.
"""

from __future__ import annotations

import importlib
import os
import re
import struct
import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import asdict, dataclass
from threading import Condition, Lock
from typing import Final, Protocol, SupportsInt, cast

from dashcam.overlay.formatting import OVERLAY_1080P_LAYOUT, OverlayLayout

NV12_FORMAT: Final = "NV12"
NV12_FRAME_WIDTH: Final = 1920
NV12_FRAME_HEIGHT: Final = 1080
NV12_Y_STRIDE: Final = 1920
NV12_Y_OFFSET: Final = 0
NV12_UV_STRIDE: Final = 1920
NV12_UV_OFFSET: Final = NV12_FRAME_WIDTH * NV12_FRAME_HEIGHT
NV12_BUFFER_SIZE: Final = NV12_FRAME_WIDTH * NV12_FRAME_HEIGHT * 3 // 2
OVERLAY_BACKGROUND_LUMA: Final = 16
OVERLAY_FOREGROUND_LUMA: Final = 235
SYSTEM_MEMORY_FEATURE: Final = "memory:SystemMemory"
DMABUF_IOCTL_SYNC: Final = 0x40086200
DMABUF_SYNC_START_WRITE: Final = 2
DMABUF_SYNC_END_WRITE: Final = 6
_DMABUF_SYNC_ARGUMENTS: Final = {
    DMABUF_SYNC_START_WRITE: struct.pack("Q", DMABUF_SYNC_START_WRITE),
    DMABUF_SYNC_END_WRITE: struct.pack("Q", DMABUF_SYNC_END_WRITE),
}
MAX_DMABUF_MAPPINGS: Final = 8
MAX_ALLOCATOR_INSTANCE: Final = 999
_ALLOCATOR_NAME: Final = re.compile(r"libcameraallocator(?:0|[1-9][0-9]{0,2})\Z")
_LATENCY_BUCKET_BOUNDS_NS: Final = (
    1_000_000,
    2_000_000,
    4_000_000,
    8_000_000,
    16_000_000,
    32_000_000,
    64_000_000,
    128_000_000,
)
_MAX_ERROR_CHARS: Final = 256
_UNSET: Final = object()


class NativeOverlayContractError(ValueError):
    """The native overlay received input outside its fixed production contract."""


class WritableMapping(Protocol):
    """The slice-write and close subset supplied by :mod:`mmap`."""

    def __setitem__(self, key: slice, value: bytes) -> None: ...

    def close(self) -> None: ...


class _FcntlModule(Protocol):
    def fcntl(self, fd: int, command: int, argument: int) -> SupportsInt: ...

    def ioctl(self, fd: int, request: int, argument: bytes) -> object: ...


@dataclass(frozen=True, slots=True)
class DmabufMemoryGeometry:
    """One exact GstMemory plane descriptor observed on the reference Pi."""

    allocator_name: str
    is_dmabuf: bool
    is_fd_memory: bool
    fd: int
    size: int
    offset: int
    maxsize: int


@dataclass(frozen=True, slots=True)
class NativeDmabufFrame:
    """Coordinate-free metadata plus the transient source DMABUF descriptor."""

    caps_features: str
    caps_media_type: str
    caps_width: int
    caps_height: int
    caps_format: str
    caps_framerate: str
    buffer_size: int
    buffer_memory_count: int
    buffer_all_memory_writable: bool
    memories: tuple[DmabufMemoryGeometry, DmabufMemoryGeometry]
    video_meta_width: int
    video_meta_height: int
    video_meta_planes: int
    video_meta_offsets: tuple[int, int, int, int]
    video_meta_strides: tuple[int, int, int, int]

    @property
    def dmabuf_fd(self) -> int:
        return self.memories[0].fd

    def validate(self) -> None:
        """Refuse every drift from the measured libcamerasrc allocation."""

        if (
            self.caps_features != SYSTEM_MEMORY_FEATURE
            or self.caps_media_type != "video/x-raw"
            or self.caps_width != NV12_FRAME_WIDTH
            or self.caps_height != NV12_FRAME_HEIGHT
            or self.caps_format != NV12_FORMAT
            or self.caps_framerate != "30/1"
        ):
            raise NativeOverlayContractError(
                "overlay caps differ from exact 1920x1080 NV12 SystemMemory"
            )
        if (
            self.buffer_size != NV12_BUFFER_SIZE
            or self.buffer_memory_count != 2
            or self.buffer_all_memory_writable
        ):
            raise NativeOverlayContractError(
                "overlay buffer size, plane count, or writability differs"
            )
        y_memory, uv_memory = self.memories
        if (
            _ALLOCATOR_NAME.fullmatch(y_memory.allocator_name) is None
            or y_memory.allocator_name != uv_memory.allocator_name
            or not y_memory.is_dmabuf
            or not uv_memory.is_dmabuf
            or not y_memory.is_fd_memory
            or not uv_memory.is_fd_memory
        ):
            raise NativeOverlayContractError(
                "overlay requires two matching bounded libcameraallocator DMABUF memories"
            )
        if y_memory.fd < 0 or y_memory.fd != uv_memory.fd:
            raise NativeOverlayContractError(
                "overlay NV12 planes do not share one valid DMABUF fd"
            )
        if (
            y_memory.size != NV12_FRAME_WIDTH * NV12_FRAME_HEIGHT
            or y_memory.offset != NV12_Y_OFFSET
            or y_memory.maxsize != NV12_FRAME_WIDTH * NV12_FRAME_HEIGHT
            or uv_memory.size != NV12_FRAME_WIDTH * NV12_FRAME_HEIGHT // 2
            or uv_memory.offset != NV12_UV_OFFSET
            or uv_memory.maxsize != NV12_BUFFER_SIZE
        ):
            raise NativeOverlayContractError("overlay DMABUF plane geometry differs")
        if (
            self.video_meta_width != NV12_FRAME_WIDTH
            or self.video_meta_height != NV12_FRAME_HEIGHT
            or self.video_meta_planes != 2
            or self.video_meta_offsets != (NV12_Y_OFFSET, NV12_UV_OFFSET, 0, 0)
            or self.video_meta_strides != (NV12_Y_STRIDE, NV12_UV_STRIDE, 0, 0)
        ):
            raise NativeOverlayContractError("overlay GstVideoMeta geometry differs")


class DmabufAccess(Protocol):
    """Injectable, bounded operating-system seam used by the renderer core."""

    def identity(self, fd: int) -> tuple[int, int]: ...

    def duplicate(self, fd: int) -> int: ...

    def map_shared(self, fd: int, length: int) -> WritableMapping: ...

    def sync(self, fd: int, flags: int) -> None: ...

    def close_fd(self, fd: int) -> None: ...


@dataclass(frozen=True, slots=True)
class NativeOverlaySnapshot:
    """Bounded, coordinate-free accounting for one camera-pad renderer."""

    state: str
    caps_accepted: bool
    enabled: bool
    updates: int
    update_rejections: int
    frames_seen: int
    frames_rendered: int
    frames_passthrough: int
    bytes_written: int
    contract_mismatches: int
    transform_failures: int
    mappings_cached: int
    mappings_created: int
    mappings_closed: int
    mapping_limit_rejections: int
    sync_starts: int
    sync_ends: int
    sync_failures: int
    render_latency_samples: int
    render_latency_last_ns: int | None
    render_latency_max_ns: int
    render_latency_total_ns: int
    # Snapshot values cross the runtime-status JSON boundary.  The fixed
    # internal tuple/list accounting is copied into fresh JSON arrays here.
    render_latency_bucket_bounds_ns: list[int]
    render_latency_bucket_counts: list[int]
    last_error: str | None


# Five columns by seven rows.  The formatter's closed output alphabet is
# uppercase status/labels, decimal numbers, numeric punctuation, and the four
# lowercase unit letters below.  Rejecting anything else preserves truthful
# text instead of silently substituting a different glyph.
_GLYPHS: Final[dict[str, tuple[str, ...]]] = {
    " ": ("00000",) * 7,
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "h": ("10000", "10000", "10110", "11001", "10001", "10001", "10001"),
    "k": ("10000", "10000", "10010", "10100", "11000", "10100", "10010"),
    "m": ("00000", "00000", "11010", "10101", "10101", "10101", "10101"),
    "p": ("00000", "00000", "11110", "10001", "11110", "10000", "10000"),
}


def _bounded_error(error: BaseException | str) -> str:
    detail = str(error).replace("\r", " ").replace("\n", " ")
    return detail[:_MAX_ERROR_CHARS]


def validate_native_overlay_text(text: str | None, layout: OverlayLayout) -> None:
    """Validate the exact renderer alphabet and fixed two-line bounds."""

    if text is None:
        return
    if not isinstance(text, str):
        raise NativeOverlayContractError("overlay text must be a string or None")
    lines = text.split("\n")
    if len(lines) > 2 or any(len(line) > layout.max_line_chars for line in lines):
        raise NativeOverlayContractError("overlay text exceeds the fixed two-line bounds")
    unsupported = sorted(
        {character for line in lines for character in line if character not in _GLYPHS}
    )
    if unsupported:
        raise NativeOverlayContractError(
            "overlay text contains unsupported glyphs: "
            + "".join(f"0x{ord(character):02x}" for character in unsupported)
        )


def render_luma_bitmap(
    text: str,
    layout: OverlayLayout = OVERLAY_1080P_LAYOUT,
) -> bytes:
    """Pre-render one opaque, tightly packed luma rectangle.

    Glyph work happens only when the 2 Hz producer changes text.  The streaming
    path copies this immutable byte string without invoking a font library.
    """

    validate_native_overlay_text(text, layout)
    bitmap = bytearray([OVERLAY_BACKGROUND_LUMA]) * (
        layout.region_width_px * layout.region_height_px
    )
    horizontal_scale = 2
    vertical_scale = 3
    glyph_width = 5 * horizontal_scale
    glyph_height = 7 * vertical_scale
    if (
        layout.glyph_width_px < glyph_width
        or layout.line_height_px < glyph_height
        or layout.max_line_chars * layout.glyph_width_px > layout.region_width_px
        or 2 * layout.line_height_px > layout.region_height_px
    ):
        raise NativeOverlayContractError("overlay layout cannot contain the fixed bitmap font")

    x_padding = (layout.glyph_width_px - glyph_width) // 2
    y_padding = (layout.line_height_px - glyph_height) // 2
    for line_index, line in enumerate(text.split("\n")):
        line_y = line_index * layout.line_height_px + y_padding
        for character_index, character in enumerate(line):
            glyph = _GLYPHS[character]
            glyph_x = character_index * layout.glyph_width_px + x_padding
            for source_y, row in enumerate(glyph):
                target_y = line_y + source_y * vertical_scale
                for source_x, pixel in enumerate(row):
                    if pixel == "0":
                        continue
                    target_x = glyph_x + source_x * horizontal_scale
                    for repeat_y in range(vertical_scale):
                        start = (
                            (target_y + repeat_y) * layout.region_width_px + target_x
                        )
                        bitmap[start : start + horizontal_scale] = bytes(
                            [OVERLAY_FOREGROUND_LUMA]
                        ) * horizontal_scale
    return bytes(bitmap)


class SystemDmabufAccess:
    """Production implementation of the narrow DMABUF syscall contract."""

    def __init__(self) -> None:
        self._fcntl_module: _FcntlModule | None = None

    def _fcntl(self) -> _FcntlModule:
        module = self._fcntl_module
        if module is None:
            module = cast(_FcntlModule, importlib.import_module("fcntl"))
            self._fcntl_module = module
        return module

    def identity(self, fd: int) -> tuple[int, int]:
        observed = os.fstat(fd)
        return int(observed.st_dev), int(observed.st_ino)

    def duplicate(self, fd: int) -> int:
        fcntl = self._fcntl()
        duplicate_cloexec = getattr(fcntl, "F_DUPFD_CLOEXEC", None)
        if isinstance(duplicate_cloexec, int):
            return int(fcntl.fcntl(fd, duplicate_cloexec, 0))
        duplicate = os.dup(fd)
        os.set_inheritable(duplicate, False)
        return duplicate

    def map_shared(self, fd: int, length: int) -> WritableMapping:
        mmap_module = importlib.import_module("mmap")
        mmap_factory = cast(
            Callable[..., WritableMapping],
            vars(mmap_module)["mmap"],
        )
        return mmap_factory(
            fd,
            length,
            flags=vars(mmap_module)["MAP_SHARED"],
            prot=(
                vars(mmap_module)["PROT_READ"]
                | vars(mmap_module)["PROT_WRITE"]
            ),
        )

    def sync(self, fd: int, flags: int) -> None:
        try:
            argument = _DMABUF_SYNC_ARGUMENTS[flags]
        except KeyError as error:
            raise NativeOverlayContractError(
                "native overlay DMABUF sync flag is invalid"
            ) from error
        self._fcntl().ioctl(fd, DMABUF_IOCTL_SYNC, argument)

    def close_fd(self, fd: int) -> None:
        os.close(fd)


class NativeNv12OverlayCore:
    """Thread-safe bitmap cache and fail-isolated DMABUF write policy."""

    def __init__(
        self,
        layout: OverlayLayout = OVERLAY_1080P_LAYOUT,
        *,
        access: DmabufAccess | None = None,
        max_mappings: int = MAX_DMABUF_MAPPINGS,
    ) -> None:
        if not isinstance(layout, OverlayLayout):
            raise NativeOverlayContractError("native overlay requires an OverlayLayout")
        if (
            layout.origin_x_px + layout.region_width_px > NV12_Y_STRIDE
            or layout.origin_y_px + layout.region_height_px > NV12_FRAME_HEIGHT
            or layout.region_width_px != 1152
            or layout.region_height_px != 64
        ):
            raise NativeOverlayContractError(
                "native overlay region differs from the measured 1152x64 contract"
            )
        if isinstance(max_mappings, bool) or not 1 <= max_mappings <= MAX_DMABUF_MAPPINGS:
            raise NativeOverlayContractError("native overlay mapping bound is invalid")
        self._layout = layout
        self._access = access or SystemDmabufAccess()
        self._max_mappings = max_mappings
        self._lock = Lock()
        self._closed = False
        self._caps_accepted = False
        self._isolated = False
        self._text: str | object | None = _UNSET
        self._bitmap_rows: tuple[bytes, ...] | None = None
        # Both row bytes and mmap slice objects are immutable across frames.
        # Building them at 2 Hz avoids 128 transient objects per 30 Hz write.
        self._destination_slices = tuple(
            slice(
                (layout.origin_y_px + row) * NV12_Y_STRIDE + layout.origin_x_px,
                (layout.origin_y_px + row) * NV12_Y_STRIDE
                + layout.origin_x_px
                + layout.region_width_px,
            )
            for row in range(layout.region_height_px)
        )
        self._mappings: dict[tuple[int, int], tuple[int, WritableMapping]] = {}
        self._updates = 0
        self._update_rejections = 0
        self._frames_seen = 0
        self._frames_rendered = 0
        self._frames_passthrough = 0
        self._bytes_written = 0
        self._contract_mismatches = 0
        self._transform_failures = 0
        self._mappings_created = 0
        self._mappings_closed = 0
        self._mapping_limit_rejections = 0
        self._sync_starts = 0
        self._sync_ends = 0
        self._sync_failures = 0
        self._render_latency_samples = 0
        self._render_latency_last_ns: int | None = None
        self._render_latency_max_ns = 0
        self._render_latency_total_ns = 0
        self._render_latency_bucket_counts = [0] * (
            len(_LATENCY_BUCKET_BOUNDS_NS) + 1
        )
        self._last_error: str | None = None

    def _record_latency(self, started_ns: int) -> None:
        elapsed_ns = max(time.monotonic_ns() - started_ns, 0)
        with self._lock:
            self._record_latency_locked(elapsed_ns)

    def _record_latency_locked(self, elapsed_ns: int) -> None:
        bucket = bisect_left(_LATENCY_BUCKET_BOUNDS_NS, elapsed_ns)
        self._render_latency_samples += 1
        self._render_latency_last_ns = elapsed_ns
        self._render_latency_max_ns = max(self._render_latency_max_ns, elapsed_ns)
        self._render_latency_total_ns += elapsed_ns
        self._render_latency_bucket_counts[bucket] += 1

    def set_text(self, text: str | None) -> None:
        """Pre-render changed text while retaining the last valid bitmap on refusal."""

        try:
            validate_native_overlay_text(text, self._layout)
            rendered = None if text is None else render_luma_bitmap(text, self._layout)
            rendered_rows = (
                None
                if rendered is None
                else tuple(
                    rendered[start : start + self._layout.region_width_px]
                    for start in range(
                        0,
                        len(rendered),
                        self._layout.region_width_px,
                    )
                )
            )
        except (NativeOverlayContractError, ValueError) as error:
            with self._lock:
                self._update_rejections += 1
                self._last_error = _bounded_error(error)
            raise
        with self._lock:
            if self._closed:
                raise NativeOverlayContractError("native overlay is closed")
            if text == self._text:
                return
            self._text = text
            self._bitmap_rows = rendered_rows
            self._updates += 1
            if not self._isolated:
                self._last_error = None

    def requires_frame_inspection(self) -> bool:
        """Return the cheap streaming-path gate before any GI introspection."""

        with self._lock:
            return (
                not self._closed
                and not self._isolated
                and self._bitmap_rows is not None
            )

    def note_passthrough_frame(self) -> None:
        """Account for one frame skipped by the cheap disabled/isolated gate."""

        with self._lock:
            self._frames_seen += 1
            self._frames_passthrough += 1

    def _latch_failure(
        self,
        error: BaseException | str,
        *,
        contract_mismatch: bool = False,
        sync_failure: bool = False,
        mapping_limit: bool = False,
    ) -> None:
        with self._lock:
            self._isolated = True
            self._caps_accepted = False
            self._transform_failures += 1
            self._frames_passthrough += 1
            self._contract_mismatches += int(contract_mismatch)
            self._sync_failures += int(sync_failure)
            self._mapping_limit_rejections += int(mapping_limit)
            self._last_error = _bounded_error(error)

    def _mapping(
        self,
        source_fd: int,
        identity: tuple[int, int],
    ) -> tuple[int, WritableMapping]:
        with self._lock:
            existing = self._mappings.get(identity)
            mapping_count = len(self._mappings)
        if existing is not None:
            return existing
        if mapping_count >= self._max_mappings:
            raise NativeOverlayContractError("native overlay DMABUF mapping bound exceeded")

        duplicate = self._access.duplicate(source_fd)
        mapped: WritableMapping | None = None
        try:
            if self._access.identity(duplicate) != identity:
                raise NativeOverlayContractError(
                    "native overlay duplicated DMABUF identity changed"
                )
            mapped = self._access.map_shared(duplicate, NV12_BUFFER_SIZE)
            with self._lock:
                if identity in self._mappings:
                    raise NativeOverlayContractError(
                        "native overlay concurrent mapping creation"
                    )
                self._mappings[identity] = (duplicate, mapped)
                self._mappings_created += 1
            return duplicate, mapped
        except Exception:
            if mapped is not None:
                mapped.close()
            self._access.close_fd(duplicate)
            raise

    def render(self, frame: NativeDmabufFrame) -> bool:
        """Render one frame in place, or latch isolated passthrough on any drift."""

        started_ns = time.monotonic_ns()
        with self._lock:
            self._frames_seen += 1
            isolated = self._isolated
            bitmap_rows = self._bitmap_rows
            closed = self._closed
        if isolated or bitmap_rows is None or closed:
            with self._lock:
                self._frames_passthrough += 1
            return False

        try:
            if not isinstance(frame, NativeDmabufFrame):
                raise NativeOverlayContractError("native overlay frame is invalid")
            frame.validate()
            identity = self._access.identity(frame.dmabuf_fd)
            if (
                len(identity) != 2
                or isinstance(identity[0], bool)
                or identity[0] < 0
                or isinstance(identity[1], bool)
                or identity[1] < 0
            ):
                raise NativeOverlayContractError(
                    "native overlay DMABUF identity is invalid"
                )
            duplicate, mapped = self._mapping(frame.dmabuf_fd, identity)
        except Exception as error:
            self._latch_failure(
                error,
                contract_mismatch=isinstance(error, NativeOverlayContractError),
                mapping_limit="mapping bound exceeded" in str(error),
            )
            self._record_latency(started_ns)
            return False

        started = False
        ended = False
        written = 0
        render_error: BaseException | None = None
        end_error: BaseException | None = None
        try:
            self._access.sync(duplicate, DMABUF_SYNC_START_WRITE)
            started = True
            for destination, bitmap_row in zip(
                self._destination_slices,
                bitmap_rows,
                strict=True,
            ):
                mapped[destination] = bitmap_row
                written += len(bitmap_row)
        except Exception as error:
            render_error = error
        finally:
            if started:
                try:
                    self._access.sync(duplicate, DMABUF_SYNC_END_WRITE)
                    ended = True
                except Exception as error:
                    end_error = error

        elapsed_ns = max(time.monotonic_ns() - started_ns, 0)
        with self._lock:
            self._sync_starts += int(started)
            self._sync_ends += int(ended)
            self._bytes_written += written
            self._record_latency_locked(elapsed_ns)
            failure = end_error or render_error
            if failure is None:
                if self._isolated:
                    return False
                self._caps_accepted = True
                self._frames_rendered += 1
                return True
            self._isolated = True
            self._caps_accepted = False
            self._transform_failures += 1
            self._frames_passthrough += 1
            self._sync_failures += int(not started or end_error is not None)
            self._last_error = _bounded_error(failure)
            return False

    def isolate(self, error: BaseException | str) -> None:
        """Latch a callback/extraction failure before any unsafe access."""

        with self._lock:
            self._frames_seen += 1
        self._latch_failure(error, contract_mismatch=True)

    def close(self) -> None:
        """Close every cached mapping and duplicate descriptor exactly once."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            mappings = tuple(self._mappings.values())
            self._mappings.clear()
        first_error: BaseException | None = None
        closed = 0
        for duplicate, mapped in mappings:
            try:
                mapped.close()
            except Exception as error:
                first_error = first_error or error
            try:
                self._access.close_fd(duplicate)
            except Exception as error:
                first_error = first_error or error
            closed += 1
        with self._lock:
            self._mappings_closed += closed
            if first_error is not None:
                self._isolated = True
                self._transform_failures += 1
                self._last_error = _bounded_error(first_error)

    def snapshot(self) -> NativeOverlaySnapshot:
        with self._lock:
            state = (
                "ISOLATED"
                if self._isolated
                else "SILENT"
                if self._bitmap_rows is None
                else "ACTIVE"
                if self._caps_accepted
                else "UNCONFIGURED"
            )
            return NativeOverlaySnapshot(
                state=state,
                caps_accepted=self._caps_accepted,
                enabled=self._bitmap_rows is not None,
                updates=self._updates,
                update_rejections=self._update_rejections,
                frames_seen=self._frames_seen,
                frames_rendered=self._frames_rendered,
                frames_passthrough=self._frames_passthrough,
                bytes_written=self._bytes_written,
                contract_mismatches=self._contract_mismatches,
                transform_failures=self._transform_failures,
                mappings_cached=len(self._mappings),
                mappings_created=self._mappings_created,
                mappings_closed=self._mappings_closed,
                mapping_limit_rejections=self._mapping_limit_rejections,
                sync_starts=self._sync_starts,
                sync_ends=self._sync_ends,
                sync_failures=self._sync_failures,
                render_latency_samples=self._render_latency_samples,
                render_latency_last_ns=self._render_latency_last_ns,
                render_latency_max_ns=self._render_latency_max_ns,
                render_latency_total_ns=self._render_latency_total_ns,
                render_latency_bucket_bounds_ns=list(_LATENCY_BUCKET_BOUNDS_NS),
                render_latency_bucket_counts=list(self._render_latency_bucket_counts),
                last_error=self._last_error,
            )


def _member(target: object, name: str) -> object:
    try:
        return getattr(target, name)
    except AttributeError as error:
        raise NativeOverlayContractError(f"native overlay dependency lacks {name}") from error


def _int_member(target: object, name: str) -> int:
    value = _member(target, name)
    if isinstance(value, bool):
        raise NativeOverlayContractError(f"native overlay {name} is invalid")
    return int(cast(SupportsInt, value))


def _extract_dmabuf_frame(
    gstallocators: object,
    gstvideo: object,
    pad: object,
    buffer: object,
) -> NativeDmabufFrame:
    """Extract one exact frame contract while its transient fds remain valid."""

    caps = cast(Callable[[], object | None], _member(pad, "get_current_caps"))()
    if caps is None:
        raise NativeOverlayContractError("native overlay source caps are unavailable")
    caps_size = int(
        cast(SupportsInt, cast(Callable[[], object], _member(caps, "get_size"))())
    )
    if caps_size != 1:
        raise NativeOverlayContractError("native overlay caps structure count differs")
    structure = cast(Callable[[int], object], _member(caps, "get_structure"))(0)
    features = cast(Callable[[int], object], _member(caps, "get_features"))(0)
    get_value = cast(Callable[[str], object], _member(structure, "get_value"))
    media_type = str(cast(Callable[[], object], _member(structure, "get_name"))())
    feature_text = str(cast(Callable[[], object], _member(features, "to_string"))())

    memory_count = int(
        cast(SupportsInt, cast(Callable[[], object], _member(buffer, "n_memory"))())
    )
    if memory_count != 2:
        raise NativeOverlayContractError("native overlay buffer memory count differs")
    peek_memory = cast(Callable[[int], object], _member(buffer, "peek_memory"))
    is_dmabuf = cast(
        Callable[[object], object],
        _member(gstallocators, "is_dmabuf_memory"),
    )
    is_fd_memory = cast(
        Callable[[object], object],
        _member(gstallocators, "is_fd_memory"),
    )
    get_fd = cast(
        Callable[[object], object],
        _member(gstallocators, "dmabuf_memory_get_fd"),
    )
    geometries: list[DmabufMemoryGeometry] = []
    for index in range(2):
        memory = peek_memory(index)
        allocator = _member(memory, "allocator")
        allocator_name = (
            ""
            if allocator is None
            else str(_member(allocator, "name"))
        )
        sizes = tuple(
            int(cast(SupportsInt, value))
            for value in cast(
                tuple[object, ...],
                cast(Callable[[], object], _member(memory, "get_sizes"))(),
            )
        )
        if len(sizes) != 3:
            raise NativeOverlayContractError("native overlay memory geometry is invalid")
        dmabuf = bool(is_dmabuf(memory))
        fd = int(cast(SupportsInt, get_fd(memory))) if dmabuf else -1
        geometries.append(
            DmabufMemoryGeometry(
                allocator_name=allocator_name,
                is_dmabuf=dmabuf,
                is_fd_memory=bool(is_fd_memory(memory)),
                fd=fd,
                size=sizes[0],
                offset=sizes[1],
                maxsize=sizes[2],
            )
        )
    video_meta = cast(
        Callable[[object], object | None],
        _member(gstvideo, "buffer_get_video_meta"),
    )(buffer)
    if video_meta is None:
        raise NativeOverlayContractError("native overlay GstVideoMeta is unavailable")
    offsets = tuple(
        int(cast(SupportsInt, value))
        for value in cast(tuple[object, ...], _member(video_meta, "offset"))
    )
    strides = tuple(
        int(cast(SupportsInt, value))
        for value in cast(tuple[object, ...], _member(video_meta, "stride"))
    )
    if len(offsets) != 4 or len(strides) != 4:
        raise NativeOverlayContractError("native overlay GstVideoMeta arrays differ")
    # Validation remains in ``NativeNv12OverlayCore.render`` so direct callers
    # and this GI extraction path share exactly one authoritative check.
    return NativeDmabufFrame(
        caps_features=feature_text,
        caps_media_type=media_type,
        caps_width=int(cast(SupportsInt, get_value("width"))),
        caps_height=int(cast(SupportsInt, get_value("height"))),
        caps_format=str(get_value("format")),
        caps_framerate=str(get_value("framerate")),
        buffer_size=int(
            cast(
                SupportsInt,
                cast(Callable[[], object], _member(buffer, "get_size"))(),
            )
        ),
        buffer_memory_count=memory_count,
        buffer_all_memory_writable=bool(
            cast(
                Callable[[], object],
                _member(buffer, "is_all_memory_writable"),
            )()
        ),
        memories=cast(
            tuple[DmabufMemoryGeometry, DmabufMemoryGeometry],
            tuple(geometries),
        ),
        video_meta_width=_int_member(video_meta, "width"),
        video_meta_height=_int_member(video_meta, "height"),
        video_meta_planes=_int_member(video_meta, "n_planes"),
        video_meta_offsets=offsets,
        video_meta_strides=strides,
    )


class GstDmabufOverlayRenderer:
    """One fail-isolated probe owned by a single recorder pipeline."""

    def __init__(
        self,
        gst: object,
        gstallocators: object,
        gstvideo: object,
        *,
        core: NativeNv12OverlayCore | None = None,
    ) -> None:
        self._gst = gst
        self._gstallocators = gstallocators
        self._gstvideo = gstvideo
        self._core = core or NativeNv12OverlayCore()
        self._pad: object | None = None
        self._probe_id: object | None = None
        self._condition = Condition()
        self._callbacks_active = 0
        self._closing = False
        self._closed = False

    def attach(self, camera: object) -> None:
        if self._pad is not None or self._closed:
            raise NativeOverlayContractError("native overlay probe ownership is invalid")
        pad = cast(
            Callable[[str], object | None],
            _member(camera, "get_static_pad"),
        )("src")
        if pad is None:
            raise NativeOverlayContractError("camera has no source pad for native overlay")
        probe_type = _member(_member(self._gst, "PadProbeType"), "BUFFER")
        probe_id = cast(
            Callable[[object, Callable[..., object]], object],
            _member(pad, "add_probe"),
        )(probe_type, self._probe)
        if probe_id is None or probe_id == 0:
            raise NativeOverlayContractError("native overlay probe installation failed")
        self._pad = pad
        self._probe_id = probe_id

    def _probe(self, pad: object, info: object) -> object:
        probe_return = _member(_member(self._gst, "PadProbeReturn"), "OK")
        with self._condition:
            if self._closing:
                return probe_return
            self._callbacks_active += 1
        try:
            if not self._core.requires_frame_inspection():
                self._core.note_passthrough_frame()
                return probe_return
            buffer = cast(
                Callable[[], object | None],
                _member(info, "get_buffer"),
            )()
            if buffer is None:
                raise NativeOverlayContractError(
                    "native overlay buffer probe received no buffer"
                )
            frame = _extract_dmabuf_frame(
                self._gstallocators,
                self._gstvideo,
                pad,
                buffer,
            )
            self._core.render(frame)
        except Exception as error:
            if self._core.snapshot().state != "ISOLATED":
                self._core.isolate(error)
        finally:
            with self._condition:
                self._callbacks_active -= 1
                if self._callbacks_active == 0:
                    self._condition.notify_all()
        return probe_return

    def set_text(self, text: str | None) -> None:
        self._core.set_text(text)

    def snapshot(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self._core.snapshot()))

    def close(self, timeout_s: float = 2.0) -> None:
        """Detach first, then close mappings only after every callback exits."""

        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or timeout_s <= 0
        ):
            raise NativeOverlayContractError("native overlay close timeout is invalid")
        with self._condition:
            if self._closed:
                return
            first_close = not self._closing
            self._closing = True
            pad, probe_id = self._pad, self._probe_id
            self._pad = None
            self._probe_id = None
        if first_close and pad is not None and probe_id is not None:
            cast(
                Callable[[object], object],
                _member(pad, "remove_probe"),
            )(probe_id)
        deadline = time.monotonic() + float(timeout_s)
        with self._condition:
            while self._callbacks_active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeOverlayContractError(
                        "native overlay callback shutdown timed out"
                    )
                self._condition.wait(remaining)
            self._closed = True
        self._core.close()


def validate_native_overlay_dependencies(
    gst: object,
    gstallocators: object,
    gstvideo: object,
) -> None:
    """Fail before camera startup if any required GI surface is unavailable."""

    _member(_member(gst, "PadProbeType"), "BUFFER")
    _member(_member(gst, "PadProbeReturn"), "OK")
    _member(gstallocators, "is_dmabuf_memory")
    _member(gstallocators, "is_fd_memory")
    _member(gstallocators, "dmabuf_memory_get_fd")
    _member(gstvideo, "buffer_get_video_meta")


__all__ = [
    "DMABUF_IOCTL_SYNC",
    "DMABUF_SYNC_END_WRITE",
    "DMABUF_SYNC_START_WRITE",
    "MAX_DMABUF_MAPPINGS",
    "NV12_BUFFER_SIZE",
    "NV12_FRAME_HEIGHT",
    "NV12_FRAME_WIDTH",
    "DmabufMemoryGeometry",
    "GstDmabufOverlayRenderer",
    "NativeDmabufFrame",
    "NativeNv12OverlayCore",
    "NativeOverlayContractError",
    "NativeOverlaySnapshot",
    "render_luma_bitmap",
    "validate_native_overlay_dependencies",
    "validate_native_overlay_text",
]
