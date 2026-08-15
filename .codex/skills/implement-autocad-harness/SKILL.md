---
name: implement-autocad-harness
description: Implement, repair, review, and verify features in the autocad-mechanical-harness repository, including deterministic 2D mechanical feature compilation, drawing read/edit/audit/takeoff/measurement, raster-to-vector intake, MCP tools, AutoCAD COM or C# bridge integration, contracts, persistence, packaging, and production acceptance. Use whenever changing this repository's Python, C#, MCP, CAD adapters, schemas, tests, roadmap, or implementation documentation.
---

# Implement AutoCAD Harness

Deliver repository changes against the authoritative architecture and measurable evidence. Preserve the single safe write path and keep the geometry kernel deterministic.

## Start every task

1. Run `python .codex/skills/implement-autocad-harness/scripts/project_audit.py` from the repository root.
2. Read `.kiro/steering/project-conventions.md` and the relevant requirement, design, and task entries.
3. Read `references/project-map.md` to select the affected layers and authoritative files.
4. Inspect `git status --short` and preserve all pre-existing changes. Never reset or overwrite unrelated work.
5. State the contract, rule, determinism proof, side effects, approval/security impact, and dependency-boundary impact before editing.

If the change touches a public model, operation, tool, error code, schema, or persisted record, treat it as a contract change. Update versioning, generated schemas, compatibility tests, migrations, and ADRs as applicable.

## Classify the work

- For feature compilers, geometry, modifiers, annotations, or multi-view work, read `docs/implementation/feature-delivery-playbook.md` and `docs/feature-authoring.md`.
- For reading, recognition, takeoff, audit, remediation, or measurement, preserve `DrawingModel` as the only read/write meeting point. Read operations remain structurally read-only; remediation re-enters the existing write pipeline at `OperationPlan`.
- For image conversion, treat raster inference as untrusted intake. Preserve the source image hash, units/scale evidence, confidence, unresolved ambiguities, and traced primitives. Never commit inferred geometry directly; require deterministic normalization, preview, validation, and engineer approval.
- For MCP work, expose high-level engineering operations only. Return the common envelope and contain exceptions at the boundary. Do not add primitive drawing tools.
- For COM or bridge work, keep AutoCAD dependencies out of the domain. Require document revision checks, one writer lease, atomic transaction semantics, stable entity metadata, readback, and post-commit measurement.
- Never authorize live writes from persisted manual-confirmation strings. Require a short-lived `lsp1` proof bound to the exact adapter, AutoCAD PID, document id, and revision; keep ordinary MCP registration read-only.
- For packaging or installation, keep the default adapter non-writing. Require an explicit configured AutoCAD session before any real-DWG integration test.

## Implement in dependency order

1. Add or revise domain contracts and stable errors.
2. Implement pure geometry or comprehension logic.
3. Add application orchestration and policy gates.
4. Add infrastructure adapters and persistence.
5. Expose the capability through MCP/CLI/UI only after the lower layers are complete.
6. Generate schemas and migration artifacts.
7. Add tests from narrow unit cases through property, contract, golden, fault, integration, and performance coverage.
8. Update roadmap checkboxes only after every stated acceptance criterion has evidence.

Do not register a feature or advertise a tool before its compiler/service, validation, adapter capability, preview, and required tests are complete.

## Enforce non-negotiable invariants

- Compute all coordinates in pure geometry code; never let an LLM or adapter act as the geometry kernel.
- Never invent dimensions, units, datum, material, thickness, tolerance class, hole count, diameter, or PCD.
- Record every allowed default with value, source, version, and impact.
- Keep preview non-mutating.
- Bind approval to the exact job, plan hash, and expected revision; reject stale revisions unconditionally.
- Make commit idempotent and post-commit measurement independent of expected values.
- Keep customer drawings, prompts, paths, approval tokens, and geometry out of logs and external connections.
- Use tolerance predicates for geometry; never compare geometric floats with `==`.

## Verify proportionally

Read `references/delivery-gates.md` and run every applicable gate. At minimum run:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src apps
uv run pytest -q
uv run python scripts/generate_schemas.py --check
uv run python scripts/run_golden_tests.py
```

Run import-boundary, packaging, fault, slow, COM, bridge, and real-AutoCAD gates when those surfaces exist or change. A skipped AutoCAD test is not production evidence. Record the exact skip reason and keep the acceptance item open.

## Finish with evidence

Report:

- files and contracts changed;
- requirements/tasks satisfied;
- exact commands and pass/fail/skip counts;
- remaining limitations and manual gates;
- whether any real DWG was modified;
- whether AutoCAD, package installation, and MCP-client acceptance were actually exercised.

Do not claim production readiness, complex-design coverage, image-conversion accuracy, or AutoCAD compatibility from Python-only tests.
