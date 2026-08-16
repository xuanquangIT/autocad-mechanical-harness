"""Build the Python engineering-preview release bundle with checksums.

    uv run python scripts/package_release.py --out dist/

The AutoCAD bridge is built separately by ``Package-BridgeBundle.ps1`` because every
AutoCAD/runtime target has a distinct manifest and signing boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_METADATA = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
PROJECT_VERSION = str(PROJECT_METADATA["version"])
PROJECT_WHEEL_NAME = str(PROJECT_METADATA["name"]).replace("-", "_")

#: Files and directories shipped alongside the wheel.
PAYLOAD: tuple[str, ...] = (
    "config/base.yaml",
    "config/compatibility.yaml",
    "config/clients.yaml",
    "config/codex-local.yaml",
    "config/live-com-planning.yaml",
    "config/live-r26-acceptance.yaml",
    "config/pilot.yaml",
    ".env.example",
    "LICENSE",
    "NOTICE",
    "README.md",
    "docs/installation.md",
    "docs/operations.md",
    "docs/releases/v0.3.0.md",
    "docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.vn.md",
    "docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md",
    "contracts",
    "src/cad_harness/company_rules/profiles/demo-profile.yaml",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the Python engineering preview")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-build", action="store_true", help="Reuse an existing wheel")
    args = parser.parse_args()

    staging = args.out / "bundle"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    if not args.skip_build:
        result = subprocess.run(["uv", "build", "--out-dir", str(args.out)], cwd=ROOT, check=False)
        if result.returncode != 0:
            print("uv build failed", file=sys.stderr)
            return result.returncode

    wheel_prefix = f"{PROJECT_WHEEL_NAME}-{PROJECT_VERSION.replace('-', '_')}-"
    wheels = tuple(sorted(args.out.glob(f"{wheel_prefix}*.whl")))
    if len(wheels) != 1:
        print(
            f"expected exactly one wheel for version {PROJECT_VERSION}, found {len(wheels)}",
            file=sys.stderr,
        )
        return 2

    missing_payload = tuple(entry for entry in PAYLOAD if not (ROOT / entry).exists())
    if missing_payload:
        print(
            "release payload is incomplete: " + ", ".join(missing_payload),
            file=sys.stderr,
        )
        return 2
    shutil.copy2(wheels[0], staging / wheels[0].name)

    for entry in PAYLOAD:
        source = ROOT / entry
        target = staging / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    # Checksums let an operator verify the bundle before installing it.
    lines = [
        f"{sha256_file(path)}  {path.relative_to(staging).as_posix()}"
        for path in sorted(staging.rglob("*"))
        if path.is_file()
    ]
    (staging / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"bundle: {staging}")
    print(f"files:  {len(lines)}")
    print(
        "Reminder: COM planning attaches to an already-open supported AutoCAD; "
        "dotnet_bridge use additionally requires the exact target-specific C# bundle."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
