# Operations

Running the harness on a developer machine and on a pilot workstation.

## Install

```powershell
uv sync                       # core
uv sync --extra com           # add pywin32 for the AutoCAD adapter (Windows)
uv sync --all-extras          # add the PySide6 desktop dependency too
```

Copy `.env.example` to `.env` and set `CAD_HARNESS_APPROVAL_SECRET` to a value unique to
the workstation. Without it, approval signing fails closed.

## Configuration

`config/base.yaml` is the committed baseline. Machine-specific overrides go in
`config/local.yaml` (gitignored) or environment variables, which take precedence.

The settings that matter most:

| Setting | Why it matters |
|---|---|
| `adapter.type` | `fake` cannot touch a drawing. Change to `com` deliberately. |
| `adapter.launch_autocad_if_missing` | Keep `false`. A background server should not start AutoCAD. |
| `standards.company_profile` | `demo-profile` is not company approved and must not be presented as such. |
| `security.export_path_allowlist` | Every export target is resolved against this. |
| `observability.log_prompts` | Keep `false`. Prompts can contain customer data. |
| `raster.*` | Local image byte/pixel limits, confidence threshold and 15-minute maximum acceptance TTL. |

## Adapter selection

| Adapter | Use for | Can commit |
|---|---|---|
| `fake` | tests, CI, demos | to memory only |
| `dxf_preview` | preview development, no CAD installed | no |
| `com` | MVP pilot on a real drawing | yes, without transaction guarantees |
| `dotnet_bridge` | transactional pilot and approved deployment path | yes; signed release and broader version acceptance remain open |

Before switching to `com`: AutoCAD running, the target drawing open, same Windows user
session, and a scratch drawing rather than a live one for the first run.

### Published compatibility targets

`config/compatibility.yaml` is the executable writer allowlist. These targets follow
Autodesk's [Managed .NET compatibility table](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-Customization/files/GUID-A6C680F2-DE2E-418A-A182-E4884073338A.htm):

| AutoCAD | COM release prefix | .NET runtime | Bridge bundle | Verification |
|---|---:|---:|---:|---|
| 2024 | 24.3 | 4.8 | separate bundle required | provisional; writer disabled |
| 2025 | 25.0 | 8.0 | 0.2.0 | provisional; live acceptance pending |
| 2026 through Update 1.1 | 25.1 | 8.0 | separate verified build required | provisional; live acceptance pending |
| 2026 Update 1.2+ | 25.1 | 10.0 | separate verified build required | not packaged by the current net8 bundle |
| 2027 | 26.0 | 10.0 | 0.2.0 engineering-preview bundle | live development acceptance passed; signed release still required |

An unlisted or unparseable version is visible in `cad_status` with
`version_supported=false` and is denied at the writer boundary. “Provisional” means the
runtime/API pairing is a declared build target, not evidence that this harness passed a
real-AutoCAD test on that release.

## Daily commands

```powershell
uv run cad-harness status                 # adapter, profile, capabilities
uv run cad-harness features               # what the catalog supports
uv run cad-harness demo                   # reference case, preview only
uv run cad-harness demo --commit          # fake adapter only; never self-approves live CAD
uv run cad-harness raster-review report.json  # resolve the exact local overlay and SHA-256
uv run cad-harness raster-accept --help   # human acceptance for calibrated image candidates
uv run cad-harness-mcp                    # MCP server on stdio
uv run python scripts/check_codex_mcp_installation.py  # redacted local registration/bundle doctor
```

Raster acceptance is intentionally two-step. First run `raster-review`, open the printed local SVG,
and compare its candidate IDs to the source image. Then pass the printed digest back with
`--reviewed-overlay-sha256 sha256:...` together with `--confirm-reviewed-overlay`. A stale or replaced
overlay is rejected; neither review nor acceptance is exposed as an MCP tool.

## Codex and ChatGPT connection checks

Codex desktop/CLI can register the local STDIO command directly. Before relying on that registration,
run `check_codex_mcp_installation.py`; it fails closed on a fake/wrong config, missing secret injection,
persisted write authority, duplicate or unsigned development bundles, reparse points, and
workspace/global version drift. Marker absence is deliberately reported as
`BUNDLE_AUTHENTICODE_UNVERIFIED`, not as proof of signing; the hardened installer remains the authority
for certificate, publisher and timestamp verification. The doctor's JSON contains counts and versions
only, never paths or secrets.

ChatGPT web cannot connect directly to a local MCP process. For private developer testing, use
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels):

1. Obtain a `tunnel_id`, Tunnels Read + Use permission, a runtime API key, and ChatGPT developer-mode
   access from the appropriate Platform/workspace administrators.
2. Download `tunnel-client` from the current link in Platform tunnel settings; do not vendor a stale
   binary or place the runtime API key in this repository.
3. Initialize a named STDIO profile whose command is the same absolute `uv --directory ... run
   cad-harness-mcp` registration validated by the local doctor.
4. Run `tunnel-client doctor --profile <profile> --explain`, then keep `tunnel-client run --profile
   <profile>` healthy while scanning tools in the ChatGPT developer-mode app.
5. Keep write tools disabled until the signed single-bundle, engineer corpus, pilot and clean-workstation
   gates pass. ChatGPT confirmation does not replace the harness's plan/revision-bound Engineer Desktop
   approval.

The tunnel is outbound-only transport; it does not turn development evidence into production approval.
Full write-capable MCP apps currently require an eligible Business or Enterprise/Edu workspace and its
admin controls. See the official
[developer-mode requirements](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta).

## Database

```powershell
$env:CAD_HARNESS_SQLITE_PATH = "./data/harness.db"
uv run alembic upgrade head
uv run alembic current
```

Back up `harness.db` before every migration on a machine with real job history. The audit
chain is only trustworthy if history survives upgrades. `cad-harness migrate` uses
`create_all` and is for development only.

## What to watch

From section 21.2 of the architecture document, the numbers that actually indicate
trouble:

- **Post-commit mismatch rate** — should be zero. Anything above it means the adapter and
  the plan disagree, which is the most serious failure this system can have.
- **Stale revision rejections** — expected to be non-zero. It means concurrency detection
  is working, not that something is broken.
- **Missing-input rate per feature** — a spike points at a feature whose required
  parameters are unclear to AI clients.
- **Validation failure rate per rule** — a rule that always fires is either miscalibrated
  or documenting a real standards gap.
- **Preview-to-commit conversion** — low conversion means engineers reject what the model
  proposes.
- **COM busy/error rate** — the signal that it is time for the C# bridge.

Logs are structured JSON on stderr. On STDIO transport, stdout carries the MCP protocol
and nothing else.

## Failure playbooks

**`AUTOCAD_BUSY`** — a command is active in AutoCAD. Retry is safe *before* a write
begins; the adapter only retries in that window.

**`STALE_DOCUMENT_REVISION`** — the drawing changed after approval. Re-inspect, preview,
validate and approve again. Never override.

**`POST_COMMIT_VALIDATION_FAILED`** — geometry was written that does not match the
approved plan. The error carries the `checkpoint_id`. Roll back, then investigate the
adapter mapping before retrying.

**`UNKNOWN_COMMIT_STATE`** — the outcome could not be determined. Do not retry. Reconcile
from the job record, the entity mapping and the current document revision, then decide.

**`IDEMPOTENCY_KEY_REUSED`** — the client sent the same key with different content.
Generate a fresh key; do not strip the key to work around it.

## Retention

- Previews: short TTL (`storage.preview_retention_days`, default 14) and a deterministic
  oldest-first quota (`storage.preview_max_total_bytes`, default 1 GiB).
- Checkpoints: allowlisted directories only, default 30-day TTL and 10 GiB quota through
  `storage.checkpoint_retention_days` / `storage.checkpoint_max_total_bytes`. They are full
  drawing copies, so treat them as customer IP and encrypt the storage volume.
- Audit: longer retention, append-only, never edited in place.

## Required manual gates for real AutoCAD

The live workflow must display each instruction and wait for confirmation of that exact
step before running its next action. The ordered steps are:

1. Open AutoCAD with a disposable copy of the target DWG and verify it is active.
2. Load the controlled company DWT and DWS files.
3. Install the signed C# Bridge `.bundle` matching the target release.
4. Grant the current Windows user access to the per-user Named Pipe ACL.
5. Confirm the detected AutoCAD version is accepted by `config/compatibility.yaml`.
6. Review the exact preview, findings, plan hash and revision in Engineer Desktop, then
   approve commit.

Engineer Desktop collects the first five confirmations interactively. For a write-capable
launch it then inspects the actual adapter and issues an `lsp1` live-session proof, valid for
at most 15 minutes and bound to the exact adapter type, AutoCAD PID, document id and revision.
The proof may be passed to one non-interactive child through
`CAD_HARNESS_LIVE_SESSION_PROOF`; it must never be stored in Codex configuration, a database,
logs, evidence, or source control. `CAD_HARNESS_LIVE_WRITE_VERIFIED=1` only requests write
mode and grants no authority by itself. A normal registered MCP process remains read-only and
does not require either the signing secret or setup proof. The old
`CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS` string is unbound, is not trusted by server startup,
and is rejected by the installation doctor. None of these setup steps confirms step 6;
every commit still requires the human approval UI, bound token, plan hash, and revision.

For an integration test, begin with the disposable DWG closed everywhere except the one
declared AutoCAD session, no active command, the expected revision recorded, and a backup
copy outside the test directory. After the test, roll back through the recorded checkpoint
or close without saving, verify the original hash/revision, remove only test-created preview
and checkpoint files, and keep the audit/job records as evidence. Never run the suite against
the sole copy of a production drawing.
