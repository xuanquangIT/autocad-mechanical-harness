# ADR 017: Schema 1.10 and separate human approval for destructive rollback

Status: Accepted

## Context

`cad_rollback` discards drawing changes after a checkpoint. The previous service path
accepted only `job_id`, so a client allowed to call the tool could restore a drawing
without a distinct human decision. Commit approval cannot safely be reused because it
authorizes creating one exact plan, not deleting subsequent work.

## Decision

Rollback uses a separate `rb1` HMAC token namespace. The signed claims bind exactly one
`job_id`, `document_id`, `checkpoint_id`, current document revision, identified
engineer, issue time and expiry. The Engineer Desktop controller is the issuance
surface; MCP can consume but cannot issue the credential. The token is held in memory
and never persisted or audited; non-secret approval metadata is hash-chained in the
audit trail. The credential expires within fifteen minutes.

The public contract advances from schema 1.9 to 1.10 for
`RollbackApprovalRecord` and the exact `RollbackRequest` bindings.

`HarnessService.rollback` verifies signature before revealing scope details, rejects
expired or changed scope, and acquires the writer lease. For checkpoint replacement it
rechecks the current revision before calling the adapter. For the session-bound undo path,
the bridge rechecks the revision atomically under its command context and document lock;
the service deliberately emits no intervening AutoCAD status/read command. Rollback is
allowed only for a committed job or a failed job with a proven post-commit checkpoint.
`UNKNOWN_COMMIT_STATE` remains ineligible.

Post-commit validation failures persist the commit result, mappings, checkpoint and new
revision atomically so rollback authority survives process restart and idempotent commit
replay remains possible.

## Consequences

- Existing `cad_rollback(job_id)` callers must supply the reviewed checkpoint, current
  revision, and separate rollback approval token.
- Adapter rollback requests carry those bindings for strict bridge validation; adapters
  do not issue credentials.
- The live bridge undo path also requires a process-local receipt and an unbroken AutoCAD
  activity fence as specified by ADR 018. Durable checkpoint-file replacement is not
  implied by the `rb1` credential.
- The wired desktop Qt review/approve/execute flow still requires live UX acceptance
  before production rollout.
