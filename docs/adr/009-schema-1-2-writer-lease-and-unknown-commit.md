# ADR-009 - Schema 1.2: writer leases and unknown commit state

- **Status:** accepted
- **Date:** 2026-08-04
- **Deciders:** harness maintainers

## Context

Requirement 2 introduces a public `WriterLease` contract and makes an indeterminate CAD write a durable
job state. A client must distinguish that state from `FAILED`, because retrying it could duplicate entities.
Both additions extend public enums/models, and the repository requires a minor schema bump and regenerated
contracts for every public additive change.

## Decision

Bump `SCHEMA_VERSION` from 1.1 to 1.2, publish `writer-lease.schema.json`, and add
`UNKNOWN_COMMIT_STATE` to `JobState`. The state is terminal for automatic commit; recovery is read-only
reconciliation.

## Consequences

Freshly compiled plans hash differently because `schema_version` remains part of `plan_hash`; outstanding
approvals must be regenerated. Stored pre-1.2 payloads retain their original version and hash. Lease expiry
or renewal uncertainty can now stop automatic progress even when the adapter later reports success, trading
availability for protection against duplicate writes. Reconciliation reads CAD state but never commits.

## Alternatives considered

- Treat an uncertain write as `FAILED`: rejected because normal retry could duplicate entities.
- Keep `WriterLease` internal: rejected because bridge/process boundaries need one versioned representation.
- Exclude schema version from plan hashing: rejected because approval must bind to the exact contract.

## Revisit when

A distributed deployment replaces SQLite leases, or reconciliation can prove outcomes strongly enough to
support an explicit engineer-authorized recovery transition.
