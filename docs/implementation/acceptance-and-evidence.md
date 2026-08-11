# Acceptance and evidence matrix

## Audited baseline — 2026-08-09

The repository currently records 230 completed and 3 pending roadmap checkboxes. The
latest complete Python gate is 933 passed with 12 explicit skips: unsafe active-document
COM writes, opt-in disposable AutoCAD acceptance, and opt-in live performance.
Ruff formatting and lint pass, 27 generated schemas are current, and strict type checking
passes across 189 source files. The pure C# bridge suite passes 144/144, the AutoCAD 2027
R26/.NET 10 plugin builds with zero warnings, and the semantic golden runner passes 247 tests.

This baseline proves the implemented offline Python/C# surfaces are internally
consistent. Disposable AutoCAD Mechanical 2027 evidence now proves bridge load, semantic
read, one-lock/transaction atomic commit, metadata readback, exact session undo and the
intervening-activity rejection fence. It does not prove a signed deployment, durable DWG
checkpoint replacement, company drawing coverage, detailed COM geometry or pilot effectiveness.

## Capability status

| Capability | Current evidence | Completion evidence still required |
|---|---|---|
| Safe deterministic write core | Contracts, state machine, SQLite stores, writer lease, approval binding, validation, preview, fault matrix, fake end-to-end evidence and live R26 atomic commit/readback/session undo | Signed deployment acceptance and durable checkpoint-file replacement |
| Complex 2D creation | Ten deterministic feature families, modifiers/annotations/views, 34 complete synthetic semantic cases, configured performance gates, and live MCP commits for a base plate, flange and L-bracket on AutoCAD 2027 | 30–50 engineer-selected company drawings, real profiles and representative prompt/pilot evidence |
| Drawing read | `DrawingModel`, bounded DXF/bridge readers, live R26 bridge parity, PID-fenced live non-mutating COM summary/selection reader, recognition with ambiguity/provenance/round-trip and company-standard reconciliation | Engineer-selected production drawings; a detailed COM model remains an explicit capability gap |
| Takeoff and quotation data | Pure takeoff engine, versioned demo material table, mass/cut/pierce/hole/weld quantities, provenance, SQLite audit, five synthetic DXF cases, and one independently calculated live R26 plate result matching area/mass/cut length exactly | Independently calculated answers on engineer-selected drawings and an approved company material table |
| Audit and modification | Deterministic auditor plus persisted-evidence remediation compiler; exact selected-finding plans, semantic deletion refs, preview/approval/commit gates, stale blocking, durable checkpoint metadata, separate rollback approval, full post-commit re-audit, live selected-finding update/delete remediation and live session undo | Durable checkpoint-file replacement and engineer acceptance |
| Measurement | Twelve analytic measurements, cooperative terminal timeouts, provenance/revision/tolerance, properties/schemas, and live R26 MCP perimeter readback matching the independent rectangle reference | Engineer-selected production drawing comparisons |
| Image to drawing | ADR-016, bounded local PNG/JPEG/TIFF intake, calibrated deterministic line/circle/arc/contour trace, overlay, signed engineer candidate/layer acceptance, sealed draft spec, property/golden/DXF round-trip | Representative shop scans and live CAD readback accuracy |
| MCP/client experience | 22 typed tools, fail-closed permissions, PySide6 engineer approval, global local-MCP install configured for `dotnet_bridge`, and direct MCP stdio acceptance through inspect/compile/preview/validate/approval/commit/read/recognize/audit/measure/takeoff on AutoCAD 2027 scratch drawings | Restart desktop to load the updated global server; ChatGPT web requires a remote MCP/tunnel; production signing and company-drawing acceptance remain open |
| Production AutoCAD bridge | Python transport plus C# contracts, secured per-user pipe, typed router, atomic executor, durable restart-safe commit journal, stable metadata, bounded inspection, R26 plugin/development bundle, 144/144 pure tests and live R26 scratch acceptance | Signed release, clean-workstation install, durable checkpoint-file replacement and production drawing acceptance |
| Pilot effectiveness | Run-scoped append-only baseline/effort/operation evidence, click-marked engineer activity, explicit failed-case classification, quality and savings gates, generated report schema and properties through task 27 | Representative engineer baseline and live pilot results meeting configured thresholds |
| Operational readiness | Windows CI definition, import/static gates, compatibility matrix, retention, timeout/process isolation, performance suite, bundle packager and manual gates | Real CI run, organisation signing identity/certificate/timestamp, install/rollback on a clean AutoCAD workstation |

The synthetic regression corpus is intentionally not production evidence. Run
`uv run python scripts/check_production_golden_acceptance.py <manifest>` for the separate
fail-closed production gate; the current repository correctly fails it until approved external
drawings, provenance, reviewers, company profiles/materials and independent takeoff answers exist.

## Claims and minimum proof

| Claim | Minimum acceptable proof |
|---|---|
| “Can design a feature” | Deterministic spec-to-plan compile, preview, validation, adapter capability, normal/boundary/invalid goldens |
| “Can understand vague engineering prompts” | Structured clarification/assumption evaluation on representative engineer prompts with zero silent technical defaults |
| “Can read and analyze drawings” | DWG/DXF extraction coverage, unsupported-entity reporting, recognition ambiguity, provenance, read-only revision invariance |
| “Can produce takeoff for quotation” | Independently checked area/mass/cut/pierce/hole/BOM values and source entity references |
| “Can modify an existing drawing” | Selected finding to remediation plan to preview/approval/commit/re-audit, with stale-revision and idempotency tests |
| “Can convert an image” | Calibrated image goldens, overlay/diff, confidence and ambiguity handling, measured vector error, human acceptance |
| “Works in AutoCAD” | Tests executed in a declared supported AutoCAD version on disposable DWGs, including readback and cleanup |
| “Production ready” | All required gates pass, no required skips, package install/rollback succeeds, compatibility/pilot evidence meets configured thresholds |

Use `.codex/skills/implement-autocad-harness/scripts/project_audit.py` to refresh structural progress. Update this matrix only from current command output and acceptance evidence.

The sanitized live record is
[`evidence/live-r26-rollback-2026-08-09.json`](evidence/live-r26-rollback-2026-08-09.json).
It intentionally distinguishes the proven session `undo_group` path from an unimplemented
whole-document checkpoint replacement.

The isolated COM reader record is
[`evidence/live-com-reader-r26-2026-08-09.json`](evidence/live-com-reader-r26-2026-08-09.json).
Its process ownership fence prevented the live test from attaching to or closing the user's
existing AutoCAD process.

Direct MCP-to-AutoCAD evidence for three representative creation workflows is recorded in
[`evidence/live-mcp-r26-base-plate.json`](evidence/live-mcp-r26-base-plate.json),
[`evidence/live-mcp-r26-flange.json`](evidence/live-mcp-r26-flange.json), and
[`evidence/live-mcp-r26-l-bracket.json`](evidence/live-mcp-r26-l-bracket.json). Each record
binds the owned AutoCAD PID, document revision, plan hash, checkpoint/undo evidence, semantic
readback and persisted DWG digest; the pre-existing user AutoCAD process remained untouched.

The schema 1.11 live read/audit/measurement/takeoff record is
[`evidence/live-mcp-r26-read-audit-measure-takeoff-1-11.json`](evidence/live-mcp-r26-read-audit-measure-takeoff-1-11.json).
For the 184 × 115 × 13.8 mm SS400 plate with four Ø16.1 holes, its independent analytic
reference exactly matches the observed 20,345.667768 mm² net area, 598 mm outer perimeter,
800.318567 mm total cut length and 2.204 kg rounded unit mass. The demo material table remains
explicitly unapproved and this single synthetic live case does not replace engineer-selected
production takeoff evidence.

The schema 1.12 direct-MCP remediation record is
[`evidence/live-mcp-r26-remediation-1-12.json`](evidence/live-mcp-r26-remediation-1-12.json).
On a PID-owned disposable AutoCAD 2027 drawing, the harness committed the 25-entity plate,
committed the same approved plan again to create a deterministic 50-entity duplicate case, then
submitted two persisted audit findings through `cad_change_submit`. The remediation preview and
live commit contained exactly one deletion and one layer update, readback contained 49 entities,
and re-audit contained neither selected `(rule_id, entity_ref)` pair. The record stores only hashes
of selected entity references, no approval token, and confirms that no AutoCAD process remained.

The exact external artifacts needed to close the remaining roadmap gates are listed in
[`engineer-acceptance-intake.md`](engineer-acceptance-intake.md).
