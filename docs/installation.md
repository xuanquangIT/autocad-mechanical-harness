# Installation and MCP setup

This guide installs AutoCAD Mechanical Harness `v0.2.2` as an engineering preview. The
offline MCP server is safe to try first. Live AutoCAD writes remain human-approved and
require a target-specific bridge; the unsigned bridge artifact is not a production release.

## 1. Install the Python MCP server

Requirements: Git, Python 3.12 or 3.13, and
[`uv`](https://docs.astral.sh/uv/). Clone the exact release tag:

```powershell
git clone https://github.com/xuanquangIT/autocad-mechanical-harness.git
Set-Location autocad-mechanical-harness
git checkout v0.2.2
uv sync --frozen --all-extras
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
  -PackageVersion 0.2.2.0 `
  -ProductCode 82658044-EBC3-4ECD-928C-A5B96770FC96 `
  -DevelopmentUnsigned
```

The output path starts with `DEVELOPMENT-UNSIGNED-`. It is suitable only for controlled
engineering acceptance. It does not satisfy AutoCAD production signing or `SECURELOAD`.
Never weaken `SECURELOAD` or edit `TRUSTEDPATHS` automatically.

Validate the artifact without installing it:

```powershell
.\Install-BridgeBundle.ps1 `
  -Action Validate `
  -BundlePath .\CadBridge.Plugin\bin\BridgePackages\DEVELOPMENT-UNSIGNED-R26-0-net10-0-windows-api-26-0-0-v0-2-2-0\AutoCADHarness.bundle `
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
4. Build/obtain the exact AutoCAD-series bundle and run installer `Validate`.
5. Remove duplicate legacy harness bundles using their owned receipts; do not delete unrelated
   Autodesk plug-ins.
6. Run receipt-bound `Install -Upgrade`, restart AutoCAD, and run the doctor.
7. Verify status and a read-only document inspection before enabling Engineer Desktop writes.
8. Recompile and reapprove old plans when the schema or plan hash changes; old approval tokens
   are intentionally invalid across such upgrades.

## 7. ChatGPT web

ChatGPT web cannot execute a local STDIO server directly. An eligible managed workspace can use
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
with an administrator-created tunnel, least-privilege API key, and developer-mode controls. The
tunnel transports MCP; it does not replace bridge signing, live-session proof, Engineer Desktop
approval, or customer engineering review.

See [operations](operations.md), [security](security.md), and the
[v0.2.2 release notes](releases/v0.2.2.md) for verification and remaining limits.
