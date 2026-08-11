"""Real STDIO regression for the prestarted pure-work process broker."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cad_harness.domain.models.envelope import ToolResponse


def _model() -> dict[str, object]:
    return {
        "document_id": "stdio-broker.dxf",
        "revision": "sha256:stdio-broker",
        "display_name": "stdio-broker.dxf",
        "source_unit_code": "mm",
        "to_mm_factor": 1.0,
        "geometry_normalized": True,
        "scope": {"kind": "model_space"},
        "entities": [],
        "arc_chord_tolerance_mm": 0.01,
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spawn/STDIO regression")
def test_measurement_worker_is_prestarted_before_real_stdio_transport(tmp_path: Path) -> None:
    config = tmp_path / "stdio.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "adapter": {"type": "fake"},
                "measure": {"timeout_seconds": 10},
                "storage": {
                    "sqlite_path": str(tmp_path / "harness.db"),
                    "preview_directory": str(tmp_path / "previews"),
                    "checkpoint_directory": str(tmp_path / "checkpoints"),
                },
                "observability": {"log_level": "ERROR"},
            }
        ),
        encoding="utf-8",
    )

    async def exercise() -> ToolResponse:
        environment = dict(os.environ)
        environment["CAD_HARNESS_CONFIG"] = str(config)
        environment["CAD_HARNESS_ADAPTER"] = "fake"
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "apps.mcp_server"],
            cwd=str(Path.cwd()),
            env=environment,
            encoding="utf-8",
            encoding_error_handler="strict",
        )
        async with stdio_client(server) as (reader, writer):  # noqa: SIM117
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool(
                        "cad_measure",
                        {
                            "model": _model(),
                            "request": {
                                "kind": "point_to_point",
                                "first_point_mm": [0.0, 0.0],
                                "second_point_mm": [3.0, 4.0],
                            },
                        },
                    ),
                    timeout=15.0,
                )
                assert result.structuredContent is not None
                return ToolResponse.model_validate(result.structuredContent)

    response = asyncio.run(exercise())

    assert response.status.value == "ok"
    assert response.data["value"] == 5.0
    assert response.data["unit"] == "mm"
