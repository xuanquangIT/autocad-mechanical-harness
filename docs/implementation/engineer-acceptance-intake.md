# Engineer acceptance intake

This repository is ready to execute the production acceptance gates, but the remaining
evidence must come from the engineering team. Do not replace these inputs with generated
fixtures or expectations produced by the harness itself.

## Golden drawing packet

Provide 30–50 approved, de-identified company drawing cases. Each case needs:

- the source DWG or DXF and its initial-state notes;
- `input_spec.json` describing the requested operation or change;
- the approved `company_profile.yaml` matching the DWT/DWS used for the drawing;
- an engineer-reviewed `expected_plan.json`, semantic entity/measurement expectation,
  validation expectation, and preview reference;
- owner, drawing class, AutoCAD version, units, and a reproducible restore procedure.

At least five cases must be existing-drawing read/takeoff cases. For each, attach a
separately prepared expected takeoff (area, mass, cut length, holes, pierces, weld/BOM as
applicable), its calculation source, reviewer, and tolerance. The expected output must not
be generated with `compute_takeoff` or `run_golden_tests.py --update`.

Place the reviewed material in the controlled acceptance store, then copy only permitted
de-identified fixtures into `tests/golden_drawings/`. Set `company_approved: true` only
after the profile owner approves it. Preserve the source packet outside the repository if it
contains customer IP.

An approval-neutral packet has already been prepared at
`data/engineer-review-packets/production-candidate-v3` (packet digest
`0911452284f6ca12289876cc0e6745b3d22eb7d301a861446d587c26b1cb1df6`). It contains
30 opaque drawing copies,
30 blank review forms and five reserved takeoff slots. Reviewers should fill the required
artifacts from independent engineering work; the packet is not production evidence and must not
be promoted by merely changing its Boolean flags. Validate the completed controlled manifest with:

Only the six local sources are classified as `customer_local_unreviewed`. The 24 licensed-public
DXFs remain `licensed_public_development` and `synthetic: true`; they are useful review exercises,
not substitutes for company drawings selected by engineers.

```powershell
uv run python scripts/check_production_golden_acceptance.py `
  path\to\reviewed-production-manifest.json `
  --trust-policy path\to\production-evidence-trust-policy.json `
  --trust-policy-sha256 sha256:<pinned-canonical-policy-digest>
```

The verifier re-hashes every source and evidence artifact, rejects reused reviews and duplicate
drawings, validates company profile/material/takeoff contracts, and requires separate selector,
golden reviewer, takeoff calculator and takeoff reviewer identities where applicable.
Every human decision is an Ed25519 attestation. The verifier accepts only public keys from the
separate policy whose canonical digest is pinned by the operator; issuer private keys remain in
issuer-side environment variables and must never be copied into the packet, policy or repository.

## Prompt and edit acceptance

Provide representative mechanical prompts, including intentionally incomplete requests.
For each, record the required clarification or approved default; this verifies the harness
never invents units, dimensions, material, thickness, tolerance class, datum, hole count or
PCD. Include existing-drawing remediation examples with an engineer-approved target state.

## Pilot packet

For each pilot run, provide the opaque pilot run ID, engineer baseline timings, authorized
activity markers, final outcome/failure classification, and approved company profile. The
pilot report only passes when the configured sample, quality and savings gates are met; a
successful synthetic or single-user trial is not a substitute.

Run the controlled packet through:

```powershell
uv run python scripts/check_production_pilot_acceptance.py `
  path\to\controlled-pilot-manifest.json `
  --trust-policy path\to\production-evidence-trust-policy.json `
  --trust-policy-sha256 sha256:<pinned-canonical-policy-digest>
```

The verifier checks actual artifact hashes, engineer consent, independent reviewer attestation,
baseline-before-harness ordering, duplicate evidence, per-case savings, and every threshold in
`config/pilot.yaml`. It rejects synthetic, simulated, generated, and development-labelled cases.

## Raster acceptance packet

Provide 5–50 de-identified shop scans spanning clean and noisy inputs. Each source needs an actual
file hash and provenance, engineer-owned calibration, the hash-bound trace result, the engineer's
accepted/rejected candidate record, an independently prepared accuracy review with tolerances,
and hash-bound live AutoCAD readback. A public textbook page or generated noise variant is useful
development data but is not shop-scan acceptance.

Run the packet through:

```powershell
uv run python scripts/check_production_raster_acceptance.py `
  path\to\controlled-raster-manifest.json `
  --trust-policy path\to\production-evidence-trust-policy.json `
  --trust-policy-sha256 sha256:<pinned-canonical-policy-digest>
```

## ChatGPT web deployment packet

The installed local Codex MCP server is configured for the local `dotnet_bridge` acceptance
profile; it fails closed when no approved disposable AutoCAD session is available. A ChatGPT
web app requires a remote HTTPS MCP endpoint or Secure MCP Tunnel,
workspace developer-mode authorization, and an admin/owner to scan and approve tools.
Do not publish a write-enabled CAD endpoint until signed bundle, clean-workstation, engineer
golden and pilot gates have passed.
