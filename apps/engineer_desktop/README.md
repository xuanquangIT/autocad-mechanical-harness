# Engineer desktop

A PySide6 window is the engineer's approval surface while the adapter is COM. Once
the C# bridge ships, an AutoCAD PaletteSet replaces it, but the contract is the same.

## Required content (architecture section 19.1)

- Active document and its revision
- Job state
- Spec parameters as submitted
- Missing inputs, if any
- Defaults applied, each with source and version
- Assumptions the system made
- Before/after preview
- Validation findings with expected, actual and tolerance
- `Accept`, `Reject`, `Commit`, `Rollback`, gated by permission

## Design constraints

- Approval is bound to one `plan_hash` and one `expected_revision`. If either changes
  while the window is open, the approval button must disable and say why.
- The AI client must not be able to drive this surface. That separation is the reason
  approval means something.
- Warnings must be acknowledged explicitly and recorded in the approval record; they
  are not dismissible without a trace.
- Never render a "compliant" badge while `cad_status` reports the loaded profile is not
  company approved.

## Install

```powershell
uv sync --extra ui
uv run cad-harness-desktop <job_id> --engineer <engineer-id>
```

The desktop imports and calls `HarnessService` directly; it is not an MCP client. The
approval token is held only inside the controller as a masked in-memory secret, is
consumed by commit, and is never placed in a widget, clipboard call, log or view model.

Rollback has a separate two-step **Review rollback** / **Execute approved rollback**
flow. The warning dialog shows the exact job, document, checkpoint and current revision.
Its short-lived `rb1` token is independently signed, kept only in controller memory and
cannot be issued through MCP; commit approval never authorizes rollback.

## Measured pilot session

Register a baseline only from a start/end measurement taken in one session, then attach
the approval window to that case:

```powershell
uv run cad-harness-desktop <job_id> `
  --engineer engineer_17 `
  --pilot-case-id case_B_001 `
  --register-pilot-baseline `
  --pilot-group B `
  --pilot-work-label ve_moi `
  --pilot-manual-minutes 42.7 `
  --pilot-measured-by engineer_17 `
  --pilot-manual-single-session
```

Use **Start effort** / **Stop effort** around engineer review activity, enter any
post-commit manual fix-up separately, then click **Finalize pilot effort**. The data is
appended to the configured local `pilot.run_id`; a duplicate case is rejected rather
than overwriting the baseline. Only opaque IDs, timestamps, counts and durations are
stored—never prompts or drawing geometry.

If the case cannot be committed, select a finite failure classification and click
**Finalize failed case**. This deliberately records `completed=false`; it cannot be
used for a committed job and it remains bound to the job opened by this desktop session.
