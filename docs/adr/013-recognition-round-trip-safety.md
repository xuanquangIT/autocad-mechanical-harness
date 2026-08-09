# ADR-013 - Recognition round-trip stays semantic

- **Status:** accepted
- **Date:** 2026-08-08
- **Deciders:** harness maintainers

## Context

Roadmap Requirement 14 asks the recognizer to identify seven geometric meanings and asks every recognized feature to convert back to `FeatureSpec`. The proposed `RecognizedFeature` contract stores scalar measured values only. An arbitrary outline needs ordered topology; a plate needs thickness that 2D geometry cannot reveal; corner fillets and chamfers need a parent outline and vertex identity. A self-contained conversion for those cases would either invent engineering inputs or expose raw-coordinate feature compilers to the AI client.

`MeasuredValue` also needs to represent measured counts such as hole count, while its original unit enum only allowed length, area, and angle.

## Decision

Recognize all seven feature types and preserve their measured evidence. Map rectangular patterns, bolt-circle patterns, and slots to existing public high-level compilers. Map part outlines, isolated circular holes, fillets, and chamfers to internal source-bound compilers. An internal spec carries only source revision and entity references; its compiler additionally requires the trusted `DrawingModel` in `CompileContext` and rejects stale revisions or missing entities. Internal compiler types are not advertised by the feature catalog.

This preserves the complete round-trip requirement without exposing a raw-coordinate compiler to AI clients and without inventing plate thickness or parent topology. User-supplied values may not override measured parameters. Extend `MeasuredValue.unit` additively with `count`; represent point coordinates as separate measured X/Y scalars so every value keeps scalar provenance. Bump the public schema minor version to 1.6.

## Consequences

Every recognized type can be recompiled against the exact source revision. Source-bound specs are deliberately unusable without trusted read-path context, so callers must go through recognition/remediation rather than submit them as ordinary drawing intent. Schema consumers must refresh bindings for version 1.6.

## Alternatives considered

- Register compilers that accept raw vertices, arc centers, and endpoints: rejected because they create a public path for the LLM to become the geometry kernel.
- Infer plate thickness or a parent corner from nearby geometry: rejected because those are technical assumptions without reliable provenance.
- Store tuple-valued measurements: rejected for this schema version because split X/Y scalars keep the existing measured-value shape simple and independently attributable.

## Revisit when

A versioned, capability-gated remediation contract can bind source topology, document revision, and selected parent feature without exposing raw geometry through the public feature catalog.
