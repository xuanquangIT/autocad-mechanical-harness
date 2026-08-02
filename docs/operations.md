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

## Adapter selection

| Adapter | Use for | Can commit |
|---|---|---|
| `fake` | tests, CI, demos | to memory only |
| `dxf_preview` | preview development, no CAD installed | no |
| `com` | MVP pilot on a real drawing | yes, without transaction guarantees |
| `dotnet_bridge` | production (Phase 5) | not implemented |

Before switching to `com`: AutoCAD running, the target drawing open, same Windows user
session, and a scratch drawing rather than a live one for the first run.

## Daily commands

```powershell
uv run cad-harness status                 # adapter, profile, capabilities
uv run cad-harness features               # what the catalog supports
uv run cad-harness demo                   # reference case, preview only
uv run cad-harness demo --commit          # full path including approval
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

- Previews: short TTL (`storage.preview_retention_days`, default 14).
- Checkpoints: allowlisted directories only. They are full drawing copies, so treat them
  as customer IP and apply quota and encryption policy.
- Audit: longer retention, append-only, never edited in place.
