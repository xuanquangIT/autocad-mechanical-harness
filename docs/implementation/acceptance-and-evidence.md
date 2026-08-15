# Acceptance and evidence matrix

## Audited baseline — 2026-08-09

The repository currently records 230 completed and 3 pending roadmap checkboxes. The
latest complete Python gate is 933 passed with 12 explicit skips: unsafe active-document
COM writes, opt-in disposable AutoCAD acceptance, and opt-in live performance.
Ruff formatting and lint pass, 27 generated schemas are current, and strict type checking
passes across 189 source files. The pure C# bridge suite passes 144/144, the AutoCAD 2027
R26/.NET 10 plugin builds with zero warnings, and the semantic golden runner passes 247 tests.

This baseline proves the implemented offline Python/C# surfaces are internally
consistent. Disposable AutoCAD Mechanical 2027 evidence now proves bridge load, semantic
read, one-lock/transaction atomic commit, metadata readback, exact session undo and the
intervening-activity rejection fence. It does not prove a signed deployment, durable DWG
checkpoint replacement, company drawing coverage, detailed COM geometry or pilot effectiveness.

## Audited refresh — 2026-08-15

The current frozen-tree offline gate is **1,355 passed with 13 explicit skips**: 12 live-only
gates and one unavailable Windows symlink fixture. Ruff lint passes, all 407 checked files are
formatted, strict mypy passes across 219 source files, 27 generated schemas are current,
import boundaries and static invariants
pass, and the semantic golden runner passes 247/247. The pure C# bridge suite passes
201/201, and the AutoCAD 2027 R26/.NET 10 plug-in builds with zero warnings and zero errors
against AutoCAD.NET 26.0.0.

Development intake now contains exactly 30 drawing candidates: six user samples and 24 pinned,
licensed-public DXFs. The public fetch lock verifies all 26 downloaded sources (24 DXFs, one PNG
and one public-domain PDF). A private review packet copies the 30 drawings under opaque hashes,
reserves five takeoff reviews and creates 30 blank human forms; all selection, review and approval
flags remain false.
Five generated plate takeoffs match an independent analytic oracle in 5/5 cases and repeat
deterministically in 5/5 cases. These are development results, not substitutes for engineer
selection, independent review, a company-approved material/price master, or a pilot.

The R26 development bundle v0.1.4.0 was packaged and installed into the workspace-local
acceptance root with a verified receipt and complete hashes. It is deliberately unsigned.
AutoCAD secure loading does not treat that workspace location as trusted, so a fresh live
bridge reload remains blocked until an approved signing identity or approved trusted
deployment is available. A diagnostic SCM `DispatchEx` fallback was rejected after it proved
unable to guarantee Job Object ownership of the COM-created process. The isolated acceptance
runner remains fail-closed and never attaches to an existing user session. Separately, the COM
adapter can attach only when the engineer deliberately starts AutoCAD, activates the target
drawing and enables the live setup gates. No acceptance path weakens
[`SECURELOAD`](https://help.autodesk.com/view/ACD/2027/ENU/?caas=caas%2Fdocumentation%2FACD%2F2014%2FENU%2Ffiles%2FGUID-541566C6-2738-49DD-87C3-C1490E924A02-htm.html)
or modifies `TRUSTEDPATHS`.

## Capability status

| Capability | Current evidence | Completion evidence still required |
|---|---|---|
| Safe deterministic write core | Contracts, state machine, SQLite stores, writer lease, approval binding, validation, preview, fault matrix, fake end-to-end evidence and live R26 atomic commit/readback/session undo; authenticated durable checkpoint catalog/restore coordinator passes offline restart, tamper, idempotency and fault tests | Signed deployment acceptance and live crash-point proof of durable checkpoint replacement |
| Complex 2D creation | Ten deterministic feature families, modifiers/annotations/views, 34 complete synthetic semantic cases, configured performance gates, and live MCP commits for a base plate, flange and L-bracket on AutoCAD 2027 | 30–50 engineer-selected company drawings, real profiles and representative prompt/pilot evidence |
| Drawing read | `DrawingModel`, bounded DXF/bridge readers, a bounded revision-pinned COM semantic subset for line/circle/arc/straight polyline, live R26 bridge parity, recognition with ambiguity/provenance/round-trip and company-standard reconciliation | Engineer-selected production drawings and full semantic parity for unsupported COM entities |
| Takeoff and quotation data | Pure takeoff engine, versioned demo material table, mass/cut/pierce/hole/weld quantities, provenance, SQLite audit, five synthetic DXF cases, and one independently calculated live R26 plate result matching area/mass/cut length exactly | Independently calculated answers on engineer-selected drawings and an approved company material table |
| Audit and modification | Deterministic auditor plus persisted-evidence remediation compiler; exact selected-finding plans, semantic deletion refs, preview/approval/commit gates, stale blocking, durable checkpoint metadata, separate rollback approval, full post-commit re-audit, live selected-finding update/delete remediation and live session undo; durable whole-DWG restore code is implemented and default-off | Live restart/crash restore matrix and engineer acceptance |
| Measurement | Twelve analytic measurements, cooperative terminal timeouts, provenance/revision/tolerance, properties/schemas, and live R26 MCP perimeter readback matching the independent rectangle reference | Engineer-selected production drawing comparisons |
| Image to drawing | ADR-016, bounded local PNG/JPEG/TIFF intake, calibrated deterministic line/circle/arc/contour trace, hash-bound local overlay review, signed candidate/layer acceptance, bearer-token redaction, sealed draft spec, property/golden/DXF round-trip, and a real existing-document MCP line add/read/audit/delete round trip | Representative shop scans and independent engineer accuracy review |
| MCP/client experience | 22 typed tools, fail-closed permissions, PySide6 engineer approval, global local-MCP install configured for `dotnet_bridge`, direct MCP stdio acceptance through inspect/compile/preview/validate/approval/commit/read/recognize/audit/measure/takeoff, and an actual registered Codex CLI read-only status/inspect smoke on AutoCAD 2027 | ChatGPT web requires a remote MCP/tunnel; the Claude Code/Kiro/Zed live-client matrix, production signing and company-drawing acceptance remain open |
| Production AutoCAD bridge | Python transport plus C# contracts, secured per-user pipe, typed router, atomic executor, durable restart-safe commit journal, authenticated checkpoint catalog/restore coordinator, stable metadata, bounded inspection, R26 plug-in/development bundle, 201/201 pure tests and prior live R26 scratch acceptance | Signed release, clean-workstation install, live durable checkpoint restore and production drawing acceptance |
| Pilot effectiveness | Run-scoped append-only baseline/effort/operation evidence, click-marked engineer activity, explicit failed-case classification, quality and savings gates, generated report schema and properties through task 27 | Representative engineer baseline and live pilot results meeting configured thresholds |
| Operational readiness | Windows CI definition, import/static gates, compatibility matrix, retention, timeout/process isolation, performance suite, hardened bundle packager/installer with crash recovery and manual gates | Real CI run, organisation signing identity/certificate/timestamp, signed install/rollback on a clean AutoCAD workstation |

The synthetic regression corpus is intentionally not production evidence. Run
`uv run python scripts/check_production_golden_acceptance.py <manifest>` for the separate
fail-closed production gate; the current repository correctly fails it until approved external
drawings, provenance, reviewers, company profiles/materials and independent takeoff answers exist.

## Development corpus — 2026-08-15

The local development intake now contains exactly 30 drawing candidates without making a
production claim: six user-supplied local DWG/DXF files and 24 licensed-public DXFs.
The public inputs are pinned by source URL and SHA-256, preserve their licence/attribution, and
are fetched only into the ignored `data/development-corpus/` tree. The intake manifest records
opaque case identifiers and hashes; it does not copy customer names or paths into reports.

`scripts/build_engineer_review_packet.py` re-hashes both input stores and publishes an immutable
draft beneath `data/engineer-review-packets/`. The current `production-candidate-v3` packet binds
30 unique source hashes, contains 30 pending review forms, and deterministically reserves five DXF
takeoff slots. Its packet digest is
`0911452284f6ca12289876cc0e6745b3d22eb7d301a861446d587c26b1cb1df6`; it deliberately contains
no expected results and remains `production_evidence: false` until humans supply and review them.
The six local drawings remain `customer_local_unreviewed`; all 24 licensed-public examples remain
`licensed_public_development` with `synthetic: true`, so they cannot be promoted as production
drawings merely by copying the packet.

Five additional disposable AL6061 plate DXFs exercise the production drawing reader and takeoff
engine against independent analytic formulae for net area, cut length, pierces, hole groups, and
mass. Their 2700 kg/m3 density reference is NASA public-use data. These cases are deliberately
labelled development evidence: the material table is not a company purchasing master and the
answers are not an independent engineer review.

The raster development evaluator runs the production tracer on a pinned QCAD mechanical image
and deterministic noise/blur variants. It proves deterministic bounded execution and exposes
candidate/ambiguity/rejection counts; it does not claim shop-scan reconstruction accuracy. The
separate production raster verifier requires hash-bound shop scans, engineering calibration,
candidate acceptance, independent accuracy review, and live AutoCAD readback. The production
pilot verifier likewise rejects generated/development evidence and recomputes configured pilot
metrics from hash-bound, consented engineer records.

Reproducible development and production-gate commands:

```powershell
uv run python scripts/fetch_development_corpus.py --output-root data/development-corpus/public-v3 --check
uv run python scripts/build_engineer_review_packet.py --local-manifest data/development-corpus/local/local-sample-manifest.json --local-source-root sample-design --public-lock data/development-corpus/public-v3/development-corpus.lock.json --public-source-root data/development-corpus/public-v3 --output-root production-candidate-v3
uv run python scripts/evaluate_development_takeoff_corpus.py --output takeoff-evaluation.local.json --output-root data/development-corpus/public
uv run python scripts/check_production_golden_acceptance.py path\to\reviewed-production-manifest.json --trust-policy path\to\policy.json --trust-policy-sha256 sha256:<pinned-digest>
uv run python scripts/check_production_raster_acceptance.py path\to\controlled-raster-manifest.json --trust-policy path\to\policy.json --trust-policy-sha256 sha256:<pinned-digest>
uv run python scripts/check_production_pilot_acceptance.py path\to\controlled-pilot-manifest.json --trust-policy path\to\policy.json --trust-policy-sha256 sha256:<pinned-digest>
```

The last three commands must fail closed until real controlled human evidence and independently
pinned Ed25519 signer identities are supplied.

## Claims and minimum proof

| Claim | Minimum acceptable proof |
|---|---|
| “Can design a feature” | Deterministic spec-to-plan compile, preview, validation, adapter capability, normal/boundary/invalid goldens |
| “Can understand vague engineering prompts” | Structured clarification/assumption evaluation on representative engineer prompts with zero silent technical defaults |
| “Can read and analyze drawings” | DWG/DXF extraction coverage, unsupported-entity reporting, recognition ambiguity, provenance, read-only revision invariance |
| “Can produce takeoff for quotation” | Independently checked area/mass/cut/pierce/hole/BOM values and source entity references |
| “Can modify an existing drawing” | Selected finding to remediation plan to preview/approval/commit/re-audit, with stale-revision and idempotency tests |
| “Can convert an image” | Calibrated image goldens, overlay/diff, confidence and ambiguity handling, measured vector error, human acceptance |
| “Works in AutoCAD” | Tests executed in a declared supported AutoCAD version on disposable DWGs, including readback and cleanup |
| “Production ready” | All required gates pass, no required skips, package install/rollback succeeds, compatibility/pilot evidence meets configured thresholds |

Use `.codex/skills/implement-autocad-harness/scripts/project_audit.py` to refresh structural progress. Update this matrix only from current command output and acceptance evidence.

The sanitized live record is
[`evidence/live-r26-rollback-2026-08-09.json`](evidence/live-r26-rollback-2026-08-09.json).
It intentionally distinguishes the proven session `undo_group` path from an unimplemented
whole-document checkpoint replacement.

The isolated COM reader record is
[`evidence/live-com-reader-r26-2026-08-09.json`](evidence/live-com-reader-r26-2026-08-09.json).
Its process ownership fence prevented the live test from attaching to or closing the user's
existing AutoCAD process.

Direct MCP-to-AutoCAD evidence for three representative creation workflows is recorded in
[`evidence/live-mcp-r26-base-plate.json`](evidence/live-mcp-r26-base-plate.json),
[`evidence/live-mcp-r26-flange.json`](evidence/live-mcp-r26-flange.json), and
[`evidence/live-mcp-r26-l-bracket.json`](evidence/live-mcp-r26-l-bracket.json). Each record
binds the owned AutoCAD PID, document revision, plan hash, checkpoint/undo evidence, semantic
readback and persisted DWG digest; the pre-existing user AutoCAD process remained untouched.

An additional explicitly user-authorized existing-document COM run is recorded in
[`evidence/live-existing-mcp-baseplate-v018.json`](evidence/live-existing-mcp-baseplate-v018.json).
MCP attached to the already-open AutoCAD Mechanical 2027 `Drawing1.dwg`, inspected revision and
standards resources, previewed and validated the plan, consumed a fresh human-bound approval,
and committed one closed 160 x 100 mm plate outline plus four diameter-14 mm holes. Direct COM
readback proved five entities on `OBJECT`, exact hole centres and radii, and a changed revision.
The run also exposed and fixed a local COM iterator defect and added blocking pre-commit checks
for missing live layers and mismatched document units. This is development evidence, not a
production drawing or company-standard approval. The v018 runner invoked the COM export surface,
which saved the active document under its evidence filename; later existing-document runs omit
export so they do not rename the engineer's active drawing.

The follow-up read-only bridge record is
[`evidence/live-existing-dotnet-read-v019.json`](evidence/live-existing-dotnet-read-v019.json).
After the engineer loaded the development-unsigned bundle once into that same AutoCAD process,
MCP connected through the real named pipe, read the five normalized entities, recognized six
features, audited the drawing, measured the exact 520 mm outer perimeter, and produced an SS400
takeoff with four holes, five pierces, 15,384.247840 mm² net area, 695.929189 mm cut length and
1.208 kg rounded unit mass. The document ID and display name are hashed in the record, and exact
pre/post revision and entity-count equality prove the workflow did not modify the open DWG. The
demo material table is not company-approved, and `Load Once` of an unsigned development bundle
is not production installation or code-signing evidence.

The same engineer-authorized open document was subsequently exercised with a more complex keyed
flange and bracket. The final non-mutating bridge record is
[`evidence/live-existing-complex-bridge-v020.json`](evidence/live-existing-complex-bridge-v020.json):
20 entities, complete supported coverage, 24,595.165918 mm2 net area, 1,300.548286 mm cut length,
eight holes, ten pierces, and 3.861 kg unit mass. Its six remaining audit findings are demo-profile
layer/property findings, not geometry failures or a company-standard approval.

The first audit-selected deletion did change the drawing but returned
`UNKNOWN_COMMIT_STATE` because ActiveX invalidated the deleted entity proxy before the adapter
could construct its operation receipt. That attempt was never retried; fresh COM and bridge reads
reconciled the effect. The sanitized incident record is
[`evidence/live-existing-overlap-remediation-v021.json`](evidence/live-existing-overlap-remediation-v021.json).
After capturing receipt fields before `Delete`, a second controlled live round trip added one
temporary duplicate outline and removed exactly that audit-selected entity. MCP returned
`committed`, the entity count returned from 21 to 20, the pre-add COM revision was restored, and
post-commit re-audit proved the selected finding absent. Evidence is split into the
[`temporary add`](evidence/live-existing-delete-receipt-v022-add.json) and
[`successful remediation`](evidence/live-existing-delete-receipt-v022.json) records. No run opened,
created, renamed, saved, or closed an AutoCAD document; the engineer's existing process remained
the target throughout.

Image-to-drawing was then exercised against that same already-open document without creating or
opening another DWG. A deterministic PNG line was calibrated to an existing slot flank, traced to
one proposed candidate, accepted through the local human-only signing boundary, compiled through
MCP, previewed, validated, approved and committed. Real COM readback observed entity count 20 to
21; drawing audit bound the new handle to one `DUPLICATE_ENTITY`, and a separately approved
remediation deleted it. Entity count returned to 20 and the COM revision returned exactly to the
pre-test digest. The SQLite evidence store was byte-scanned and contains neither raw/base64 image
bytes nor the `raster-v1` acceptance token. The latest sanitized workflow record is
[`evidence/live-existing-raster-roundtrip-v029.json`](evidence/live-existing-raster-roundtrip-v029.json),
and the final independent non-mutating bridge read is
[`evidence/live-existing-raster-roundtrip-v025-bridge.json`](evidence/live-existing-raster-roundtrip-v025-bridge.json).
This rerun attached only to the engineer-opened document, observed the temporary entity count
change from 20 to 21 and back to 20, restored the exact pre-test COM revision, and independently
confirmed the unchanged 20-entity bridge revision afterward. It did not open, create, save, rename,
or close an AutoCAD document. The live runner also emitted a separately keyed Ed25519 execution
receipt over the actual COM PID, document, pre/post revisions, job, plan, validation digest and
readback-artifact digest; an independent verifier recomputed the readback SHA-256 and verified the
receipt against the recorded public-key pin. The key is explicitly a development-run issuer, so
this proves the producer/verification path without claiming organizational production trust.
This proves the line path and cleanup on AutoCAD 2027; it is not representative shop-scan accuracy
or independent engineer acceptance evidence.

A repeatable complex existing-document workflow now applies an exact geometry/layer multiset
preflight and refuses before preview or commit when any planned geometry is absent. In the real v035
COM run, the already-open drawing contained the keyed flange with eight holes, keyed bore, slot and
L-bracket before the test. The plan temporarily created 15 audit-identical entities, changing the
count from 20 to 35. Every new reference was then proven as `DUPLICATE_ENTITY` and deleted through a
separately previewed, validated and human-approved remediation job. The count returned to 20 and the
exact pre-test COM revision was restored. The sanitized workflow is
[`evidence/live-existing-complex-roundtrip-v035.json`](evidence/live-existing-complex-roundtrip-v035.json),
and an independent non-mutating bridge session subsequently confirmed the unchanged 20 entities,
628.318531 mm perimeter and 24,595.165918 mm2 net area in
[`evidence/live-existing-complex-roundtrip-v035-bridge.json`](evidence/live-existing-complex-roundtrip-v035-bridge.json).
The run did not open, create, save, rename or close a document, and its evidence contains no raw
document identifiers or approval credentials. This is development evidence, not the required
engineer-selected production corpus. Because COM has no durable checkpoint restore, a process crash
between commit and remediation remains a residual risk; production mutation tests still require a
disposable drawing or the separately verified durable bridge restore path.

The registered local MCP configuration was then exercised by an actual ephemeral Codex CLI 0.147.0
client, rather than by the in-process compatibility harness. With a read-only sandbox and an exact
two-tool instruction, Codex discovered the registered server, called `cad_status` and
`cad_document_inspect`, and reached the real `dotnet_bridge` hosted by AutoCAD 2027 PID 9260. It
reported 20 entities at the same bridge revision as the independent v035 read, with no write tool
call. The redacted record is
[`evidence/codex-registered-client-read-v036.json`](evidence/codex-registered-client-read-v036.json).
This closes the Codex discovery/read smoke only; it does not substitute for the full external-client
matrix in Requirement 25.10 or for ChatGPT Secure MCP Tunnel acceptance.

The schema 1.11 live read/audit/measurement/takeoff record is
[`evidence/live-mcp-r26-read-audit-measure-takeoff-1-11.json`](evidence/live-mcp-r26-read-audit-measure-takeoff-1-11.json).
For the 184 × 115 × 13.8 mm SS400 plate with four Ø16.1 holes, its independent analytic
reference exactly matches the observed 20,345.667768 mm² net area, 598 mm outer perimeter,
800.318567 mm total cut length and 2.204 kg rounded unit mass. The demo material table remains
explicitly unapproved and this single synthetic live case does not replace engineer-selected
production takeoff evidence.

The schema 1.12 direct-MCP remediation record is
[`evidence/live-mcp-r26-remediation-1-12.json`](evidence/live-mcp-r26-remediation-1-12.json).
On a PID-owned disposable AutoCAD 2027 drawing, the harness committed the 25-entity plate,
committed the same approved plan again to create a deterministic 50-entity duplicate case, then
submitted two persisted audit findings through `cad_change_submit`. The remediation preview and
live commit contained exactly one deletion and one layer update, readback contained 49 entities,
and re-audit contained neither selected `(rule_id, entity_ref)` pair. The record stores only hashes
of selected entity references, no approval token, and confirms that no AutoCAD process remained.

The exact external artifacts needed to close the remaining roadmap gates are listed in
[`engineer-acceptance-intake.md`](engineer-acceptance-intake.md).
