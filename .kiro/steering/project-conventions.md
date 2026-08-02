# AutoCAD Mechanical Harness - working conventions

Authoritative architecture: `docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md`
(Vietnamese original: `docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.vn.md`). When this file
and the architecture document disagree, the architecture document wins.

## Commands

```powershell
uv sync --all-extras                                  # install
uv run pytest -q                                      # full suite, no AutoCAD needed
uv run pytest -m "not integration and not com"        # explicitly skip CAD-dependent tests
uv run ruff check . ; uv run ruff format .            # lint and format
uv run mypy                                           # type check src/cad_harness
uv run python scripts/generate_schemas.py --check     # contracts up to date?
uv run python scripts/run_golden_tests.py             # golden cases
uv run cad-harness status                             # adapter and profile
uv run cad-harness demo                               # reference base-plate case
```

Python 3.12. Shell is PowerShell: use `;` between commands, not `&&`.

## Layering

```
apps/ -> application/ -> domain/ -> geometry/ + validation/
adapters/ implement domain ports.
```

- `cad_harness.domain` must not import MCP, COM, AutoCAD, SQLAlchemy, ezdxf or UI code.
- `win32com` and `pythoncom` may only be imported in `adapters/autocad_com.py`. Ruff
  enforces this (banned-api). Do not add a second `# noqa: TID251` site.
- Adapters contain no business rules. If you are tempted to decide a layer, a tolerance
  or a default inside an adapter, it belongs in the compiler or the profile.

## Non-negotiables

These are invariants, not preferences. Breaking one is a bug even if tests pass.

1. **The LLM is not the geometry kernel.** Coordinates come from `geometry/`, always.
2. **No silent defaults.** A default needs value, source, version and impact, and must
   be declared in a company profile's `allowed_defaults`. Sizes, datums, hole counts,
   diameters, PCDs, tolerance classes and units are never defaulted.
3. **Preview never modifies the live drawing.**
4. **Commit requires an approval bound to one exact `plan_hash` and revision.**
5. **A stale revision rejects the commit.** No exceptions, no override flag.
6. **The same idempotency key never creates duplicate entities.**
7. **Committed entities are read back and re-measured.** An adapter that echoes
   `expected` back as its measurement makes post-commit validation worthless.
8. **Never compare floats with `==`** in geometry. Use a `ToleranceProfile` predicate.

## Determinism

The plan hash is the spine of the approval flow. Anything that changes it invalidates
approvals, which is correct - but it must never change for an unrelated reason.

- Excluded from the hash: `plan_hash`, timestamps, `request_id`, `plan_id`, `job_id`.
- Included: document id, expected revision, profile ref, operations, expectations.
- Operation and vertex order are semantic. Do not sort them "for tidiness".
- `operation_id` values are stable strings (`op:<feature_id>:<suffix>`), not counters.

## Adding a feature to the catalog

Follow the Definition of Done (architecture section 29). In practice:

1. `feature_catalog/<name>/compiler.py` with `validate_inputs` and `compile`.
2. `validate_inputs` reports every missing input; `compile` refuses to run while any
   remain.
3. Register it in `feature_catalog/__init__.py`. Registration is the claim that it
   works - leave it out and it stays in `PLANNED_FEATURES`.
4. Validation rules with `expected`, `actual` and `tolerance` on every finding.
5. Unit tests, property tests, and three golden cases: normal, boundary, invalid.
6. An adapter mapping, or an explicit `AdapterCapabilityMissingError`.

## Error handling

- Raise a `HarnessError` subclass with a stable `ErrorCode`, a `required_action`, and
  `details` free of stack traces and absolute paths.
- The MCP boundary maps exceptions to the envelope. Tool functions return the envelope;
  they never raise.
- `UNKNOWN_COMMIT_STATE` is never retryable. Reconcile from job records instead.

## Security

- Never log full prompts, whole geometry, raw customer paths or approval tokens.
- Export targets go through `security.paths.ensure_path_allowed`; no overwrite by
  default.
- Treat drawing content as customer IP: local-only by default, redaction on.

## Style

- Type hints everywhere; `ContractModel` (frozen, `extra="forbid"`) for wire models.
- Comments explain *why*, not *what*. Prefer a comment on a non-obvious constraint over
  a comment restating the code.
- No new dependency without a reason recorded in the PR.

## Every change should answer

- Which contract changed?
- Which rule was added?
- Which test proves determinism?
- Is there a new side effect?
- Is approval or security affected?
- Did any COM or AutoCAD dependency leak into the domain?
