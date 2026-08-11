# Security Policy

AutoCAD Mechanical Harness can interact with engineering drawings and a local AutoCAD
process. Security reports are taken seriously, especially reports involving unauthorized
drawing mutation, approval bypass, stale-revision writes, path traversal, named-pipe access,
secret exposure, or customer-data leakage.

## Supported versions

The project is pre-1.0. Security fixes are applied to the latest `main` branch. No older commit
or development bundle is currently supported.

## Report a vulnerability

Use GitHub's private vulnerability reporting flow:

1. Open the repository **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Do not disclose a suspected vulnerability in a public issue, discussion, pull request, test
fixture, drawing, or log.

Include the affected commit, adapter, configuration, reproduction steps, expected safety
invariant, actual behavior, and a minimal synthetic fixture. Remove customer names, drawing
paths, approval tokens, credentials, geometry, and proprietary standards.

The maintainer will acknowledge a complete report, assess severity and scope, and coordinate a
fix and disclosure. Response times depend on maintainer availability; this project currently
has no commercial support SLA.

## Safe testing

- Use the fake adapter whenever possible.
- Never test a write against a user's active production drawing.
- Live COM or bridge tests require a PID-owned disposable drawing and explicit opt-in.
- Do not publish Autodesk binaries, customer DWGs, private DWT/DWS files, or real approval
  secrets.

Security policy violations may be rejected even when functional tests pass.
