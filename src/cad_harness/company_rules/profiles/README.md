# Company standard profiles

One YAML file per profile. The filename stem is the `profile_id`.

## Rules

- `demo-profile.yaml` is committed and is **not** company approved. It exists so the
  test suite and demos can run without company data.
- Real company profiles (`company-*.yaml`) are gitignored. They carry drawing
  standards that are usually confidential.
- Setting `company_approved: true` is an engineering decision, not a developer one.
  It asserts that layers, styles, title block and tolerance class were reviewed
  against the controlled DWT/DWS set.
- Bump `version` on every change. Plans record the profile ref, so a version bump
  changes the plan hash and correctly invalidates prior approvals.

## Adding a profile

1. Copy `demo-profile.yaml`.
2. Fill layers and `layer_map` from the controlled template.
3. List only the defaults the standard genuinely mandates under `allowed_defaults`.
   Anything left out will be requested from the engineer instead of guessed.
4. Add a golden drawing case that exercises the profile.
