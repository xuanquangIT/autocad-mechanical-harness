# Installer

The production bridge installer is implemented in
`dotnet/AutoCADBridge/Install-BridgeBundle.ps1`; the matching deterministic packager is
`dotnet/AutoCADBridge/Package-BridgeBundle.ps1`. This directory remains the home for a future
whole-product Python/runtime installer, not an indication that bridge installation is absent.

## Implemented bridge installer

- Validates the exact R25/R26 bundle manifest, module identity and exhaustive checksum set.
- Requires Authenticode signatures, timestamp evidence and signer continuity for release installs.
- Allows development-unsigned bundles only in an explicit non-default custom root.
- Uses a same-root authenticated transaction journal, exclusive install lock and crash recovery.
- Publishes an exact ownership receipt and uses it for fail-closed upgrade/uninstall.
- Rejects traversal, reparse/network roots, duplicate files, version drift and a running AutoCAD
  process for normal deployment.

It does not manufacture organisation signing evidence or approve a customer profile. Those remain
release and engineering gates.

## Whole-product installer still pending

- Python runtime and the wheel
- MCP server launcher plus example client configuration
- `config/base.yaml` template; leaves an existing `config/local.yaml` untouched
- SQLite migration on install (`alembic upgrade head`), with a backup first
- Demo company profile only; real profiles are installed separately by the customer
- Generates a per-workstation `CAD_HARNESS_APPROVAL_SECRET`
- Creates the allowlisted export, preview and checkpoint directories
- Signed checksums plus an uninstall and rollback guide

## External production evidence still required

- Code signing for the plug-in and the installer itself
- Approved timestamp service and signer-rotation policy
- Clean-workstation signed install, upgrade, rollback and uninstall evidence
- Python runtime/wheel plus MCP-client registration under the same owned receipt

## Rules

- Never overwrite an existing config or company profile.
- Back up `harness.db` before migrating; the audit chain is only trustworthy if history
  survives upgrades.
- Do not ship a single DLL for every AutoCAD version when the runtime differs.
- The installer must not enable a `company_approved` profile on the customer's behalf.
  That is an engineering sign-off, not an install step.
