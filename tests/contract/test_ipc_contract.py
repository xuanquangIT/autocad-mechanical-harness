"""Static cross-language checks for the monomorphic bridge IPC contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cad_harness.adapters.dotnet_bridge import build_request
from cad_harness.domain.models.base import SCHEMA_VERSION

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = _ROOT / "contracts" / "ipc-envelope.schema.json"
_CSHARP = _ROOT / "dotnet" / "AutoCADBridge" / "CadBridge.Contracts" / "IpcEnvelope.cs"


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _enum_members(source: str, enum_name: str) -> tuple[str, ...]:
    match = re.search(rf"public enum {enum_name}\s*\{{(?P<body>.*?)\}}", source, re.DOTALL)
    assert match is not None
    return tuple(
        _snake_case(item.strip().rstrip(","))
        for item in match.group("body").splitlines()
        if item.strip() and not item.strip().startswith("//")
    )


def test_csharp_ipc_models_match_published_schema() -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    source = _CSHARP.read_text(encoding="utf-8")
    request_schema = schema["$defs"]["request"]
    response_schema = schema["$defs"]["response"]
    error_schema = schema["$defs"]["error"]

    assert _enum_members(source, "IpcMethod") == tuple(
        request_schema["properties"]["method"]["enum"]
    )
    assert _enum_members(source, "IpcResponseStatus") == tuple(
        response_schema["properties"]["status"]["enum"]
    )
    json_names = set(re.findall(r'JsonPropertyName\("([a-z_]+)"\)', source))
    expected_names = {
        *request_schema["properties"],
        *response_schema["properties"],
        *error_schema["properties"],
    }
    assert json_names == expected_names
    assert "UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow" in source
    assert "JsonStringEnumConverter" in source


def test_python_and_csharp_publish_same_schema_version_and_cancel_shape() -> None:
    source = _CSHARP.read_text(encoding="utf-8")
    declared = re.search(r'CurrentSchemaVersion = "(?P<version>\d+\.\d+)"', source)
    assert declared is not None
    assert declared.group("version") == SCHEMA_VERSION

    request = build_request(
        "cancel",
        {"target_request_id": "request-target"},
        request_id="request-control",
    )
    assert request == {
        "schema_version": SCHEMA_VERSION,
        "method": "cancel",
        "request_id": "request-control",
        "job_id": None,
        "idempotency_key": None,
        "params": {"target_request_id": "request-target"},
    }
