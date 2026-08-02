# Engineer desktop (planned)

A PySide6 window that is the engineer's approval surface while the adapter is COM. Once
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
```
