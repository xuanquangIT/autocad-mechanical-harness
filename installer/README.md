# Installer

Empty until Phase 4. `scripts/package_release.py` produces the MVP bundle with
checksums; this directory is for the installer that consumes it.

## MVP installer (Phase 4)

- Python runtime and the wheel
- MCP server launcher plus example client configuration
- `config/base.yaml` template; leaves an existing `config/local.yaml` untouched
- SQLite migration on install (`alembic upgrade head`), with a backup first
- Demo company profile only; real profiles are installed separately by the customer
- Generates a per-workstation `CAD_HARNESS_APPROVAL_SECRET`
- Creates the allowlisted export, preview and checkpoint directories
- Signed checksums plus an uninstall and rollback guide

## Production additions (Phase 5)

- C# plug-in `.bundle` with `PackageContents.xml` for each target AutoCAD version
- Code signing for the plug-in and the installer itself
- Named-pipe ACL restricted to the permitted user or service account
- Version compatibility matrix, health check, crash-recovery policy

## Rules

- Never overwrite an existing config or company profile.
- Back up `harness.db` before migrating; the audit chain is only trustworthy if history
  survives upgrades.
- Do not ship a single DLL for every AutoCAD version when the runtime differs.
- The installer must not enable a `company_approved` profile on the customer's behalf.
  That is an engineering sign-off, not an install step.
