# CadBridge - C# AutoCAD plug-in (Phase 5)

The offline-testable contracts, IPC, execution, inspection, metadata, hosting router and
Autodesk-dependent plug-in foundation live here. Operation dispatch and live AutoCAD
acceptance remain separate gates; an offline build is not evidence of AutoCAD runtime
correctness.

## Why it exists

COM gives us enough to prove the MVP, but not enough to trust production writes:

| Need | COM | C# bridge |
|---|---|---|
| Atomic transaction across a whole job | no | yes |
| `DocumentLock` before writing | no | yes |
| One undo group per commit | approximate | yes |
| Stable feature metadata (XData / extension dictionary) | uneven | yes |
| Reliable database-level revision fingerprint | coarse | yes |
| In-viewport preview, PaletteSet, reactors | no | yes |

Until this ships, the COM adapter compensates with file checkpoints, post-commit
measurement and an operation journal, and it declares those capability gaps honestly
through `AdapterStatus`.

## Projects

| Project | Responsibility |
|---|---|
| `CadBridge.Contracts` | DTOs mirroring `contracts/*.schema.json`. No behaviour. |
| `CadBridge.Ipc` | Named-pipe server, ACL, length-prefixed framing, size limits. |
| `CadBridge.Execution` | Marshals into AutoCAD command context; `DocumentLock`, transaction, undo group. |
| `CadBridge.Inspection` | Document and selection reads, revision fingerprint. |
| `CadBridge.Metadata` | Feature ids in XData / extension dictionary under a registered app name. |
| `CadBridge.Palette` | PaletteSet approval UI. Optional; after the core bridge works. |
| `CadBridge.Tests` | Unit tests plus fault-injection against a mocked document. |

## Execution contract

```
receive request
-> authenticate the local client
-> validate contract version and request size
-> enqueue into AutoCAD command context
-> verify document identity and revision
-> acquire DocumentLock
-> begin undo group
-> begin transaction
-> execute all operations
-> measure and check adapter invariants
-> commit transaction
-> end undo group
-> compute the new revision
-> return the result
```

Failure before the transaction commits: abort, leaving nothing behind. Failure after the
commit but before the response: mark the outcome for reconciliation using the job and
idempotency records. Never guess.

The write-enabled plug-in stores restart-safe idempotency state below
`%LOCALAPPDATA%\AutoCADMechanicalHarness\bridge-commit-journal-v1` by default. An operator may
set `CAD_HARNESS_COMMIT_JOURNAL_ROOT` to another absolute local directory; network/device paths
are rejected. The journal durably records `prepared` before invoking AutoCAD, atomically replaces
it with a flushed `committed` receipt before returning success, and recovers an interrupted
`prepared` entry as `unknown` only after its PID plus process-start epoch proves the reserving
process is dead. Live competing processes cannot poison the owner's safe-abandon path. Entry names
hash the job/key, raw job ids are redacted from stored receipts, and plans, approval tokens, drawing
paths, exceptions, and stacks are never written. Journal envelopes are HMAC authenticated with an
independent random key protected by Windows DPAPI for the current user; the local root receives a
non-inherited current-user-only ACL and reparse points are rejected. This protects against accidental
corruption and other-user modification, not malicious code already executing as the same Windows
user, which can access that user's DPAPI material and is inside the documented trust boundary.

## Hard rules

- No exception may escape the pipe boundary. Map to an `ErrorCode`.
- Never deserialize a client-supplied .NET type name. The envelope is monomorphic by
  design (`contracts/ipc-envelope.schema.json`).
- Pipe ACL grants only the permitted user or service account; the installer sets it.
- Plug-in and installer are code-signed for production.
- Reject unknown contract major versions rather than best-effort parsing.
- `AutoCADHarness.bundle/PackageContents.xml` is built per target AutoCAD version. Do
  not ship one DLL for all versions when the runtime differs.

## Bundle packaging

`Package-BridgeBundle.ps1` is the only supported bundle assembly path. It restores with
this subtree's `NuGet.Config`, builds with explicit target framework/API/Series inputs,
checks `PackageContents.xml`, excludes Autodesk runtime DLLs and writes an ordinal,
deterministic `SHA256SUMS.ps1` manifest.

An unsigned package is development-only and must be requested explicitly:

```powershell
.\Package-BridgeBundle.ps1 `
  -TargetFramework net8.0-windows `
  -AutoCADManagedApiVersion 25.0.1 `
  -AutoCADSeries R25.0 `
  -PackageVersion 0.2.0.0 `
  -ProductCode A4B53210-2848-4FA6-8720-5A55A628899C `
  -DevelopmentUnsigned
```

It is emitted below `CadBridge.Plugin\bin\BridgePackages\DEVELOPMENT-UNSIGNED-*` and
contains an internal `DEVELOPMENT-UNSIGNED.txt` warning. Release mode is the default and
fails closed unless both `-OrganizationSupportEmail` uses a routable non-`.local` domain
and `-SigningCertificateThumbprint` resolves to a current code-signing certificate with
a private key. Release mode Authenticode-signs every staged DLL and the checksum manifest,
then verifies each signature before returning the bundle path.

## Before starting

Confirm against Autodesk's documentation for the *actual* target version, not this file:

- [AutoCAD Developer Documentation](https://help.autodesk.com/view/OARX/2026/ENU/)
- [Managed .NET Transaction Manager](https://help.autodesk.com/cloudhelp/2026/HUN/OARX-DevGuide-Managed/files/GUID-12ADA0F2-C44D-4D88-B248-1803D39DF3AA.htm)
- [AutoCAD .NET compatibility](https://help.autodesk.com/cloudhelp/2026/KOR/AutoCAD-Customization/files/GUID-A6C680F2-DE2E-418A-A182-E4884073338A.htm)
- [Plug-in package reference](https://help.autodesk.com/cloudhelp/2026/DEU/AutoCAD-MAC-Customization/files/GUID-BC76355D-682B-46ED-B9B7-66C95EEF2BD0.htm)

## Definition of done

- The adapter contract suite in `tests/contract/` passes against `DotNetBridgeAdapter`.
- The Python core's public contract is unchanged.
- A failure between operations leaves no partial geometry (fault-injection suite).
- One undo group per commit, verified interactively.
- Feature ids survive save, close and reopen.
