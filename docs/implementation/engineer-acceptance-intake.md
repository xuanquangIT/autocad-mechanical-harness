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

## ChatGPT web deployment packet

The installed local Codex MCP server is suitable for development and runs with the fake
adapter. A ChatGPT web app requires a remote HTTPS MCP endpoint or Secure MCP Tunnel,
workspace developer-mode authorization, and an admin/owner to scan and approve tools.
Do not publish a write-enabled CAD endpoint until signed bundle, clean-workstation, engineer
golden and pilot gates have passed.
