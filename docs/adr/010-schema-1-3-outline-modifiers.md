# ADR-010 - Schema 1.3: versioned outline modifiers

- **Status:** accepted
- **Date:** 2026-08-05
- **Deciders:** harness maintainers

## Context

Requirement 8 adds ordered modifiers to a feature. Modifier intent crosses the client boundary, must reject caller-supplied intermediate coordinates, and must remain deterministic in the plan hash. This is an additive public `DrawingSpec` change.

## Decision

Bump `SCHEMA_VERSION` from 1.2 to 1.3, add strict `ModifierSpec` and ordered `FeatureSpec.modifiers`, and publish `modifier-spec.schema.json`. Modifier feature identifiers derive from the parent, modifier type, and semantic tuple index.

## Consequences

Fresh plans use schema 1.3 and therefore require fresh approval. Unknown modifier fields and coordinate-shaped parameter keys are rejected before compilation. Tuple order is semantic and changing it intentionally changes modifier IDs and the plan hash.

## Alternatives considered

- Encode modifiers as child features: rejected because modifiers replace parent boundary geometry rather than add independent entities.
- Accept generated points from clients: rejected because it bypasses the geometry kernel.
- Keep modifiers internal: rejected because clients need a versioned, discoverable input contract.

## Revisit when

Modifiers need references to non-outline topology such as faces or 3D edges.