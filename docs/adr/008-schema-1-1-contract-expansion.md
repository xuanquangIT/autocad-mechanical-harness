# ADR-008 - Schema 1.1: additive contract expansion for the production roadmap

- **Status:** accepted
- **Date:** 2026-08-03
- **Deciders:** harness maintainers

## Context

The `cad-ai-production-roadmap` spec opens up the read direction (`Drawing_Reader`,
`Feature_Recognizer`, `Takeoff_Engine`), per-client tool permissions and the
`Annotation_Engine`. All three need vocabulary the 1.0 contract does not have, and the
first phase of the roadmap has now added it:

- `ErrorCode` gained `TOOL_NOT_ALLOWED`, `UNSUPPORTED_INPUT_FORMAT` and
  `READ_SCOPE_TOO_LARGE`, each with an exception class carrying a
  `default_required_action`.
- `OperationType` gained `CREATE_ANGULAR_DIMENSION` and `CREATE_HATCH`, with matching
  `ENTITY_TYPE_BY_OPERATION` entries; `ValidationStage` gained `DRAWING_AUDIT` and
  `DRAWING_STANDARD`; `AuditEventType` gained `SPEC_CHANGED`, `TOOL_CALL_REJECTED`,
  `DRAWING_READ`, `TAKEOFF_REPORT_CREATED` and `DRAWING_AUDITED`.
- `ValidationReport` gained `entities_examined` and `company_approved`;
  `AdapterStatus` gained `version_supported`.
- The annotation and layout vocabulary landed on the profile side: `CompanyProfile`
  gained `annotation_rules`, `layout_rules` (holding `view_spacing_mm`),
  `title_block_fields`, `dwt_ref`, `dws_ref` and `material_profile_ref`;
  `ToleranceProfile` gained `arc_chord_tolerance_mm`; `CompileContext` gained
  `parent_outline`.
- `JobStore` grew `map_entity`, `entity_mappings_for`, `save_checkpoint` and the
  approval methods, plus a new `EntityMappingRecord` contract model.

`ErrorCode` is a client-facing contract: an MCP client branches on the code, and the C#
bridge echoes it in the IPC envelope. A peer on 1.0 has no branch for
`READ_SCOPE_TOO_LARGE`. That is what makes this non-obvious - none of the individual
additions break an existing payload, but together they change what a peer must be
prepared to receive. `contracts/README.md` says a minor bump may only add optional
fields, and every one of these additions satisfies that. What it does not say is who is
allowed to make the bump silently, and a bump moves `plan_hash`, so it needs a record.

## Decision

Bump `SCHEMA_VERSION` from `1.0` to `1.1` once for the whole phase-1 expansion, and
regenerate `contracts/*.schema.json`. The additions listed above are additive only: no
field changed meaning, no field was removed, no enum member was renamed or reused. The
still-pending `Annotations` additions of the same roadmap (`hole_table`, `gdt`, `views`,
task 12) are part of this same 1.1 minor and do not warrant a second bump - they are
optional fields on an existing model, filled by the caller, defaulted to "off".

## Consequences

**`plan_hash` moves, and that is the cost of this ADR.** Two distinct effects, kept
separate because conflating them is how an approval flow quietly breaks:

- Extending an enum does *not* change the hash of an existing plan. A stored
  `OperationPlan` payload contains only the operations it actually used, so adding
  `CREATE_HATCH` to `OperationType` leaves every previously stored plan byte-identical
  and its recomputed hash unchanged. Same for the new `ValidationStage`,
  `AuditEventType` and `ErrorCode` members, which never appear in a plan at all. The
  design's `plan_hash` table records this as "không, tới khi được dùng".
- The bump itself *does* change the hash of a re-compile. `OperationPlan.schema_version`
  is a plan field and is not in `VOLATILE_FIELDS`, so it is hashed. Verified: the same
  plan hashes differently under `1.0` and `1.1`. Plans already persisted keep
  `schema_version: "1.0"` in their stored JSON, so `verify_hash` on stored plans and the
  approvals bound to them still agree. But recompiling the *same* `DrawingSpec` after
  this bump produces a plan whose hash no longer matches a pre-bump approval, and the
  commit is refused with `PLAN_HASH_MISMATCH`. This is the intended behaviour of
  non-negotiable 4, not a regression: re-approve after the bump.

Other consequences:

- Any in-flight approval issued before this bump cannot be reused for a fresh compile.
  Anyone with an open approval must re-approve.
- Adapters reject unknown *majors*, so a 1.0 peer keeps working against 1.1 payloads
  until it meets a field or an error code it does not know. A 1.0 client that receives
  `TOOL_NOT_ALLOWED` will fall through to its generic error path rather than showing the
  `allowed_tools` remedy. That is a degraded message, not a wrong outcome.
- `contracts/operation-plan.schema.json` and
  `contracts/validation-report.schema.json` changed, so the C# bridge must be
  regenerated or its schema-match test will fail.
- Config additions (`read`, `takeoff`, `measure`, `lease`, `mcp.client_profiles`,
  `bridge`, `compatibility`, `pilot`) are deliberately excluded from this decision.
  Config is not a wire contract, it does not carry `schema_version`, and it produced no
  schema diff. Confirmed against the regenerated contracts.

## Alternatives considered

- **One bump per contract change.** Rejected: phase 1 touches ten models and would
  produce ten minors and ten ADRs, each invalidating approvals again. Batching the
  additive changes of one phase into one minor keeps the number of approval-invalidating
  events at one.
- **Bump to 2.0.** Rejected: nothing here changes a field's meaning or removes one, so a
  major bump would force peers to reject payloads they can in fact read, for no gain.
- **Exclude `schema_version` from `plan_hash` to avoid invalidating approvals.**
  Rejected, and worth being explicit about. It would mean an approval issued under one
  contract could authorise a commit compiled under another. The version is part of what
  the engineer approved.
- **Add error codes without a bump, on the grounds that no payload shape changed.**
  Rejected: a client branching on `ErrorCode` cannot discover new members without a
  version signal, which is exactly what the version is for.

## Revisit when

A roadmap phase needs a contract change that is *not* additive: renaming an
`ErrorCode` member, removing a field, or changing what an existing field means. That is
a major bump with a migration window (server supports current and previous major), and
it needs its own ADR. Also revisit if a second phase-1-sized batch appears, to decide
whether it rides on 1.1 or takes 1.2.
