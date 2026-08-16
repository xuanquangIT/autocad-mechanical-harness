# Architectural Decision Records

ADR-001 through ADR-007 are recorded in section 30 of the architecture document:

- [Vietnamese](../AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.vn.md#30-architectural-decision-records)
- [English](../AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md#30-architectural-decision-records)

| ADR | Decision |
|---|---|
| 001 | Python-first |
| 002 | COM is a temporary adapter for the MVP |
| 003 | C# bridge for production |
| 004 | High-level MCP tools only |
| 005 | Mandatory human approval |
| 006 | Semantic golden testing, not byte comparison |
| 007 | C++/ObjectARX is out of scope for the MVP |

New decisions get their own file here: `NNN-short-title.md`.

| ADR | Decision | File |
|---|---|---|
| 008 | Schema 1.1: additive contract expansion for the production roadmap | [008-schema-1-1-contract-expansion.md](008-schema-1-1-contract-expansion.md) |
| 009 | Schema 1.2: writer leases and unknown commit state | [009-schema-1-2-writer-lease-and-unknown-commit.md](009-schema-1-2-writer-lease-and-unknown-commit.md) |
| 010 | Schema 1.3: versioned outline modifiers | [010-schema-1-3-outline-modifiers.md](010-schema-1-3-outline-modifiers.md) |
| 011 | Schema 1.4: annotation, views, and GD&T | [011-schema-1-4-annotation-views-gdt.md](011-schema-1-4-annotation-views-gdt.md) |
| 012 | Schema 1.5: drawing read contract | [012-schema-1-5-drawing-read-contract.md](012-schema-1-5-drawing-read-contract.md) |
| 013 | Recognition round-trip stays semantic | [013-recognition-round-trip-safety.md](013-recognition-round-trip-safety.md) |
| 014 | Pilot metrics contract and measurement semantics | [014-pilot-metrics-contract.md](014-pilot-metrics-contract.md) |
| 015 | Schema 1.8: explicit bridge cancellation | [015-schema-1-8-bridge-cancellation.md](015-schema-1-8-bridge-cancellation.md) |
| 016 | Schema 1.9: calibrated raster intake is an untrusted read path | [016-calibrated-raster-intake.md](016-calibrated-raster-intake.md) |
| 017 | Schema 1.10: separate human approval for destructive rollback | [017-separate-rollback-approval.md](017-separate-rollback-approval.md) |
| 018 | Session-bound undo rollback and activity fence | [018-session-undo-rollback-fence.md](018-session-undo-rollback-fence.md) |
| 019 | Schema 1.11: explicit cross-layer take-off contours | [019-explicit-takeoff-contours.md](019-explicit-takeoff-contours.md) |
| 020 | Schema 1.12: remediation submission and restart evidence | [020-schema-1-12-remediation-submission.md](020-schema-1-12-remediation-submission.md) |
| 021 | Trust-anchored production evidence attestations | [021-production-evidence-attestations.md](021-production-evidence-attestations.md) |
| 022 | Schema 1.13: bounded reference geometry and planning-only MCP sessions | [022-bounded-reference-geometry.md](022-bounded-reference-geometry.md) |

## Template

```markdown
# ADR-008 - Title

- **Status:** proposed | accepted | superseded by ADR-00N
- **Date:** YYYY-MM-DD
- **Deciders:** names or roles

## Context

What forced a decision. Include the constraint that makes this non-obvious.

## Decision

What we will do, in one or two sentences.

## Consequences

What this costs us, not only what it buys. Include the new failure modes.

## Alternatives considered

What was rejected and why. A rejected option with no stated reason is not a decision.

## Revisit when

The condition that should reopen this. ADR-007 is the model: revisit when custom
entities, deep native graphics or extreme performance are needed.
```
