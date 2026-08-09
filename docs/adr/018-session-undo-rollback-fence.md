# ADR 018: Session-bound undo rollback fence

Status: Accepted

## Context

AutoCAD's managed R26 API does not expose a durable identifier for the top entry of the
native undo stack. Calling `UNDO 1` after an unrelated command could therefore remove the
wrong user action even when the drawing content happens to have the expected revision.
The bridge also executes its host callback through an AutoCAD command wrapper, which may
place an empty command entry above the harness undo group.

## Decision

A successful fresh bridge commit registers an in-memory receipt bound to the bridge
process epoch, job, document, checkpoint, opaque undo-group identifier, previous revision
and committed revision. A new commit supersedes the preceding available receipt. Any
intervening `CommandWillStart` event invalidates an available receipt before rollback.
Restarting the plugin never reconstructs rollback authority from a checkpoint or commit
journal.

Rollback requires a separately signed `rb1` approval and the exact receipt scope. It runs
under the same write semaphore, AutoCAD command context and document lock used by commit.
The bridge checks the committed revision immediately before issuing a fixed, non-user-
derived `_.UNDO 1`. It accepts success only when inspection returns the exact pre-commit
revision. One additional fixed undo is permitted only when the first observation remains
the exact post-commit revision; a third revision or an unchanged second observation
quarantines the receipt. A completed request is replayable by approval ID without issuing
another command.

The Python service retains the matching receipt only in memory. For this path it does not
run a status or revision preflight, because either AutoCAD command would itself invalidate
the activity fence; the bridge performs the atomic revision check. Checkpoint-file restore
remains a distinct, unimplemented document-replacement workflow.

## Consequences

- Immediate rollback of the last untouched harness commit is supported and fail-closed.
- Rollback after plugin restart or any intervening AutoCAD command is deliberately rejected.
- The `checkpoint_id` remains recovery evidence, but the bridge does not claim that it can
  replace the active DWG from that file.
- The bridge advertises `rollback_undo_group`, not `checkpoint_restore`; a backend may
  declare the latter only after it implements an independently verified durable document
  replacement workflow.
- AutoCAD Mechanical 2027 R26 live scratch evidence covers commit, metadata readback,
  exact revision restoration, replay and activity-fence rejection. Signed release packaging
  and later checkpoint replacement remain separate acceptance gates.
