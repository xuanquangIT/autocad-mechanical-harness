# Authoring a feature

A feature is the unit of engineering meaning. Adding one is the main way this system
grows. The bar is the Definition of Done in section 29 of the architecture document;
this page is the practical walkthrough.

## 1. Decide what the feature *means*

Write the sentence an engineer would say. "A rectangular plate is an outline of a given
width and height, positioned from a datum, that may carry hole patterns." If you cannot
write that sentence, the feature is not ready to implement.

Then split its inputs into three groups:

| Group | Rule |
|---|---|
| Required engineering input | Missing means stop and ask. Sizes, datums, counts, diameters, PCDs, tolerance classes. |
| Profile default | May be applied, but only if the company profile declares it in `allowed_defaults`, and the provenance is recorded. |
| Derived value | The kernel computes it. Never ask the user for a hole coordinate. |

If you find yourself wanting a fourth group called "sensible default", stop. That is the
silent-default failure mode.

## 2. Add the geometry, if any

Pure functions in `src/cad_harness/geometry/`. No I/O, no AutoCAD, no randomness. Take a
`ToleranceProfile` for any comparison. Return immutable primitives.

Existing helpers: `bolt_circle`, `rectangular_grid`, `slot_outline`,
`segment_intersection`, `point_to_segment_distance`, `circles_overlap`.

## 3. Write the compiler

`src/cad_harness/feature_catalog/<name>/compiler.py` with two methods:

```python
def validate_inputs(self, feature: FeatureSpec, context: CompileContext) -> InputReport:
    report = InputReport()
    report.require(
        feature.parameters.get("width_mm") is not None,
        f"features[{feature.feature_id}].parameters.width_mm",
        "Width is a required plate size",
        "positive number in millimetres",
    )
    return report
```

`validate_inputs` must report **every** missing input, not the first one: the client
should be able to fix them in a single round trip.

`compile` re-runs `validate_inputs` and refuses to proceed while anything is missing.
It then emits:

- `Operation` objects, each with a stable `operation_id` (`op:<feature_id>:<suffix>`),
  a layer resolved through `context.layer_for(purpose)`, geometry, and an `expected`
  block that post-commit validation will check against.
- `ValidationExpectation` objects: the measurable claims your rules will verify.
- `DefaultRecord` and `Assumption` entries for anything not directly specified.

Order matters. Operation and vertex order feed the plan hash, so do not reorder them for
tidiness.

### Parent and child features

A child compiler receives `context.parent_box` and `context.parent_feature_id` via
`CompileContext.for_child(...)`. That is how a hole pattern resolves edge offsets against
the real outline instead of assuming one. Record `parent_feature_id` in your expectation
so the containment rules can find the outline.

## 4. Write the validation rules

`src/cad_harness/validation/rules/`. A rule is a frozen dataclass with `rule_id`,
`stages` and `evaluate`. Every finding carries `expected`, `actual` and `tolerance`,
plus a `suggested_fix` when there is an obvious one.

Severity, chosen deliberately:

| Severity | Meaning |
|---|---|
| `blocking` | Physically wrong or unmeasurable. Stops the next stage always. |
| `error` | Violates the standard or the compiled expectation. Stops commit by default. |
| `warning` | Needs a human decision. Must be acknowledged in the approval record. |
| `info` | Context. Notably: "could not check X", which is honest rather than a false pass. |

Register the rule in `validation/rules/__init__.py::all_rules`.

## 5. Map it in the adapters

Either add the operation type to the COM adapter's `_execute`, or let it raise
`AdapterCapabilityMissingError`. A silent skip is the one unacceptable option. Add the
type to `preview/dxf_writer.py` too, or list it in `unsupported_operations` so the
preview service reports the gap.

## 6. Test it

- **Unit:** exact coordinates for a reference case; one test per missing required input.
- **Property:** the invariant that must hold for all valid inputs. For a pattern, that is
  usually "count is exact" and "every point satisfies the defining equation".
- **Golden:** three cases minimum, in `tests/golden_drawings/<case>/`: normal, boundary
  (minimum edge distance, single hole, largest supported size), invalid (must be rejected
  with a recorded `error_code`).

Regenerate golden expectations with
`uv run python scripts/run_golden_tests.py --update` and read the diff. A changed
`plan_hash` is expected when geometry changes, and it correctly voids prior approvals.

## 7. Register it

Add it to `feature_catalog/__init__.py`. Registration is a claim that the feature works
end to end. Until then leave it in `PLANNED_FEATURES`, where `flange`, `slot` and
`l_bracket` currently sit: declared so a client can say "not supported yet" instead of
improvising.

## Checklist

- [ ] Feature schema and version
- [ ] Required and optional parameters documented
- [ ] No silent defaults
- [ ] Deterministic compile
- [ ] Validation rules with expected/actual/tolerance
- [ ] Dimension and annotation rules, if applicable
- [ ] Unit tests
- [ ] Property tests
- [ ] Three golden cases
- [ ] Preview support
- [ ] Adapter mapping or a declared capability gap
- [ ] Example spec in the docs
- [ ] Security and data-exposure review
