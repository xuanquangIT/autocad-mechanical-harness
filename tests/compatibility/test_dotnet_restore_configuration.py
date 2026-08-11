"""Compatibility checks for the isolated AutoCAD bridge NuGet configuration."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NUGET_CONFIG = REPOSITORY_ROOT / "dotnet" / "AutoCADBridge" / "NuGet.Config"


def test_test_host_transitive_packages_are_mapped_to_nuget_org() -> None:
    root = ElementTree.parse(NUGET_CONFIG).getroot()
    nuget_org = root.find("./packageSourceMapping/packageSource[@key='nuget.org']")

    assert nuget_org is not None
    patterns = {package.attrib["pattern"] for package in nuget_org.findall("package")}
    # Microsoft.NET.Test.Sdk's test host restores these non-Microsoft-prefixed packages.
    assert {"Newtonsoft.Json", "System.Reflection.Metadata"} <= patterns
