# ADR-016 - Calibrated raster intake is an untrusted read path

- **Status:** accepted
- **Date:** 2026-08-09
- **Deciders:** project owner and harness maintainers

## Context

The original roadmap excluded raster drawings because pixels do not carry engineering units,
layers, dimensions, or topology. The project owner has now explicitly required image-to-drawing
conversion. Treating computer-vision output as exact CAD geometry would violate the existing rules
against silent defaults and unmeasured production commits.

## Decision

Add a local-only raster intake path for PNG, JPEG, and TIFF. Every trace is bound to the source
SHA-256 and requires an explicit pixel-to-millimetre calibration supplied by the engineer. The
tracer returns candidates, confidence/evidence, rejected or ambiguous marks, and an overlay; its
output is never a commit authority.

An engineer must accept the calibrated trace before it becomes deterministic vector operations.
Those operations then enter the existing job -> preview -> validation -> approval -> commit ->
post-commit measurement path. Unknown scale, ambiguous topology, or unsupported marks fail closed.
Raster pixels and full traced geometry stay local and are not written to logs or audit payloads.

This decision also introduces contract version 1.9. It adds raster trace contracts, a point geometry
variant needed for centermarks, and an explicit `unknown` observed document unit. It does not permit
`unknown` as a design-spec or operation-plan unit.

## Consequences

- Image conversion is useful for clean orthographic line art and scanned shop sketches, but it is
  not evidence that dimensions, tolerances, material, or intent were recovered.
- A source image without an engineer-provided calibration can be previewed in pixel space but cannot
  produce a production plan.
- Confidence thresholds are configuration, not hard-coded acceptance decisions.
- OpenCV becomes an optional/local processing dependency; no cloud vision service is introduced.
- Golden raster fixtures and measured vector-error tests are required before the feature is called
  complete.

## Alternatives considered

- Keep raster permanently out of scope: rejected because it no longer meets the project owner's
  required product outcome.
- Let a multimodal model emit CAD coordinates directly: rejected because results are probabilistic,
  cannot be independently calibrated, and would bypass the geometry kernel.
- Auto-commit high-confidence traces: rejected because confidence is not engineering approval.

## Revisit when

Revisit if PDF vector import, OCR of tolerances, perspective-corrected photographs, or learned local
models are required. Each needs its own evidence and threat model.
