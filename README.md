# AutoCAD Mechanical Harness

Deterministic 2D mechanical drawing automation for AutoCAD, driven through the Model
Context Protocol (MCP).

An AI client describes what to draw in engineering terms. This harness compiles that
description into a deterministic operation plan, previews it outside the live drawing,
validates it against measurable rules, and only writes to the DWG after an engineer
approves that exact plan.

**The LLM is not the geometry kernel.** It produces a `DrawingSpec`; Python computes
every coordinate.

- Architecture (Vietnamese): [`docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.vn.md`](docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.vn.md)
- Architecture (English): [`docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md`](docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md)

## Status

Active production-roadmap implementation. Offline gates pass and the R26 bridge has passed
disposable-drawing read, atomic commit, metadata readback and session undo acceptance;
a signed release bundle, company drawings and pilot evidence remain open.

| Area | State |
|---|---|
| Contracts, domain models, job state machine | Implemented |
| Geometry kernel (primitives, tolerance, patterns) | Implemented |
| Feature catalog | 10 deterministic mechanical features implemented |
| Drawing comprehension | DXF/bridge read, seven-type recognition, takeoff, audit, remediation, 12 measurements and calibrated raster tracing |
| Validation engine + drawing auditor | Implemented |
| DXF/SVG preview, semantic diff | Implemented |
| Fake adapter | Implemented (atomic, revision-tracked) |
| COM adapter | Summary/selection reader passed isolated PID-fenced R26 non-mutation acceptance; detailed geometry remains bridge/DXF-only |
| C# bridge | R26 plugin/IPC/bounded inspection/atomic executor and durable commit replay implemented; live scratch commit and session-bound undo rollback passed; durable DWG checkpoint replacement remains unavailable |
| MCP server | 22 typed, permission-guarded tools wired to production services |
| Engineer desktop | PySide6 review/approval/commit and separately approved rollback surface with stale-scope polling and memory-only tokens |
| Pilot metrics | Run-scoped baseline/effort/operation evidence, finite failure classification and acceptance report |
| SQLite persistence | Runtime job, audit, lease, takeoff, drawing-audit and pilot-metrics stores |

## Quick start

Requires Python 3.12–3.13 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync                      # install locked dependencies
uv run pytest                # run the suite (no AutoCAD needed)
uv run ruff check .          # lint
uv run cad-harness status    # inspect configuration and adapter
```

The default adapter is `fake`, so a fresh checkout cannot touch a real drawing.

### Run the MCP server

```powershell
uv run cad-harness-mcp
```

Register it with an MCP client (paths must be absolute):

```json
{
  "mcpServers": {
    "cad-harness": {
      "command": "uv",
      "args": ["--directory", "D:\\Workspace\\autocad-mechanical-harness", "run", "cad-harness-mcp"],
      "env": { "CAD_HARNESS_ADAPTER": "fake" }
    }
  }
}
```

For a local Codex/ChatGPT desktop smoke run, point `CAD_HARNESS_CONFIG` at
`config/codex-local.yaml`. That profile explicitly grants the identity-less STDIO client
planning/preview permissions while pinning the adapter to `fake`; it cannot touch AutoCAD.

Switch `CAD_HARNESS_ADAPTER` to `com` only on a Windows machine with AutoCAD open on
the target drawing, and install the COM extra: `uv sync --extra com`.

## Tool surface

Twenty-two high-level tools. No `draw_line`, no `trim`, no `offset` — primitive tools
would let the model assemble geometry itself, which is exactly what this design avoids.

| Tool | Side effect | Permission set |
|---|---|---|
| `cad_status` | none | read-only |
| `cad_document_inspect` | none | read-only |
| `cad_selection_inspect` | none | read-only |
| `cad_feature_catalog_search` | none | read-only |
| `cad_drawing_read` | audit record | read-only |
| `cad_feature_recognize` | none | read-only |
| `cad_takeoff` | persisted report + audit | read-only |
| `cad_audit` | persisted evidence + audit | read-only |
| `cad_measure` | none | read-only |
| `cad_image_inspect` | local review overlay | read-only |
| `cad_image_trace` | local calibrated review overlay | read-only |
| `cad_image_draft` | returns a sealed draft spec; no job/DWG write | read-only |
| `cad_validate` | validation record | read-only |
| `cad_diff_get` | none | read-only |
| `cad_job_create` | internal DB | approval-required client |
| `cad_spec_submit` | internal DB | approval-required client |
| `cad_change_submit` | internal DB | approval-required client |
| `cad_preview` | temporary files | approval-required client |
| `cad_takeoff_export` | allowlisted file | approval-required client |
| `cad_export` | allowlisted file | approval-required client |
| `cad_commit` | modifies DWG | approval token + client permission |
| `cad_rollback` | modifies DWG | separate Engineer Desktop `rb1` token bound to exact checkpoint/current revision + client permission |

## Layering

```
apps/  (MCP, CLI, desktop)
  -> application/  (orchestration, state machine, gates)
    -> domain/  (models, ports, errors)
      -> geometry/ + validation/  (pure, deterministic)

adapters/ implement domain ports.
```

The domain never imports MCP, COM, AutoCAD, SQLAlchemy or UI code. `win32com` is
confined to `adapters/autocad_com.py`, enforced by a Ruff banned-api rule.

## Non-negotiables

These are enforced in code, not just documented:

1. No silent defaults. A default carries value, source, version and impact, or it is
   asked of the engineer.
2. Preview never modifies the active drawing.
3. Commit requires an approval bound to one exact `plan_hash` and revision.
4. A stale revision rejects the commit.
5. The same idempotency key never creates duplicate entities.
6. Committed entities are read back and re-measured; a mismatch fails the commit.

## Calibrated image-to-drawing

`cad_image_inspect` and `cad_image_trace` accept bounded base64 for a local PNG, JPEG
or TIFF. A trace is only a review report: it never infers dimensions, tolerance,
material, layer or design intent. Calibrated millimetre geometry requires two distinct
pixel points and their real distance.

An engineer reviews the opaque local SVG overlay and issues a short-lived acceptance
outside MCP:

```powershell
uv run cad-harness raster-accept .\trace-report.json `
  --candidate raster-candidate-... `
  --accepted-by engineer_17 `
  --layer TRACE_REVIEWED `
  --confirm-reviewed-overlay
```

Pass that acceptance to `cad_image_draft`. The returned `DrawingSpec` still must go
through `cad_spec_submit`, preview, validation, Engineer Desktop approval, commit and
post-commit readback. Image-derived geometry is never treated as production evidence by
itself.

## Before a pilot

Three things must be supplied by the organisation, not guessed:

1. The AutoCAD version actually in use.
2. The controlled DWT/DWS set, layers, dimstyles, title block and plot profiles.
3. The standard and tolerance profile (ISO, ASME, JIS or internal).

Until then the harness runs on `demo-profile`, which must never be labelled
"company approved".

## Layout

```
apps/                MCP server, CLI, engineer desktop
config/              base.yaml plus local overrides
contracts/           generated JSON Schemas
docs/                architecture, ADRs
dotnet/              C# AutoCAD bridge (Phase 5)
scripts/             schema generation, golden test runner, packaging
src/cad_harness/     the library
tests/               unit, property, contract, integration, golden, fault injection
```

## License

Proprietary. Internal use only.
