"""Versioned durable-intent model and idempotent reconciliation planner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, cast
from uuid import UUID

from dashcam.storage.naming import ClipNameError, validate_filename_component

_SCHEMA_VERSION: Final = 1
_MAX_RELATIVE_PATH_CHARS: Final = 240


class IntentKind(StrEnum):
    """Cross-filesystem operation whose intent is persisted on ext4."""

    FINALIZE = "FINALIZE"
    RECONCILE_NAME = "RECONCILE_NAME"
    PROTECT = "PROTECT"
    UNPROTECT = "UNPROTECT"
    DELETE = "DELETE"


class PairMember(StrEnum):
    """The two independently mutated members of a logical clip."""

    VIDEO = "VIDEO"
    SIDECAR = "SIDECAR"


class ActionKind(StrEnum):
    """A filesystem action requested by the pure planner."""

    MOVE = "MOVE"
    UNLINK = "UNLINK"


@dataclass(frozen=True, slots=True)
class PairPaths:
    """Source and optional target paths for both clip members."""

    video_source: str
    sidecar_source: str
    video_target: str | None = None
    sidecar_target: str | None = None

    def __post_init__(self) -> None:
        for path in (
            self.video_source,
            self.sidecar_source,
            self.video_target,
            self.sidecar_target,
        ):
            if path is not None:
                _validate_relative_path(path)
        if self.video_source == self.sidecar_source:
            raise ValueError("video and sidecar source paths must differ")
        if (
            self.video_target is not None
            and self.sidecar_target is not None
            and self.video_target == self.sidecar_target
        ):
            raise ValueError("video and sidecar target paths must differ")


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """Serializable intent written before exFAT pair mutations begin."""

    intent_id: UUID
    clip_id: UUID
    kind: IntentKind
    created_monotonic_ns: int
    paths: PairPaths
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, UUID) or not isinstance(self.clip_id, UUID):
            raise TypeError("intent_id and clip_id must be UUID values")
        if not isinstance(self.kind, IntentKind):
            raise TypeError("kind must be an IntentKind")
        if not isinstance(self.paths, PairPaths):
            raise TypeError("paths must be PairPaths")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != _SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported intent schema version: {self.schema_version}")
        if isinstance(self.created_monotonic_ns, bool) or not isinstance(
            self.created_monotonic_ns, int
        ):
            raise TypeError("created_monotonic_ns must be an integer")
        if self.created_monotonic_ns < 0:
            raise ValueError("created_monotonic_ns cannot be negative")
        targets = (self.paths.video_target, self.paths.sidecar_target)
        if self.kind is IntentKind.DELETE:
            if targets != (None, None):
                raise ValueError("delete intent cannot contain target paths")
        elif None in targets:
            raise ValueError(f"{self.kind} intent requires both target paths")

    def as_dict(self) -> dict[str, object]:
        """Return a closed JSON-serializable representation."""

        return {
            "schema_version": self.schema_version,
            "intent_id": str(self.intent_id),
            "clip_id": str(self.clip_id),
            "kind": self.kind.value,
            "created_monotonic_ns": self.created_monotonic_ns,
            "paths": {
                "video_source": self.paths.video_source,
                "sidecar_source": self.paths.sidecar_source,
                "video_target": self.paths.video_target,
                "sidecar_target": self.paths.sidecar_target,
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> OperationIntent:
        """Parse a strict representation and reject unknown/missing fields."""

        _require_exact_keys(
            raw,
            {
                "schema_version",
                "intent_id",
                "clip_id",
                "kind",
                "created_monotonic_ns",
                "paths",
            },
        )
        raw_paths = raw["paths"]
        if not isinstance(raw_paths, dict):
            raise TypeError("paths must be an object")
        path_values = cast(dict[str, object], raw_paths)
        _require_exact_keys(
            path_values,
            {"video_source", "sidecar_source", "video_target", "sidecar_target"},
        )

        return cls(
            schema_version=_strict_int(raw["schema_version"], "schema_version"),
            intent_id=UUID(_strict_str(raw["intent_id"], "intent_id")),
            clip_id=UUID(_strict_str(raw["clip_id"], "clip_id")),
            kind=IntentKind(_strict_str(raw["kind"], "kind")),
            created_monotonic_ns=_strict_int(raw["created_monotonic_ns"], "created_monotonic_ns"),
            paths=PairPaths(
                video_source=_strict_str(path_values["video_source"], "video_source"),
                sidecar_source=_strict_str(path_values["sidecar_source"], "sidecar_source"),
                video_target=_optional_str(path_values["video_target"], "video_target"),
                sidecar_target=_optional_str(path_values["sidecar_target"], "sidecar_target"),
            ),
        )


@dataclass(frozen=True, slots=True)
class MemberObservation:
    """Observed source/target existence for one pair member."""

    source_exists: bool
    target_exists: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source_exists, bool) or not isinstance(self.target_exists, bool):
            raise TypeError("member observations must be boolean")


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    """One idempotently re-plannable filesystem action."""

    kind: ActionKind
    member: PairMember
    source: str
    target: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationProblem:
    """A condition requiring quarantine or explicit operator policy."""

    member: PairMember
    code: str


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """Pure plan generated from durable intent plus current observations."""

    actions: tuple[ReconciliationAction, ...]
    problems: tuple[ReconciliationProblem, ...]
    completed_members: tuple[PairMember, ...]

    @property
    def complete(self) -> bool:
        """Return whether both members reached the intended final state."""

        return (
            not self.actions
            and not self.problems
            and set(self.completed_members) == {PairMember.VIDEO, PairMember.SIDECAR}
        )


def plan_reconciliation(
    intent: OperationIntent,
    *,
    video: MemberObservation,
    sidecar: MemberObservation,
) -> ReconciliationPlan:
    """Plan the next safe pair-operation steps without touching a filesystem."""

    actions: list[ReconciliationAction] = []
    problems: list[ReconciliationProblem] = []
    completed: list[PairMember] = []

    observations = (
        (PairMember.VIDEO, video, intent.paths.video_source, intent.paths.video_target),
        (
            PairMember.SIDECAR,
            sidecar,
            intent.paths.sidecar_source,
            intent.paths.sidecar_target,
        ),
    )
    for member, observation, source, target in observations:
        if intent.kind is IntentKind.DELETE:
            if observation.target_exists:
                problems.append(ReconciliationProblem(member, "UNEXPECTED_DELETE_TARGET"))
            elif observation.source_exists:
                actions.append(ReconciliationAction(ActionKind.UNLINK, member, source, None))
            else:
                completed.append(member)
            continue

        if observation.source_exists and observation.target_exists:
            problems.append(ReconciliationProblem(member, "SOURCE_TARGET_CONFLICT"))
        elif observation.source_exists:
            actions.append(ReconciliationAction(ActionKind.MOVE, member, source, target))
        elif observation.target_exists:
            completed.append(member)
        else:
            problems.append(ReconciliationProblem(member, "SOURCE_AND_TARGET_MISSING"))

    return ReconciliationPlan(tuple(actions), tuple(problems), tuple(completed))


def _validate_relative_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > _MAX_RELATIVE_PATH_CHARS
        or not path.isascii()
    ):
        raise ValueError("relative path has invalid length")
    if "\\" in path or ":" in path or "//" in path:
        raise ValueError("relative path must use Windows-safe forward-slash syntax")
    raw_parts = path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path must remain below the managed recording root")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        raise ValueError("path must remain below the managed recording root")
    if any(part.rstrip(" .") != part for part in raw_parts):
        raise ValueError("path components cannot end in a dot or space")
    try:
        for part in raw_parts:
            validate_filename_component(part)
    except ClipNameError as exc:
        raise ValueError("path contains a non-portable component") from exc


def _require_exact_keys(raw: dict[str, object], expected: set[str]) -> None:
    actual = set(raw)
    if actual != expected:
        raise ValueError(
            f"object keys differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _strict_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _strict_str(value, field)


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value
