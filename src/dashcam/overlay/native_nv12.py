"""Deterministic fixed-region renderer for the production NV12 video path.

The GStreamer element is registered lazily because non-Pi control-plane
processes do not have PyGObject.  All rendering and buffer-write policy lives
in :class:`NativeNv12OverlayCore`, which is independent of GStreamer and can be
tested on every supported development host.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Final, Protocol, SupportsInt, cast

from dashcam.overlay.formatting import OVERLAY_1080P_LAYOUT, OverlayLayout

NATIVE_OVERLAY_FACTORY: Final = "dashcamnv12overlay"
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
_MAX_ERROR_CHARS: Final = 256
_UNSET: Final = object()


class NativeOverlayContractError(ValueError):
    """The native overlay received input outside its fixed production contract."""


class WritableBuffer(Protocol):
    """The bounded Gst.Buffer subset used by the renderer core."""

    def get_size(self) -> int: ...

    def fill(self, offset: int, data: bytes) -> int: ...


@dataclass(frozen=True, slots=True)
class Nv12FrameLayout:
    """Exact tightly packed NV12 layout accepted by the production transform."""

    width: int
    height: int
    format: str
    buffer_size: int
    y_stride: int
    y_offset: int
    uv_stride: int
    uv_offset: int

    def validate(self) -> None:
        if (
            self.width,
            self.height,
            self.format,
            self.buffer_size,
            self.y_stride,
            self.y_offset,
            self.uv_stride,
            self.uv_offset,
        ) != (
            NV12_FRAME_WIDTH,
            NV12_FRAME_HEIGHT,
            NV12_FORMAT,
            NV12_BUFFER_SIZE,
            NV12_Y_STRIDE,
            NV12_Y_OFFSET,
            NV12_UV_STRIDE,
            NV12_UV_OFFSET,
        ):
            raise NativeOverlayContractError(
                "overlay requires tightly packed 1920x1080 NV12"
            )


@dataclass(frozen=True, slots=True)
class NativeOverlaySnapshot:
    """Bounded, coordinate-free accounting for one transform instance."""

    state: str
    caps_accepted: bool
    enabled: bool
    updates: int
    update_rejections: int
    frames_seen: int
    frames_rendered: int
    frames_passthrough: int
    bytes_written: int
    buffer_size_mismatches: int
    short_writes: int
    transform_failures: int
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


class NativeNv12OverlayCore:
    """Thread-safe cached renderer and fail-isolated per-buffer copy policy."""

    def __init__(self, layout: OverlayLayout = OVERLAY_1080P_LAYOUT) -> None:
        if not isinstance(layout, OverlayLayout):
            raise NativeOverlayContractError("native overlay requires an OverlayLayout")
        self._layout = layout
        self._lock = Lock()
        self._caps_accepted = False
        self._isolated = False
        self._text: str | object | None = _UNSET
        self._bitmap: bytes | None = None
        self._updates = 0
        self._update_rejections = 0
        self._frames_seen = 0
        self._frames_rendered = 0
        self._frames_passthrough = 0
        self._bytes_written = 0
        self._buffer_size_mismatches = 0
        self._short_writes = 0
        self._transform_failures = 0
        self._last_error: str | None = None

    def configure_layout(self, frame: Nv12FrameLayout) -> None:
        """Accept exactly one tightly packed production layout."""

        try:
            if not isinstance(frame, Nv12FrameLayout):
                raise NativeOverlayContractError("native overlay layout is invalid")
            frame.validate()
            layout = self._layout
            if (
                layout.origin_x_px + layout.region_width_px > frame.y_stride
                or layout.origin_y_px + layout.region_height_px > frame.height
                or layout.region_width_px != 1152
                or layout.region_height_px != 64
            ):
                raise NativeOverlayContractError(
                    "native overlay region differs from the measured 1152x64 contract"
                )
        except (NativeOverlayContractError, ValueError) as error:
            with self._lock:
                self._caps_accepted = False
                self._last_error = _bounded_error(error)
            raise
        with self._lock:
            self._caps_accepted = True
            self._last_error = None

    def set_text(self, text: str | None) -> None:
        """Pre-render changed text while retaining the last valid bitmap on refusal."""

        try:
            validate_native_overlay_text(text, self._layout)
            rendered = None if text is None else render_luma_bitmap(text, self._layout)
        except (NativeOverlayContractError, ValueError) as error:
            with self._lock:
                self._update_rejections += 1
                self._last_error = _bounded_error(error)
            raise
        with self._lock:
            if text == self._text:
                return
            self._text = text
            self._bitmap = rendered
            self._updates += 1
            if not self._isolated:
                self._last_error = None

    def transform(self, buffer: WritableBuffer) -> bool:
        """Copy the cached luma region, or isolate safely and pass the frame."""

        with self._lock:
            self._frames_seen += 1
            caps_accepted = self._caps_accepted
            isolated = self._isolated
            bitmap = self._bitmap
        if not caps_accepted:
            raise NativeOverlayContractError("native overlay received a buffer before exact caps")
        if isolated or bitmap is None:
            with self._lock:
                self._frames_passthrough += 1
            return False

        written = 0
        try:
            size = buffer.get_size()
            if isinstance(size, bool) or not isinstance(size, int) or size != NV12_BUFFER_SIZE:
                with self._lock:
                    self._buffer_size_mismatches += 1
                raise NativeOverlayContractError(
                    f"native overlay buffer size {size!r} differs from {NV12_BUFFER_SIZE}"
                )
            width = self._layout.region_width_px
            for row in range(self._layout.region_height_px):
                source_start = row * width
                chunk = bitmap[source_start : source_start + width]
                destination = (
                    NV12_Y_OFFSET
                    + (self._layout.origin_y_px + row) * NV12_Y_STRIDE
                    + self._layout.origin_x_px
                )
                count = buffer.fill(destination, chunk)
                if isinstance(count, bool) or not isinstance(count, int) or count != width:
                    with self._lock:
                        self._short_writes += 1
                    raise NativeOverlayContractError(
                        f"native overlay short write at row {row}: {count!r}"
                    )
                written += count
        except Exception as error:
            with self._lock:
                self._isolated = True
                self._transform_failures += 1
                self._frames_passthrough += 1
                self._bytes_written += written
                self._last_error = _bounded_error(error)
            return False

        with self._lock:
            self._frames_rendered += 1
            self._bytes_written += written
        return True

    def snapshot(self) -> NativeOverlaySnapshot:
        with self._lock:
            state = (
                "ISOLATED"
                if self._isolated
                else "UNCONFIGURED"
                if not self._caps_accepted
                else "SILENT"
                if self._bitmap is None
                else "ACTIVE"
            )
            return NativeOverlaySnapshot(
                state=state,
                caps_accepted=self._caps_accepted,
                enabled=self._bitmap is not None,
                updates=self._updates,
                update_rejections=self._update_rejections,
                frames_seen=self._frames_seen,
                frames_rendered=self._frames_rendered,
                frames_passthrough=self._frames_passthrough,
                bytes_written=self._bytes_written,
                buffer_size_mismatches=self._buffer_size_mismatches,
                short_writes=self._short_writes,
                transform_failures=self._transform_failures,
                last_error=self._last_error,
            )


def _member(target: object, name: str) -> object:
    try:
        return getattr(target, name)
    except AttributeError as error:
        raise NativeOverlayContractError(f"native overlay dependency lacks {name}") from error


def _video_info_layout(gstvideo: object, caps: object) -> Nv12FrameLayout:
    """Extract the exact GstVideo layout without retaining any frame object."""

    video_info_type = _member(gstvideo, "VideoInfo")
    new_from_caps = cast(Callable[[object], object], _member(video_info_type, "new_from_caps"))
    info = new_from_caps(caps)
    if info is None:
        raise NativeOverlayContractError("GstVideo rejected native overlay caps")
    structure = cast(Callable[[int], object], _member(caps, "get_structure"))(0)
    media_type = str(cast(Callable[[], object], _member(structure, "get_name"))())
    get_value = cast(Callable[[str], object], _member(structure, "get_value"))
    if media_type != "video/x-raw":
        raise NativeOverlayContractError("native overlay caps are not video/x-raw")
    strides = tuple(
        int(cast(SupportsInt, value))
        for value in cast(tuple[object, ...], _member(info, "stride"))
    )
    offsets = tuple(
        int(cast(SupportsInt, value))
        for value in cast(tuple[object, ...], _member(info, "offset"))
    )
    return Nv12FrameLayout(
        width=int(cast(int, _member(info, "width"))),
        height=int(cast(int, _member(info, "height"))),
        format=str(get_value("format")),
        buffer_size=int(cast(int, _member(info, "size"))),
        y_stride=strides[0],
        y_offset=offsets[0],
        uv_stride=strides[1],
        uv_offset=offsets[1],
    )


def register_native_nv12_overlay(
    gst: object,
    gstbase: object,
    gstvideo: object,
    gobject: object,
) -> None:
    """Register the dashcam-owned transform in the current process once."""

    element_factory = _member(gst, "ElementFactory")
    find = cast(Callable[[str], object | None], _member(element_factory, "find"))
    if find(NATIVE_OVERLAY_FACTORY) is not None:
        return

    base_transform = _member(gstbase, "BaseTransform")
    caps_type = _member(gst, "Caps")
    caps = cast(Callable[[str], object], _member(caps_type, "from_string"))(
        "video/x-raw,format=(string)NV12,width=(int)1920,height=(int)1080"
    )
    pad_template_type = _member(gst, "PadTemplate")
    new_template = cast(
        Callable[[str, object, object, object], object],
        _member(pad_template_type, "new"),
    )
    direction = _member(gst, "PadDirection")
    presence = _member(gst, "PadPresence")
    templates = (
        new_template("sink", _member(direction, "SINK"), _member(presence, "ALWAYS"), caps),
        new_template("src", _member(direction, "SRC"), _member(presence, "ALWAYS"), caps),
    )
    flow_return = _member(gst, "FlowReturn")

    class DashcamNativeNv12Overlay(base_transform):  # type: ignore[misc, valid-type]
        __gtype_name__ = "DashcamNativeNv12Overlay"
        __gstmetadata__ = (
            "Dashcam native NV12 overlay",
            "Filter/Effect/Video",
            "Copies one cached opaque luma region into exact production NV12 frames",
            "DashCamPiZero2W",
        )
        __gsttemplates__ = templates

        def __init__(self) -> None:
            super().__init__()
            self._overlay_core = NativeNv12OverlayCore()
            self.set_in_place(True)
            self.set_passthrough(True)

        def set_overlay_text(self, text: str | None) -> None:
            self._overlay_core.set_text(text)
            isolated = self._overlay_core.snapshot().state == "ISOLATED"
            self.set_passthrough(text is None or isolated)

        def overlay_snapshot(self) -> dict[str, object]:
            return cast(dict[str, object], asdict(self._overlay_core.snapshot()))

        def do_set_caps(self, input_caps: object, output_caps: object) -> bool:
            try:
                incoming = _video_info_layout(gstvideo, input_caps)
                outgoing = _video_info_layout(gstvideo, output_caps)
                if incoming != outgoing:
                    raise NativeOverlayContractError(
                        "native overlay input and output layouts differ"
                    )
                self._overlay_core.configure_layout(incoming)
                return True
            except Exception:
                return False

        def do_transform_ip(self, buffer: WritableBuffer) -> object:
            try:
                rendered = self._overlay_core.transform(buffer)
            except NativeOverlayContractError:
                return _member(flow_return, "NOT_NEGOTIATED")
            if (
                not rendered
                and self._overlay_core.snapshot().state == "ISOLATED"
            ):
                self.set_passthrough(True)
            return _member(flow_return, "OK")

    type_register = cast(Callable[[type[object]], object], _member(gobject, "type_register"))
    type_register(DashcamNativeNv12Overlay)
    element_type = _member(gst, "Element")
    register = cast(
        Callable[[object | None, str, object, type[object]], bool],
        _member(element_type, "register"),
    )
    rank = _member(gst, "Rank")
    if not register(
        None,
        NATIVE_OVERLAY_FACTORY,
        _member(rank, "NONE"),
        DashcamNativeNv12Overlay,
    ):
        raise NativeOverlayContractError("could not register native NV12 overlay")
    if find(NATIVE_OVERLAY_FACTORY) is None:
        raise NativeOverlayContractError("native NV12 overlay registration was not discoverable")


__all__ = [
    "NATIVE_OVERLAY_FACTORY",
    "NV12_BUFFER_SIZE",
    "NV12_FRAME_HEIGHT",
    "NV12_FRAME_WIDTH",
    "NativeNv12OverlayCore",
    "NativeOverlayContractError",
    "NativeOverlaySnapshot",
    "Nv12FrameLayout",
    "register_native_nv12_overlay",
    "render_luma_bitmap",
    "validate_native_overlay_text",
]
