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

Scaffold. Phase 1 of the roadmap in section 27 of the architecture document.

| Area | State |
|---|---|
| Contracts, domain models, job state machine | Implemented |
| Geometry kernel (primitives, tolerance, patterns) | Implemented |
| Feature catalog | `rectangular_plate`, `rectangular_hole_pattern`, `bolt_circle_pattern` |
| Feature catalog | `flange`, `slot`, `l_bracket` declared, not implemented |
| Validation engine + 11 rules | Implemented |
| DXF/SVG preview, semantic diff | Implemented |
| Fake adapter | Implemented (atomic, revision-tracked) |
| COM adapter | Skeleton: inspect, commit for outlines and holes, export |
| C# bridge | Contract only (Phase 5) |
| MCP server | 13 tools wired to the application facade |
| SQLite persistence | Tables defined; in-memory store used at runtime |

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

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

Switch `CAD_HARNESS_ADAPTER` to `com` only on a Windows machine with AutoCAD open on
the target drawing, and install the COM extra: `uv sync --extra com`.

## Tool surface

Thirteen high-level tools. No `draw_line`, no `trim`, no `offset` — primitive tools
would let the model assemble geometry itself, which is exactly what this design avoids.

| Tool | Side effect | Approval |
|---|---|---|
| `cad_status` | none | no |
| `cad_document_inspect` | none | no |
| `cad_selection_inspect` | none | no |
| `cad_feature_catalog_search` | none | no |
| `cad_job_create` | internal DB | no |
| `cad_spec_submit` | internal DB | no |
| `cad_change_submit` | internal DB | no |
| `cad_preview` | temp files | no |
| `cad_validate` | none | no |
| `cad_diff_get` | none | no |
| `cad_commit` | modifies DWG | **required** |
| `cad_rollback` | destructive | **required** |
| `cad_export` | writes files | per policy |

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
