# ADR 020: Schema 1.12 remediation submission and restart evidence

## Status

Accepted.

## Context

The deterministic remediation compiler and post-commit re-audit existed inside the
application service, but no MCP contract could submit an engineer's selected audit
findings. Its selection was also held only in process memory, so a server restart
between planning and commit could lose the evidence required by Requirement 22.7.

## Decision

Advance the public contract from schema 1.11 to 1.12. Keep the 22-tool surface and
extend `cad_change_submit` with exactly one of:

- `spec`, for an ordinary revised `DrawingSpec`; or
- `remediation`, containing only a persisted `audit_id`, ordered exact
  `rule_id`/`entity_ref` selections, and documented technical inputs.

The MCP boundary never accepts a caller-supplied `DrawingModel`, operation plan, or
coordinates. The server freshly reads the pinned active drawing, recompiles the plan
from persisted audit evidence, and routes it through preview, validation, human
approval, commit, readback, and re-audit.

Persist the immutable `RemediationResult` with its exact plan hash in the job
aggregate. A restarted service reloads and verifies it before any writer side effect.
A remediation-shaped plan without matching evidence fails closed.

## Consequences

- Schema 1.11 bridge/client peers fail closed until rebuilt for 1.12.
- The remediation selection table is append-only with one record per job.
- Only `cad_commit` and `cad_rollback` can mutate a DWG; `cad_change_submit` writes
  internal planning state only.
- Existing 1.11 live evidence remains historical and is not rewritten.
