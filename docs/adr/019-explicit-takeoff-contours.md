# ADR 019: Schema 1.11 and explicit cross-layer take-off contours

## Status

Accepted.

## Context

A drawing can contain closed centreline, construction or annotation loops inside a
part outline. Pure geometric nesting cannot distinguish those loops from material
voids. Live AutoCAD acceptance exposed this failure when a rectangular centreline
pattern was subtracted from a base plate, materially understating area and mass.

At the same time, legitimate holes and cutouts may intentionally live on a different
layer from the selected outer contour. Inferring their role from layer names would be
company-specific and unsafe.

## Decision

Advance the public contract from schema 1.10 to 1.11. `PartInput` gains
`inner_contour_entity_refs`, an ordered, unique and bounded tuple of opaque entity
references. Take-off automatically considers visible contours in the selected outline's
space and layer. A contour on any other layer contributes only when its entity reference
is explicitly supplied, and every supplied reference must form a closed boundary inside
the selected outline.

The result remains fully auditable through the existing per-quantity evidence mapping.
No layer-name heuristic, hidden geometry or annotation loop is silently treated as a cut.

## Consequences

- Existing same-layer drawing workflows remain source-compatible.
- Cross-layer hole/cutout workflows must supply the observed entity references.
- Schema 1.10 bridge/client peers fail closed until upgraded to 1.11.
- Synthetic golden take-off requests explicitly identify their `HOLE`-layer contours;
  they remain regression fixtures and are not promoted to independent production evidence.

## Alternatives considered

- **Treat every nested loop as a void:** rejected because construction geometry can
  materially understate quotation area and mass.
- **Infer intent from layer names:** rejected because layer semantics are company-specific
  and would introduce an undeclared business default.
- **Ignore every cross-layer contour:** rejected because legitimate cross-layer holes would
  be omitted with no explicit recovery path.

## Revisit when

A company-approved profile defines a versioned contour-role taxonomy with sufficient
production evidence to replace explicit cross-layer selection safely.
