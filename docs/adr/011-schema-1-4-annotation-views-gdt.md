# ADR-011 - Schema 1.4: annotation, views, GD&T and preview provenance

- **Status:** accepted
- **Date:** 2026-08-05
- **Deciders:** harness maintainers

## Context

Requirements 9–11 add title-block values, multiple linked views, explicit GD&T declarations, and an approval label on every preview. These values cross process boundaries and therefore cannot remain unversioned dictionaries internal to the compiler.

## Decision

Bump `SCHEMA_VERSION` from 1.3 to 1.4. Add optional, strict `ViewSpec`, `DatumFeatureSymbol`, `FeatureControlFrame`, and `annotations.title_block_values` fields to `DrawingSpec`; change the absence of an explicit dimension request to `dimensions="none"`; add `company_approved` to `PreviewResult`; and publish `preview-result.schema.json`.

Automatic dimensions remain opt-in through `auto_required` or `auto_optional`. This avoids silently changing old specs while ensuring an explicit required request enters deterministic phase-two compilation.

## Consequences

Fresh plans use schema 1.4 and need fresh approval. Unknown view types reach the view compiler and produce actionable `UNSUPPORTED_FEATURE` details. GD&T operations can only originate from explicit contract fields. Preview consumers can reliably label demo-profile artifacts.

## Alternatives considered

- Store views and GD&T in untyped feature parameters: rejected because clients could not discover or validate the contract.
- Infer GD&T from geometry: rejected by Requirement 11.6.
- Keep automatic dimensions as an implicit default: rejected because it is a silent plan-changing behavior.

## Revisit when

Projection requires true 3D geometry rather than linked 2D orthographic placement.
