# Installation and MCP setup

This guide installs AutoCAD Mechanical Harness `v0.3.0` as an engineering preview. The
offline MCP server is safe to try first. Live AutoCAD writes remain human-approved and
require a target-specific bridge; the unsigned bridge artifact is not a production release.

## 1. Install the Python MCP server

Requirements: Git, Python 3.12 or 3.13, and
[`uv`](https://docs.astral.sh/uv/). Clone the exact release tag:

```powershell
git clone https://github.com/xuanquangIT/autocad-mechanical-harness.git
Set-Location autocad-mechanical-harness
git checkout v0.3.0
uv sync --frozen --all-extras
uv run cad-harness --config config/base.yaml migrate
uv run cad-harness status
uv run cad-harness demo
```

The committed default uses `fake`; it cannot modify AutoCAD. Keep machine configuration,
drawings, databases, approval material, and secrets outside Git.

## 2. Register the local STDIO MCP server

Use absolute paths. A read-only/offline registration is:

```json
{
  "mcpServers": {
    "autocad-mechanical-harness": {
      "command": "C:\\Users\\you\\.local\\bin\\uv.exe",
      "args": [
        "--directory",
        "D:\\Workspace\\autocad-mechanical-harness",
        "run",
        "cad-harness-mcp"
      ],
      "env": {
        "CAD_HARNESS_CONFIG": "D:\\Workspace\\autocad-mechanical-harness\\config\\codex-local.yaml"
      }
    }
  }
}
```

Restart the MCP client, then verify `cad_status` reports `adapter_type: fake`. This proves
tool discovery only; it is not a live-CAD result.

For an engineer who already has AutoCAD open, register the same command with
`config/live-com-planning.yaml` instead. This profile attaches through COM without launching
AutoCAD and exposes planning only: inspect, internal job creation, compile, preview,
validation, and semantic diff. It does **not** expose commit, rollback, or export.

For Codex CLI/Desktop:

```powershell
$repo = (Resolve-Path .).Path
$uv = (Get-Command uv -ErrorAction Stop).Source
$config = Join-Path $repo 'config\live-com-planning.yaml'
Copy-Item (Join-Path $repo 'data\live-com\harness.db') `
  (Join-Path $repo 'data\live-com\harness.db.pre-v0.3.0.bak') `
  -ErrorAction SilentlyContinue
uv --directory $repo run cad-harness --config $config migrate
codex mcp remove autocad-mechanical-harness 2>$null
codex mcp add autocad-mechanical-harness `
  --env "CAD_HARNESS_CONFIG=$config" `
  -- $uv --directory $repo run cad-harness-mcp
```

The client starts the STDIO process when it opens the MCP connection and stops it when the
connection closes. There is no separate Windows service. AutoCAD itself remains open. MCP
clients are not required to disconnect after an idle period, so this is connection-scoped
shutdown rather than a guaranteed inactivity timer.

After restarting the client, call `cad_status` and `cad_document_inspect`. Require
`adapter_type: com`, the expected AutoCAD PID/version/document, `version_supported: true`,
and an unchanged revision after inspection. A natural-language request with complete values,
such as "draw R20 mm at [0,0] on layer 0", is represented as `reference_circle`; the client
should create a job and call `cad_change_prepare` without asking for those values again.
If the engineer omits the unit, the client may reuse the inspected drawing unit only when it
exactly matches the selected profile. A unitless, unknown, or mismatched drawing requires one
focused unit/standards correction before commit; silently treating inch geometry as millimetres
is forbidden.

The planning registration deliberately carries no write secret. When the returned validation
allows commit, copy its `job_id` into the human-only Engineer Desktop and grant write authority
only to that process:

```powershell
$jobId = '<job_id returned by cad_change_prepare>'
$env:CAD_HARNESS_APPROVAL_SECRET = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
)
$env:CAD_HARNESS_LIVE_WRITE_VERIFIED = '1'
try {
  uv --directory $repo run cad-harness-desktop $jobId --config $config
}
finally {
  Remove-Item Env:CAD_HARNESS_LIVE_WRITE_VERIFIED -ErrorAction SilentlyContinue
  Remove-Item Env:CAD_HARNESS_APPROVAL_SECRET -ErrorAction SilentlyContinue
}
```

For COM, AutoCAD PID, active document, revision and supported version are verified from the
live adapter. The only setup prompt left is the company-standards confirmation; the exact
preview/revision approval remains in the Desktop UI. Never put either environment variable in
the persistent MCP registration. The random secret above is suitable for a controlled test
session; production workstations still require organisation-managed secret provisioning.

Codex CLI/Desktop can use the same command and environment. Check the effective registration
without touching AutoCAD:

```powershell
uv run python scripts/check_codex_mcp_installation.py
```

The doctor intentionally fails if a live registration is inconsistent, development bundles
are duplicated, or bridge versions drift. A normal read-only registration must not contain
approval secrets, static manual confirmations, or reusable write authority.

## 3. Build the AutoCAD 2027/R26 bridge

AutoCAD 2027 uses .NET 10 and AutoCAD managed API `26.0.0`. Install the .NET 10 SDK, then run:

```powershell
Set-Location dotnet\AutoCADBridge
.\Package-BridgeBundle.ps1 `
  -TargetFramework net10.0-windows `
  -AutoCADManagedApiVersion 26.0.0 `
  -AutoCADSeries R26.0 `
  -PackageVersion 0.3.0.0 `
  -ProductCode 15AD106E-4705-4CB7-9538-1621587CF860 `
  -DevelopmentUnsigned
```

The output path starts with `DEVELOPMENT-UNSIGNED-`. It is suitable only for controlled
engineering acceptance. It does not satisfy AutoCAD production signing or `SECURELOAD`.
Never weaken `SECURELOAD` or edit `TRUSTEDPATHS` automatically.

Validate the artifact without installing it:

```powershell
.\Install-BridgeBundle.ps1 `
  -Action Validate `
  -BundlePath .\CadBridge.Plugin\bin\BridgePackages\DEVELOPMENT-UNSIGNED-R26-0-net10-0-windows-api-26-0-0-v0-3-0-0\AutoCADHarness.bundle `
  -ExpectedAutoCADSeries R26.0 `
  -DevelopmentUnsigned `
  -InstallRoot D:\cad-harness-development-install
```

For an organisation-signed build, omit `-DevelopmentUnsigned` and supply the packager's
reviewed support-email, signing-certificate thumbprint, and approved HTTPS timestamp inputs.
The installer also requires its embedded approved signer policy; it fails closed until release
engineering provisions that policy.

## 4. Install, upgrade, or uninstall a signed bridge

First close **every AutoCAD process**. Back up the drawing and `data/harness.db`. Never install
over a running session or the only copy of a production drawing.

```powershell
# Validate a signed artifact first.
.\Install-BridgeBundle.ps1 -Action Validate `
  -BundlePath C:\release\AutoCADHarness.bundle `
  -ExpectedAutoCADSeries R26.0

# Fresh signed install to the per-user Autodesk ApplicationPlugins root.
.\Install-BridgeBundle.ps1 -Action Install `
  -BundlePath C:\release\AutoCADHarness.bundle `
  -ExpectedAutoCADSeries R26.0

# Owned upgrade; the receipt and signer continuity are checked.
.\Install-BridgeBundle.ps1 -Action Install -Upgrade `
  -BundlePath C:\release\AutoCADHarness.bundle `
  -ExpectedAutoCADSeries R26.0

# Receipt-bound uninstall. BundlePath is intentionally not accepted here.
.\Install-BridgeBundle.ps1 -Action Uninstall `
  -ExpectedAutoCADSeries R26.0
```

Development-unsigned installation is allowed only with `-DevelopmentUnsigned` and an explicit
non-default custom `-InstallRoot`. That custom root is a test boundary, not proof that AutoCAD
will discover or trust the plug-in. Do not manually copy DLLs into ApplicationPlugins.

## 5. Connect to an already-open AutoCAD drawing

After installing one matching bridge, open a disposable copy of the DWG in AutoCAD and make it
active. Use `config/live-r26-acceptance.yaml`, which sets `dotnet_bridge`, never launches AutoCAD,
and requires approval:

```json
{
  "mcpServers": {
    "autocad-mechanical-harness": {
      "command": "C:\\Users\\you\\.local\\bin\\uv.exe",
      "args": ["--directory", "D:\\Workspace\\autocad-mechanical-harness", "run", "cad-harness-mcp"],
      "env": {
        "CAD_HARNESS_CONFIG": "D:\\Workspace\\autocad-mechanical-harness\\config\\live-r26-acceptance.yaml"
      }
    }
  }
}
```

Restart the MCP client. Call `cad_status`, then `cad_document_inspect`. Require all of the
following before trusting the connection:

- `adapter_type` is `dotnet_bridge`, never `fake`;
- AutoCAD version/series is the expected R26 target;
- document id and revision match the active disposable DWG;
- the Named Pipe server PID belongs to that AutoCAD process;
- read-only inspection leaves the document revision unchanged.

Do not place `CAD_HARNESS_APPROVAL_SECRET`, an approval token, or
`CAD_HARNESS_LIVE_SESSION_PROOF` in persistent MCP configuration. Engineer Desktop obtains the
manual setup confirmations, issues a short-lived process/document/revision-bound session proof,
and performs the separate plan-hash approval. MCP confirmation alone never authorizes a write.

## 6. Upgrade checklist

1. Stop MCP clients and Engineer Desktop; close AutoCAD.
2. Back up `harness.db`, local configuration, and required checkpoint/audit data.
3. Check out the new tag and run `uv sync --frozen --all-extras`.
4. Run `uv run cad-harness --config <exact-config.yaml> migrate`. The command adopts an
   unversioned v0.2.2 database only when its complete schema matches the trusted legacy layout;
   unknown or partially modified databases fail without being stamped.
5. Build/obtain the exact AutoCAD-series bundle and run installer `Validate`.
6. Remove duplicate legacy harness bundles using their owned receipts; do not delete unrelated
   Autodesk plug-ins.
7. Run receipt-bound `Install -Upgrade`, restart AutoCAD, and run the doctor.
8. Verify status and a read-only document inspection before enabling Engineer Desktop writes.
9. Recompile and reapprove old plans when the schema or plan hash changes; old approval tokens
   are intentionally invalid across such upgrades.

## 7. Roll back an unsuccessful upgrade

1. Stop every MCP client and Engineer Desktop process, then close AutoCAD.
2. Preserve the failed migrated database and installer output for diagnosis; do not open it
   with an older harness version.
3. Restore the exact pre-upgrade database backup and matching local configuration to their
   original paths.
4. Check out the previous verified release tag and run its locked `uv sync --frozen` command.
5. For a signed bridge, use the current receipt-bound installer to uninstall the failed bundle,
   then validate and reinstall the previous organisation-signed bundle. For a development
   custom root, remove only the exact receipt-owned test bundle and reinstall the previous
   validated artifact; never delete unrelated Autodesk plug-ins.
6. Start AutoCAD, run `cad-harness status`, and perform one read-only document inspection.
   Keep writes disabled until adapter type, version, document identity, units, profile and
   revision all match the restored release evidence.

## 8. ChatGPT web

ChatGPT web cannot execute a local STDIO server directly. An eligible managed workspace can use
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
with an administrator-created tunnel, least-privilege API key, and developer-mode controls. The
tunnel transports MCP; it does not replace bridge signing, live-session proof, Engineer Desktop
approval, or customer engineering review.

See [operations](operations.md), [security](security.md), and the
[v0.3.0 release notes](releases/v0.3.0.md) for verification and remaining limits.
