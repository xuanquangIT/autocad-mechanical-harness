# ADR-014: Pilot metrics contract and measurement semantics

- **Status:** accepted
- **Date:** 2026-08-08
- **Deciders:** AutoCAD Mechanical Harness maintainers

## Context

The 80% effort-saving claim needs reproducible data without recording prompts or
drawing geometry. Audit silence is not necessarily idle time because an engineer may
be reading a preview between harness events.

## Decision

Schema 1.7 adds `PilotReport`. Pilot storage is local and limited to opaque IDs,
timestamps, counts, durations and ratios. All acceptance values come from
`config/pilot.yaml`.

Raw manual time is checked against that policy before half-up rounding to 0.1 minute.
Harness time begins at `JOB_CREATED`, ends at the last recorded case activity, includes
explicit click-marked engineer activity intervals and manual post-commit fix-up, and
excludes every inactive interval strictly longer than the configured threshold in full.

Medians use the conventional midpoint for even samples. P95 uses linear interpolation
at rank `(n - 1) * 0.95`. Empty metrics are nullable, have sample count zero and are
always insufficient. Incomplete and missing cases remain in every applicable
denominator with saving zero. Every case below the configured floor carries a member of
the finite `FailureReason` enum.

## Consequences

- A structurally valid baseline still cannot claim success until every decision metric
  meets the configured minimum sample count.
- Pilot reports can be reproduced from audit events plus local engineer activity.
- Consumers must accept the new 1.7 minor contract version.

## Alternatives considered

Treating every long audit gap as idle was rejected because it erases real engineer
review time. Storing prompts or geometry for later reconstruction was rejected because
the metrics privacy requirement explicitly forbids it.

## Revisit when

The pilot organisation adopts a different percentile convention or supplies a signed
external measurement protocol.
