# ADR-022 - Bounded reference geometry and planning-only MCP sessions

- **Status:** accepted
- **Date:** 2026-08-16
- **Deciders:** harness maintainers

## Context

The adapters already implement typed circle creation, preview, validation, approval,
idempotency, and post-commit measurement. However, the feature catalog had no public
standalone-circle intent. An engineer could say "draw R20 mm at the origin on layer 0" and
the client still had no legal way to compile the request. The resulting workaround was
either repeated clarification or model-generated AutoLISP/command scripts. Both make a
simple drafting task slower; unrestricted scripts also bypass the harness safety model.

The existing read-only client profile created a second UX dead end: it could inspect a
drawing but could not create internal job state or a non-mutating preview. Giving the
permanent AI registration access to `cad_commit` merely to enable planning would grant
more surface than the task needs.

## Decision

Advance the public contract from schema `1.12` to `1.13`. The change is additive,
but the bridge handshake remains exact-version and fail-closed: Python clients, the
C# bridge, generated schemas, and explicit allowlists must be refreshed together.

Add a bounded public `reference_circle` feature. It accepts one explicit center (or the
DrawingSpec datum), a positive radius, and a layer declared by the selected company
profile. It compiles to the existing typed `create_circle` operation. It does not accept
source code, command strings, arbitrary operation plans, or a sequence of primitive CAD
commands.

Add a planning-only MCP permission mode and a high-level `cad_change_prepare` operation.
Planning mode may inspect, create internal jobs, compile a typed spec, render temporary
previews, validate, and produce a semantic diff. It cannot commit, roll back, or export.
`cad_change_prepare` keeps an explicit job id and performs submit, preview, pre-commit
validation, and diff without changing the active DWG.

Keep one human approval bound to the exact plan hash and document revision before any
live write. Setup confirmations are adapter-specific: a COM session never asks an
engineer to confirm a bridge bundle or Named Pipe ACL.

## Determinism and input policy

The reference-circle compiler copies explicit engineering values; it does not infer a
hole function, tolerance, fit, material, mating relationship, or manufacturing intent.
Missing radius or placement remains `needs_input`. An explicit radius unit, origin such as
`[0, 0]`, and declared layer such as `0` are complete inputs and must not trigger more
questions. An omitted unit can be resolved from inspected CAD context only when the active
drawing and selected standards profile report the same known unit; otherwise one unit question
is a necessary scale boundary, not a drafting preference.

The same input spec, profile, document revision, and schema always produce the same
operation and plan hash. The adapter independently measures the created center and radius
after commit.

## Consequences

- Simple standalone circles no longer need a bespoke mechanical-feature compiler or a
  generated script.
- Permanent AI registrations can prepare reviewable work without holding a DWG mutation
  capability.
- The public MCP tool count and permission vocabulary grow additively; clients with an
  explicit allowlist must opt into the new tool.
- Existing schema `1.12` peers fail closed until rebuilt or refreshed for `1.13`.
  Because the schema version participates in plan hashing and rollback approval claims,
  plans, approvals, and rollback tokens from an older schema must be regenerated.
- This decision does not authorize arbitrary lines, polylines, trim/extend operations, or
  code execution. Each future quick-edit intent needs a bounded semantic contract and
  independent measurement coverage.
- AutoCAD-version support is still evidence-based per adapter/runtime tuple. Adding this
  feature does not turn a provisional version into a verified writer.

## Alternatives considered

- **Generate and execute AutoLISP, Python, SCR, or `SendCommand`:** rejected because it
  bypasses closed schemas, revision binding, preview validation, approval scope,
  idempotency, transaction/undo policy, and post-write measurement.
- **Expose `draw_circle` as a primitive MCP tool:** rejected because a model could assemble
  unreviewed operation sequences and become the geometry kernel.
- **Require a new manufacturing feature for every circle:** rejected because reference
  geometry has no implied hole/shaft/manufacturing semantics.
- **Grant the permanent client the approval-required/full profile:** rejected because
  planning does not require exposing commit or rollback tools.

## Revisit when

Add more bounded quick-edit intents only when each has a closed input model, deterministic
compiler, preview and validation coverage, and adapter-independent post-readback
measurements. Revisit arbitrary scripting only if it can meet the same guarantees without
expanding the trusted code-execution boundary.
