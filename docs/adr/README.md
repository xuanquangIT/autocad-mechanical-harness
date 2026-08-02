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
