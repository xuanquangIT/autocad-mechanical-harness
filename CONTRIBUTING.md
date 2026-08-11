# Contributing

Thank you for helping make AI-assisted CAD safer, more deterministic, and easier to audit.

AutoCAD Mechanical Harness accepts code, tests, documentation, engineering references, and
reproducible acceptance evidence. Small, focused pull requests are preferred.

## Before you start

1. Search existing issues and discussions.
2. Open an issue before a large feature, public contract change, new dependency, or
   architecture change.
3. Never upload customer drawings, company profiles, approval tokens, private paths, or
   proprietary standards.
4. Use synthetic or explicitly de-identified fixtures that you have permission to publish.

## Development setup

The offline core runs on Python 3.12 or 3.13. Live AutoCAD integrations require Windows and
an appropriately licensed AutoCAD installation.

```powershell
git clone https://github.com/xuanquangIT/autocad-mechanical-harness.git
cd autocad-mechanical-harness
uv sync --all-extras
uv run pytest -m "not integration and not com" -q
```

The default configuration uses the non-writing fake adapter.

## Project rules

- Geometry coordinates are computed only in `src/cad_harness/geometry` or deterministic
  feature compilers.
- Never guess engineering dimensions, units, datums, material, tolerances, or feature
  counts.
- Preview is non-mutating.
- Commit approval is bound to an exact plan hash and document revision.
- A stale revision must fail closed.
- Adapters do not contain engineering business rules.
- Public errors use stable codes and must not expose stack traces or private paths.
- Real AutoCAD tests use an explicitly approved disposable drawing.

Read the [architecture](docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md),
[project conventions](.kiro/steering/project-conventions.md), and relevant ADRs before
changing core behavior.

## Pull request workflow

1. Create a focused branch.
2. Add or update tests before changing a roadmap checkbox.
3. Update generated schemas and compatibility tests for public contract changes.
4. Document any new dependency and why it is required.
5. Run the applicable gates and include exact pass/fail/skip evidence in the pull request.
6. Explain whether any real DWG, AutoCAD session, package installation, or MCP client was
   actually exercised.

Minimum local gates:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src apps
uv run pytest -q
uv run python scripts/generate_schemas.py --check
uv run python scripts/run_golden_tests.py
```

The C# bridge gates run in GitHub Actions. Contributors with a .NET 8 SDK can also run:

```powershell
dotnet restore dotnet/AutoCADBridge/CadBridge.Tests/CadBridge.Tests.csproj `
  --configfile dotnet/AutoCADBridge/NuGet.Config
dotnet test dotnet/AutoCADBridge/CadBridge.Tests/CadBridge.Tests.csproj `
  -c Release --no-restore --nologo -warnaserror
```

## AI-assisted contributions

AI-assisted contributions are welcome. The contributor remains responsible for correctness,
licensing, security, test evidence, and every statement in the pull request. Do not send
customer drawings, secrets, or proprietary company standards to an external model or service.

## Licensing

By submitting a contribution, you agree that it may be distributed under the
[Apache License 2.0](LICENSE). Only contribute material that you have the right to license.

All contributors must follow the [Code of Conduct](CODE_OF_CONDUCT.md).
