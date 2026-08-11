"""Open-source metadata must remain complete and internally consistent."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COMMUNITY_FILES = {
    "CHANGELOG.md",
    "CITATION.cff",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "ROADMAP.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/dependabot.yml",
}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_required_open_source_files_are_present() -> None:
    missing = sorted(
        path for path in REQUIRED_COMMUNITY_FILES if not (REPOSITORY_ROOT / path).is_file()
    )

    assert missing == []


def test_package_metadata_declares_apache_license() -> None:
    payload = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]

    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) == {"LICENSE", "NOTICE"}
    assert "License :: OSI Approved :: Apache Software License" in project["classifiers"]


def test_license_and_readme_do_not_contradict_package_metadata() -> None:
    license_text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Apache License" in license_text
    assert "Version 2.0" in license_text
    assert "Proprietary. Internal use only." not in readme
    assert "Apache License 2.0" in readme


def test_yaml_community_metadata_is_parseable() -> None:
    paths = (
        "CITATION.cff",
        ".github/dependabot.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
    )

    for relative_path in paths:
        payload = yaml.safe_load((REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8"))
        assert isinstance(payload, dict), relative_path

    citation = yaml.safe_load((REPOSITORY_ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    assert citation["license"] == "Apache-2.0"
    assert citation["repository-code"].endswith("/autocad-mechanical-harness")


def test_readme_local_links_resolve() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    unresolved: list[str] = []

    for target in MARKDOWN_LINK.findall(readme):
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        relative_path = target.split("#", maxsplit=1)[0]
        if relative_path and not (REPOSITORY_ROOT / relative_path).exists():
            unresolved.append(target)

    assert unresolved == []
