from __future__ import annotations

from pathlib import Path

from scripts.check_import_boundaries import check_import_boundaries, main


def _write(root: Path, relative_path: str, source: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_domain_forbidden_dependencies_are_reported_with_source_locations(tmp_path: Path) -> None:
    bad_path = _write(
        tmp_path,
        "src/cad_harness/domain/bad.py",
        "\n".join(
            (
                "from mcp.server import FastMCP",
                "import win32com.client",
                "from sqlalchemy import select",
                "import ezdxf",
                "from apps.engineer_desktop import view_model",
            )
        ),
    )

    violations = check_import_boundaries(tmp_path)

    domain_violations = [item for item in violations if item.code.startswith("DOMAIN_")]
    assert [(item.code, item.line) for item in domain_violations] == [
        ("DOMAIN_MCP", 1),
        ("DOMAIN_COM_AUTOCAD", 2),
        ("DOMAIN_SQLALCHEMY", 3),
        ("DOMAIN_EZDXF", 4),
        ("DOMAIN_UI", 5),
    ]
    rendered = domain_violations[2].render(tmp_path)
    assert rendered.startswith("src/cad_harness/domain/bad.py:3: [DOMAIN_SQLALCHEMY]")
    assert "depend on a domain port instead" in rendered
    assert domain_violations[2].path == bad_path


def test_relative_internal_implementation_imports_cannot_bypass_domain_check(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/cad_harness/domain/ports/bad.py",
        "from ...adapters import autocad_com\n",
    )

    violations = check_import_boundaries(tmp_path)

    assert [(item.code, item.imported_module) for item in violations] == [
        ("DOMAIN_COM_AUTOCAD", "cad_harness.adapters.autocad_com")
    ]


def test_pywin32_is_allowed_only_in_autocad_com_adapter(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/cad_harness/adapters/autocad_com.py",
        "import pythoncom\nimport win32com.client\n",
    )
    _write(tmp_path, "apps/cli/bad.py", "from win32com import client\n")

    violations = check_import_boundaries(tmp_path)

    assert [(item.code, item.path.relative_to(tmp_path).as_posix()) for item in violations] == [
        ("PYWIN32_ISOLATION", "apps/cli/bad.py")
    ]
    assert "move pywin32 access behind" in violations[0].remediation


def test_cli_fails_closed_for_bad_import_and_parse_error(tmp_path: Path, capsys: object) -> None:
    _write(tmp_path, "src/cad_harness/domain/bad.py", "import PySide6\n")
    _write(tmp_path, "apps/cli/broken.py", "def broken(:\n")

    exit_code = main(["--root", str(tmp_path)])

    assert exit_code == 1
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Import boundary check failed with 2 violation(s)" in output
    assert "src/cad_harness/domain/bad.py:1: [DOMAIN_UI]" in output
    assert "apps/cli/broken.py:1: [BOUNDARY_PARSE]" in output


def test_current_repository_satisfies_import_boundaries(capsys: object) -> None:
    repository_root = Path(__file__).resolve().parents[2]

    assert main(["--root", str(repository_root)]) == 0
    assert capsys.readouterr().out == "Import boundary check passed.\n"  # type: ignore[attr-defined]
