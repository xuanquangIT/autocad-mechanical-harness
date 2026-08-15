from __future__ import annotations

from pathlib import Path

_PACKAGER = (
    Path(__file__).resolve().parents[2] / "dotnet" / "AutoCADBridge" / "Package-BridgeBundle.ps1"
)


def test_dotnet_discovery_iterates_each_path_candidate() -> None:
    source = _PACKAGER.read_text(encoding="utf-8")
    discovery = source[
        source.index("function Get-DotNetExecutable") : source.index(
            "function Assert-TargetCompatibility"
        )
    ]

    assert "Get-Command 'dotnet' -CommandType Application -All" in discovery
    assert "foreach ($command in $commands)" in discovery
    assert "$candidates.Add($command.Source)" in discovery
