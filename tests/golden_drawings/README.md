# Golden drawing cases

Semantic comparison, not byte comparison. A DWG's bytes change with metadata and
serialization details even when the drawing is engineering-identical, so these cases
compare operations, measurements, layers and validation outcomes instead
(architecture sections 22.4 and ADR-006).

## Case layout

```
<case-name>/
├── input_spec.json                  # the DrawingSpec submitted
├── company_profile.yaml             # exact profile ref and approval status
├── expected_plan.json               # operations, expectations, plan_hash
├── expected_semantic_entities.json  # entities and measurements after commit
├── expected_validation.json         # findings by rule and severity
└── preview_reference.svg            # stable human visual reference
```

The SVG must exist and parse, but pixels never decide engineering pass/fail. The semantic
comparator matches entity multisets, layers, styles and exact measurement key sets within the
profile tolerance while ignoring handles and timestamps.

Five or more take-off cases additionally contain `input_drawing.dxf`, `takeoff_request.json`
and `expected_takeoff.json`. Their reports are compared semantically. Compilation and validation
negative cases live under `_negative/` and do not count toward the 30 complete production cases.

The `extended_*` fixtures are deterministic synthetic coverage. Before a release is labelled
production-ready, a mechanical engineer must replace or supplement them with independently selected
shop drawings and independently calculated take-off expectations.

## Adding a case

Each new feature needs at least three cases (Definition of Done, section 29):

1. **normal** - a realistic part
2. **boundary** - minimum edge distance, single hole, largest supported size
3. **invalid** - must be rejected, with the expected `error_code` recorded

Regenerate expectations with:

```powershell
uv run python scripts/run_golden_tests.py --update
```

Review the diff before committing. A changed `plan_hash` means the geometry, profile or
operation vocabulary changed, and prior approvals for that plan are void.
