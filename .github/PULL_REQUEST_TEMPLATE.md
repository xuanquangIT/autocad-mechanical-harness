## Summary

Describe the problem and the smallest change that solves it.

## Contract and safety impact

- Public contract changed: yes/no
- New rule or default: yes/no; include source, version, and impact
- New side effect: yes/no
- Approval or security impact: none / describe
- Dependency-boundary impact: none / describe
- Real DWG modified: no / disposable drawing only / describe authorized evidence

## Verification

List exact commands and pass/fail/skip counts. Explain every skip.

## Checklist

- [ ] Geometry remains deterministic and outside the model/adapter boundary.
- [ ] Required engineering inputs are not guessed.
- [ ] Preview remains non-mutating and stale revisions fail closed.
- [ ] Tests and documentation cover the change.
- [ ] Public contract changes include schemas, compatibility evidence, and ADR/version review.
- [ ] No customer drawing, private path, credential, approval token, or proprietary standard is included.
- [ ] I have the right to contribute this work under Apache-2.0.
