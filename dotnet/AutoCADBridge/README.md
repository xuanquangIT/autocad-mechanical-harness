# CadBridge - C# AutoCAD plug-in (Phase 5)

Not implemented yet. This directory holds the intended structure so the Python side can
be built against a known contract, and so the switch from COM is a configuration change
rather than a rewrite.

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

## Hard rules

- No exception may escape the pipe boundary. Map to an `ErrorCode`.
- Never deserialize a client-supplied .NET type name. The envelope is monomorphic by
  design (`contracts/ipc-envelope.schema.json`).
- Pipe ACL grants only the permitted user or service account; the installer sets it.
- Plug-in and installer are code-signed for production.
- Reject unknown contract major versions rather than best-effort parsing.
- `AutoCADHarness.bundle/PackageContents.xml` is built per target AutoCAD version. Do
  not ship one DLL for all versions when the runtime differs.

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
