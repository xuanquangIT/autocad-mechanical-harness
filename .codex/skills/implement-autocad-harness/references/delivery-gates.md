# Delivery gates

Select all gates affected by the change; do not substitute a narrower test for a required higher-level gate.

| Change | Required evidence |
|---|---|
| Pure geometry | Exact unit examples, relevant property invariant, Ruff, strict mypy |
| Feature compiler/modifier | Missing-input unit tests, property tests, normal/boundary/invalid golden cases, preview mapping, adapter preflight |
| Public contract | Schema regeneration/check, JSON round-trip, compatibility test, version/ADR review |
| Persistence/migration | Repository tests, restart round-trip, migration idempotency, lock/retry fault tests |
| MCP tool | Tool-set partition, input/output schema, envelope/exception containment, permission profile, audit event |
| Read/comprehension | Non-mutation proof, provenance completeness, ambiguity and unsupported-entity cases |
| Takeoff/measurement | Independent analytic reference, units/ranges, rounding, provenance, JSON/CSV artifact policy |
| Remediation/write | Exact finding scope, preview and approval binding, stale-revision rejection, idempotency, readback measurement |
| Raster intake | Source hash, scale calibration, confidence/ambiguity output, deterministic normalization, traced-vs-source comparison, human acceptance |
| COM/bridge | Contract suite plus real AutoCAD test for document lock, atomicity, metadata, revision, readback, undo group |
| Release/client install | Clean package build, checksums, fresh-environment smoke, MCP discovery, representative end-to-end prompts |

## Gate order

1. Run focused unit/property tests while iterating.
2. Run Ruff and strict mypy over `src` and `apps`.
3. Run the complete non-CAD suite.
4. Check generated schemas and golden cases.
5. Run fault/performance/package checks when implemented.
6. Run COM/bridge tests only against an explicitly configured disposable drawing.
7. Perform MCP-client acceptance with the fake adapter first, then a separately approved real-AutoCAD session.

Treat every skip as an open evidence item. Record environment prerequisites rather than converting a skip into a pass.
