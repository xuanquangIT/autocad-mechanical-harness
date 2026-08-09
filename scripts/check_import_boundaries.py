"""Fail CI when production imports cross the harness architecture boundaries."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_PRODUCTION_ROOTS = (Path("src/cad_harness"), Path("apps"), Path("scripts"))
_DOMAIN_ROOT = Path("src/cad_harness/domain")
_AUTOCAD_COM_ADAPTER = Path("src/cad_harness/adapters/autocad_com.py")
_PYWIN32_MODULES = ("win32com", "pythoncom")

# Import prefixes include both third-party libraries and in-repository entry points.  The
# latter prevents the domain from reaching a forbidden dependency indirectly through an
# implementation module.
_DOMAIN_FORBIDDEN_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MCP", ("mcp", "fastmcp", "apps.mcp_server")),
    (
        "COM_AUTOCAD",
        (
            "win32com",
            "pythoncom",
            "comtypes",
            "autocad",
            "autodesk",
            "cad_harness.adapters.autocad_com",
            "cad_harness.adapters.com_drawing_reader",
            "cad_harness.adapters.dotnet_bridge",
            "cad_harness.adapters.bridge_drawing_reader",
        ),
    ),
    ("SQLALCHEMY", ("sqlalchemy", "cad_harness.persistence")),
    ("EZDXF", ("ezdxf", "cad_harness.adapters.dxf_drawing_reader")),
    (
        "UI",
        (
            "pyside6",
            "pyqt5",
            "pyqt6",
            "tkinter",
            "wx",
            "apps.engineer_desktop",
        ),
    ),
)


@dataclass(frozen=True)
class BoundaryViolation:
    """One actionable import-boundary failure."""

    path: Path
    line: int
    code: str
    imported_module: str
    remediation: str

    def render(self, repository_root: Path) -> str:
        """Render a stable compiler-style diagnostic."""
        try:
            display_path = self.path.relative_to(repository_root)
        except ValueError:
            display_path = self.path
        return (
            f"{display_path.as_posix()}:{self.line}: [{self.code}] import "
            f"'{self.imported_module}' is forbidden; {self.remediation}"
        )


def _matches_prefix(module: str, prefix: str) -> bool:
    normalized = module.casefold()
    forbidden = prefix.casefold()
    return normalized == forbidden or normalized.startswith(f"{forbidden}.")


def _module_name(path: Path, repository_root: Path) -> tuple[str, bool]:
    source_root = repository_root / "src"
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        relative = path.relative_to(repository_root)
    parts = list(relative.with_suffix("").parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _resolve_from_module(
    node: ast.ImportFrom,
    *,
    current_module: str,
    current_is_package: bool,
) -> str:
    if node.level == 0:
        return node.module or ""

    package_parts = current_module.split(".") if current_module else []
    if not current_is_package and package_parts:
        package_parts.pop()
    parents_to_remove = node.level - 1
    if parents_to_remove:
        package_parts = package_parts[:-parents_to_remove]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def _imported_modules(
    node: ast.Import | ast.ImportFrom,
    *,
    current_module: str,
    current_is_package: bool,
) -> Iterable[str]:
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
        return

    base = _resolve_from_module(
        node,
        current_module=current_module,
        current_is_package=current_is_package,
    )
    if base:
        yield base
    for alias in node.names:
        if alias.name != "*":
            yield f"{base}.{alias.name}" if base else alias.name


def _python_files(repository_root: Path) -> list[Path]:
    files: set[Path] = set()
    for relative_root in _PRODUCTION_ROOTS:
        production_root = repository_root / relative_root
        if production_root.is_dir():
            files.update(path for path in production_root.rglob("*.py") if path.is_file())
    return sorted(files, key=lambda path: path.as_posix().casefold())


def _parse_file(path: Path) -> tuple[ast.Module | None, BoundaryViolation | None]:
    try:
        source = path.read_text(encoding="utf-8")
        return ast.parse(source, filename=str(path)), None
    except (OSError, UnicodeError, SyntaxError) as exc:
        line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno else 1
        return None, BoundaryViolation(
            path=path,
            line=line,
            code="BOUNDARY_PARSE",
            imported_module="<unavailable>",
            remediation=f"make this production module parseable ({exc})",
        )


def check_import_boundaries(repository_root: Path) -> list[BoundaryViolation]:
    """Return all forbidden imports in production Python beneath *repository_root*."""
    repository_root = repository_root.resolve()
    violations: list[BoundaryViolation] = []
    domain_root = repository_root / _DOMAIN_ROOT
    allowed_pywin32_path = repository_root / _AUTOCAD_COM_ADAPTER

    for path in _python_files(repository_root):
        tree, parse_error = _parse_file(path)
        if parse_error is not None:
            violations.append(parse_error)
            continue
        assert tree is not None
        current_module, current_is_package = _module_name(path, repository_root)
        is_domain = path.is_relative_to(domain_root)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported_modules = tuple(
                _imported_modules(
                    node,
                    current_module=current_module,
                    current_is_package=current_is_package,
                )
            )
            if path != allowed_pywin32_path:
                pywin32_import = next(
                    (
                        module
                        for module in imported_modules
                        if any(_matches_prefix(module, prefix) for prefix in _PYWIN32_MODULES)
                    ),
                    None,
                )
                if pywin32_import is not None:
                    violations.append(
                        BoundaryViolation(
                            path=path,
                            line=node.lineno,
                            code="PYWIN32_ISOLATION",
                            imported_module=pywin32_import,
                            remediation=(
                                "move pywin32 access behind cad_harness.adapters.autocad_com"
                            ),
                        )
                    )

            if not is_domain:
                continue
            for category, prefixes in _DOMAIN_FORBIDDEN_PREFIXES:
                forbidden_import = next(
                    (
                        module
                        for module in imported_modules
                        if any(_matches_prefix(module, prefix) for prefix in prefixes)
                    ),
                    None,
                )
                if forbidden_import is not None:
                    violations.append(
                        BoundaryViolation(
                            path=path,
                            line=node.lineno,
                            code=f"DOMAIN_{category}",
                            imported_module=forbidden_import,
                            remediation="depend on a domain port instead",
                        )
                    )
                    break

    unique = {
        (item.path, item.line, item.code, item.imported_module, item.remediation): item
        for item in violations
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item.path.as_posix().casefold(),
            item.line,
            item.code,
            item.imported_module.casefold(),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args(argv)
    repository_root = args.root.resolve()
    if not repository_root.is_dir():
        parser.error(f"repository root is not a directory: {repository_root}")

    violations = check_import_boundaries(repository_root)
    if violations:
        print(f"Import boundary check failed with {len(violations)} violation(s):")
        for violation in violations:
            print(violation.render(repository_root))
        return 1

    print("Import boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
