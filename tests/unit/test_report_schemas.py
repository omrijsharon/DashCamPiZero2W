from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"
_REPORT_ROOT = Path(__file__).resolve().parents[2] / "docs" / "test-reports"


def _load_schema(name: str) -> dict[str, Any]:
    with (_SCHEMA_ROOT / name).open(encoding="utf-8") as schema_file:
        return cast(dict[str, Any], json.load(schema_file))


def _load_document(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as document_file:
        return cast(dict[str, Any], json.load(document_file))


def _validator(name: str) -> Draft202012Validator:
    schema = _load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _producer() -> dict[str, str]:
    return {
        "name": "dashcam-test",
        "version": "0.1.0.dev0",
        "build_id": "local-test",
    }


def _test_result() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_utc": "2026-07-24T12:00:00Z",
        "producer": _producer(),
        "status": "passed",
        "suite": {
            "id": "unit",
            "name": "Local unit tests",
            "run_id": "run-001",
        },
        "summary": {
            "duration_ms": 10,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "skipped": 0,
        },
        "cases": [],
        "evidence_artifacts": [],
        "measurements": [],
        "warnings": [],
        "environment": {
            "kind": "local",
            "os": "test-os",
            "architecture": "test-architecture",
            "python_version": "3.12",
        },
    }


def _not_probed_section() -> dict[str, Any]:
    return {
        "state": "not_probed",
        "facts": [],
        "evidence_artifact_paths": [],
        "warnings": [],
    }


def _capability_report() -> dict[str, Any]:
    section_names = (
        "target_identity",
        "os",
        "hardware",
        "media",
        "audio",
        "uart",
        "storage",
        "thermal",
    )
    return {
        "schema_version": 1,
        "generated_at_utc": "2026-07-24T12:00:00Z",
        "producer": _producer(),
        "status": "not_probed",
        "target": {
            "kind": "local_fixture",
            "identity_state": "not_probed",
        },
        "raw_observations": {name: _not_probed_section() for name in section_names},
        "evaluated_capabilities": [],
        "evidence_artifacts": [],
        "warnings": [],
    }


@pytest.mark.parametrize(
    ("schema_name", "document"),
    [
        ("test-result-v1.schema.json", _test_result()),
        ("capability-report-v1.schema.json", _capability_report()),
    ],
)
def test_report_schema_and_minimal_document_are_valid(
    schema_name: str, document: dict[str, Any]
) -> None:
    _validator(schema_name).validate(document)


def test_test_result_rejects_path_traversal() -> None:
    document = copy.deepcopy(_test_result())
    document["evidence_artifacts"] = [
        {
            "path": "../private.env",
            "kind": "text",
        }
    ]

    with pytest.raises(ValidationError):
        _validator("test-result-v1.schema.json").validate(document)


def test_capability_report_rejects_unbounded_extra_fields() -> None:
    document = copy.deepcopy(_capability_report())
    document["environment_dump"] = {"AP_PASSWORD": "must-not-be-recorded"}

    with pytest.raises(ValidationError):
        _validator("capability-report-v1.schema.json").validate(document)


def test_committed_test_result_reports_validate() -> None:
    reports = sorted(_REPORT_ROOT.glob("*-test-result-v1.json"))
    assert reports, "at least one machine-readable local test report is required"

    validator = _validator("test-result-v1.schema.json")
    for report in reports:
        validator.validate(_load_document(report))
