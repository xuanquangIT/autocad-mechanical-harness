# ADR-015: Schema 1.8 explicit bridge cancellation

- **Status:** accepted
- **Date:** 2026-08-09
- **Deciders:** AutoCAD Mechanical Harness maintainers

## Context

Returning a timeout from Python while a worker thread or AutoCAD handler keeps running
does not satisfy the cancellation invariant. Closing a client pipe also cannot prove that
the server-side transaction stopped before its irreversible commit boundary.

## Decision

Schema 1.8 adds the monomorphic IPC method `cancel`. A cancellation request identifies one
active `request_id`; the bridge cancels only that handler and does not acknowledge the
control request until the target handler is terminal. Execution propagates a cancellation
token through inspection and transaction loops. A commit without a proven pre-commit
terminal cancellation is reported as `UNKNOWN_COMMIT_STATE`, never guessed to be a safe
timeout.

## Consequences

- The Python transport needs a second local pipe connection for cancellation while the
  original request is active.
- The bridge listener must accept concurrent local connections under the same per-user ACL.
- Every operation must checkpoint cancellation before side effects and immediately before
  transaction commit.
- Consumers must accept the new 1.8 minor contract version.

## Alternatives considered

Daemon worker threads were rejected because Python cannot stop a blocked native/COM call.
Treating pipe close as cancellation was rejected because disconnect does not prove the
AutoCAD handler or transaction has terminated.

## Revisit when

The transport moves to an OS primitive that provides authenticated cancellation and a
terminal outcome acknowledgement as part of its native request protocol.
