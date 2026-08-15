# ADR-021 - Trust-anchored production evidence attestations

- **Status:** accepted
- **Date:** 2026-08-15
- **Deciders:** project maintainers and production evidence owners

## Context

Hash-locking a drawing, review form, material table, pilot record, or raster result proves only
that the bytes did not change. A manifest containing `engineer_selected: true` or an opaque
reviewer name can still be written by the same process that wants the production gate to pass.
That is not evidence of independent engineering review or company approval.

The repository also contains development and synthetic corpora. Those assets are valuable for
regression testing but must never become production evidence merely because a JSON flag changed.

## Decision

Production golden, pilot, and raster verifiers require a separate strict trust-policy document
and role-specific Ed25519 attestations over exact canonical claim sets. Every trusted identity has
one opaque ID, an explicit closed set of roles, and a unique public key. Deployment configuration
must independently pin the canonical SHA-256 digest of the whole policy. The policy and
attestations reject unknown fields, invalid roles, duplicate identities or public keys, an absent
or substituted policy pin, claim drift, signature drift, future issuance, and expired validity
windows.

Selection and review, calculation and review, participant and pilot review, and raster engineering
and accuracy review must be performed by distinct trusted identities wherever independence is a
requirement. The verifier recomputes deterministic CAD, validation, takeoff, pilot-metric, and
raster-accuracy results before it evaluates the attestations. A string, boolean, hash, or unsigned
review form is never sufficient by itself.

The local issuer receives the private-key environment-variable name separately from the public
policy, verifies the same external policy pin, writes an append-only attestation file, and never
prints identities, paths, claims, or keys. Production verifiers receive public keys only and cannot
mint a valid attestation.

Live raster execution uses a second, independently pinned Ed25519 key and signature domain. Its
receipt is generated from the completed adapter result and binds the exact CAD PID, document,
pre/post revisions, job, plan, validation digest and readback-artifact digest. Human review keys
cannot serve as execution keys, and the execution signer cannot replace the required engineer and
accuracy reviewers.

## Consequences

- Development packets remain deliberately unable to pass a production gate.
- Evidence owners must provision independent identity private keys and pin the public trust policy
  outside the reviewed corpus.
- Changing any signed claim or bound artifact requires a new attestation.
- Ed25519 separates signing authority from verification. It does not prove competence, employment,
  or organizational authorization by itself; those identities and policy pins still require
  controlled onboarding. Long-lived public evidence may additionally require an external timestamp
  and revocation system.
- Human participation, authorization, competence, and organizational approval remain real-world
  facts; software can verify their signed evidence but cannot manufacture them.

## Alternatives considered

- **Trust manifest booleans and reviewer IDs.** Rejected because the claimant can write them.
- **Hash review files only.** Rejected because a hash proves integrity, not authorship or role.
- **Embed keys in the corpus.** Rejected because anyone with the corpus could forge evidence.
- **Mark development fixtures as reviewed.** Rejected because it would erase the production
  boundary the verifier exists to enforce.

## Revisit when

Add certificate-backed identity, revocation, and a durable external timestamp when evidence must be
verified outside the organization or when formal legal non-repudiation is required.
