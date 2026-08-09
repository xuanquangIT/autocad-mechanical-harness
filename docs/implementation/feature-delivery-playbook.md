# Feature delivery playbook

## 1. Establish the engineering contract

Write the feature in the language a mechanical engineer would use. Separate required inputs, profile-approved defaults, and derived values. Missing dimensions, units, datum, material, thickness, tolerance class, hole count, diameter, or PCD must produce `needs_input`; none may be guessed from a vague prompt.

Map the change to requirement acceptance criteria, design correctness properties, and task IDs. Record an ADR before implementing a deliberate conflict with the current architecture.

## 2. Preserve the two-path architecture

The read path is read-only:

`DWG/DXF/image intake -> DrawingModel -> recognition/takeoff/audit/measurement`

The write path is gated:

`DrawingSpec -> deterministic compiler -> OperationPlan -> preview -> validation -> approval -> commit -> readback measurement`

The only bridge from analysis to modification is a remediation plan that enters the gated write path. No service, MCP tool, COM adapter, or raster tracer may bypass it.

## 3. Implement from pure core outward

1. Define frozen, versioned domain models and stable error codes.
2. Implement pure geometry or comprehension functions with explicit tolerances and units.
3. Compile high-level mechanical meaning to stable operation IDs and deterministic ordered geometry.
4. Add validation findings with expected, actual, tolerance, evidence, and suggested action.
5. Add application orchestration without importing infrastructure into the domain.
6. Add persistence and adapters with capability preflight and exception containment.
7. Expose high-level MCP/CLI/UI operations only when lower layers are complete.
8. Regenerate schemas and migrations; update compatibility and operational docs.

## 4. Handle vague prompts safely

Translate a vague idea into a structured clarification record before creating geometry:

- intended part/function and manufacturing process;
- canonical units and scale;
- governing datum and coordinate orientation;
- envelope and critical interfaces;
- material, thickness, fits/tolerances, surface and drawing standard;
- required views, dimensions, notes, title block, and output format;
- explicit assumptions with provenance and impact.

Only profile-authorized defaults may be applied. Every other unresolved engineering choice remains blocking.

## 5. Handle images as untrusted measurements

Raster-to-drawing extends the original DWG/DXF-only scope and therefore requires an ADR and explicit acceptance rules. The intake pipeline must:

1. Hash and preserve the source image and record pixel dimensions.
2. Require a scale reference, known dimension, or calibrated capture; otherwise output unitless geometry and block production commit.
3. Detect/trace candidate lines, arcs, circles, contours, text, and dimensions with confidence and source regions.
4. Normalize candidates through deterministic geometry code and flag ambiguous intersections, occlusions, perspective, and low resolution.
5. Produce an overlay and semantic diff against the source for engineer review.
6. Convert accepted candidates into `DrawingSpec`/`DrawingModel`; never write traced primitives straight to DWG.
7. Run the standard preview, validation, approval, commit, and post-commit measurement path.

## 6. Prove the feature

Provide the smallest exact unit examples, one property test per design property, contract round-trips, normal/boundary/invalid golden cases, relevant fault injection, and adapter capability tests. For COM or bridge changes, add a real-AutoCAD test with a disposable drawing and verify revision, transaction/undo behavior, metadata, created-entity count, and independent readback measurements.

Do not mark a roadmap checkbox complete until every acceptance criterion in that checkbox has direct evidence.
