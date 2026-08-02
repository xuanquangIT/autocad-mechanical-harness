# Golden drawing cases

Semantic comparison, not byte comparison. A DWG's bytes change with metadata and
serialization details even when the drawing is engineering-identical, so these cases
compare operations, measurements, layers and validation outcomes instead
(architecture sections 22.4 and ADR-006).

## Case layout

```
<case-name>/
├── input_spec.json                  # the DrawingSpec submitted
├── expected_plan.json               # operations, expectations, plan_hash
├── expected_semantic_entities.json  # entities and measurements after commit
└── expected_validation.json         # findings by rule and severity
```

`preview_reference.svg` may be added for visual review. It is never used to decide
pass/fail: that comes from measurements and rules.

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
