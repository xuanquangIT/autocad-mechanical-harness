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
| 2025 | 25.0 | 8.0 | 0.1.0 | provisional; live acceptance pending |
| 2026 through Update 1.1 | 25.1 | 8.0 | separate verified build required | provisional; live acceptance pending |
| 2026 Update 1.2+ | 25.1 | 10.0 | separate verified build required | not packaged by the current net8 bundle |

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
uv run cad-harness raster-accept --help   # human acceptance for calibrated image candidates
uv run cad-harness-mcp                    # MCP server on stdio
```

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

Engineer Desktop collects the first five confirmations interactively before constructing
the live adapter. A non-interactive MCP host must receive equivalent explicit startup
evidence through `CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS`, containing the exact first five
step ids in the order above, comma-separated. Missing, reordered, unknown, or extra ids
fail before COM/Named Pipe construction. This evidence never confirms step 6; commit still
requires the human approval UI, bound token, plan hash, and revision.

For an integration test, begin with the disposable DWG closed everywhere except the one
declared AutoCAD session, no active command, the expected revision recorded, and a backup
copy outside the test directory. After the test, roll back through the recorded checkpoint
or close without saving, verify the original hash/revision, remove only test-created preview
and checkpoint files, and keep the audit/job records as evidence. Never run the suite against
the sole copy of a production drawing.
