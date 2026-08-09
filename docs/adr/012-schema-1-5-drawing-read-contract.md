# ADR-012 - Schema 1.5: drawing read contract

- **Status:** accepted
- **Date:** 2026-08-06
- **Deciders:** harness maintainers

## Context

Requirements 13 and 14 introduce a read-only path shared by DXF, COM, and bridge sources. Feature recognition, take-off, audit, and measurement need one immutable semantic representation whose entity order, source units, provenance, unsupported coverage, block nesting, and revision survive process boundaries.

## Decision

Bump `SCHEMA_VERSION` from 1.4 to 1.5. Publish strict frozen `DrawingModel`, `DrawingSummary`, geometry records, `ReadScope`, `DrawingSourceRef`, and `DrawingReadRequest` contracts. Publish `drawing-model.schema.json` and `drawing-summary.schema.json`.

`to_mm_factor` remains required and nullable: `None` means the document did not declare units and forces `geometry_normalized=false`. Entity collections are tuples in source read order. The source port exposes only `read`, `summarize`, and `current_revision`; format, size, and revision policy stay in `DrawingReadService`.

## Consequences

Consumers can round-trip extracted drawings without losing semantic order or unsupported-entity evidence. A source with unknown units cannot silently claim millimetre geometry. Read adapters cannot mutate through the domain port. Existing generated contracts gain the 1.5 default and therefore clients should refresh generated bindings.

## Alternatives considered

- Reuse untyped dictionaries from adapter inspection: rejected because geometry variants and ordering would not be discoverable or round-trip safe.
- Default unknown units to millimetres: rejected as an unsafe silent default.
- Put read limits in each adapter: rejected because policy would diverge across DXF, COM, and bridge implementations.

## Revisit when

A versioned 3D drawing model or an ODA-backed DWG file reader is introduced.
