# Security

Full threat model: section 17 of the architecture document. This page records what is
implemented today and what is still open.

## Trust boundaries

```
AI client (untrusted intent)
  -> MCP server        : schema validation, permission scope
    -> application     : approval, revision, idempotency gates
      -> adapter       : the only code that writes
```

The AI client is treated as an untrusted source of *intent*. It may propose anything; it
cannot commit anything without a human-issued approval token it does not have access to.

## Implemented controls

| Control | Where |
|---|---|
| No primitive drawing tools exposed | `apps/mcp_server/tools/` — 23 high-level tools only |
| HMAC-signed approval tokens scoped to `(job_id, plan_hash, revision)` | `security/approval.py` |
| Short-lived approvals (15 min default) | `config.security.approval_ttl_minutes` |
| Approval revoked on any spec or plan change | `HarnessService.submit_spec` |
| Optimistic concurrency on document revision | `HarnessService.commit`, adapters |
| Idempotency keys with request-digest comparison | `HarnessService.commit` |
| Export path allowlist, no overwrite by default | `security/paths.py` |
| Path traversal resolved before the allowlist check | `security/paths.py::_resolve` |
| Token, secret and prompt redaction | `security/redaction.py` |
| Path pseudonymisation in logs and audit | `security/redaction.py::redact_path` |
| Hash-chained append-only audit | `observability/audit.py` |
| Strict schemas (`extra="forbid"`) at every boundary | `domain/models/base.py` |
| No `eval`, shell, AutoLISP, or business `SendCommand` | enforced by review; COM adapter uses the object API |
| COM confined to one module | Ruff banned-api rule on `win32com` / `pythoncom` |
| Local-only by default | `config.app.local_only` |
| Per-client tool allowlist enforced on every MCP call | `apps/mcp_server/tools/permissions.py`, `security/client_profiles.py` |
| Planning-only profile excludes commit, rollback, and export | `security/client_profiles.py::PLANNING_TOOLS` |
| Cross-process writer lease with heartbeat and atomic terminal release | `persistence/sql_lease_store.py`, `application/services/lease_service.py` |
| Current-user-only Named Pipe endpoint | `CadBridge.Ipc/WindowsNamedPipeFactory.cs` |
| Durable SQLite audit chain verification | `persistence/sql_audit_sink.py::verify_chain` |
| Crash-recoverable bundle install/uninstall journal | `dotnet/AutoCADBridge/Install-BridgeBundle.ps1` |

## Prompt injection

The mitigation is structural rather than filter-based. Even if a drawing, a filename or a
user message convinces the model to attempt a destructive write:

- `cad_commit` needs a `plan_hash` matching an approved plan, and an `approval_token` the
  model never sees. It is issued to the engineer through the approval surface.
- `cad_rollback` and `cad_export` are approval-gated by policy.
- Preview cannot modify the drawing at all, whatever the model asks for.

Content arriving from a drawing, a command output or a fetched URL is data, not
instruction.

## Secrets

`CAD_HARNESS_APPROVAL_SECRET` is per workstation, read from the environment only, and
never written to a config file, a log line or an audit payload. An empty secret is a
hard error rather than a silent fallback: signing with `""` would make forgery trivial.

## Data minimisation

Tool responses carry document metadata, feature summaries, the selection the engineer
permitted, and the measurements needed to judge the result. `cad_selection_inspect` is
capped (`max_entities`, default 200) and reports `truncated` rather than dumping the
entity database into a model's context.

## Open items

| Item | Phase | Note |
|---|---|---|
| Code signing for plug-in and installer | 5 | Required before any production deployment |
| Approval-secret provisioning | 5 | Production install must inject a workstation secret without storing it in client config |
| Manual-gate evidence binding | 5 | Stored confirmation IDs must be replaced by evidence bound to the current PID, document, revision and expiry |
| Checkpoint artifact encryption and quota | 4 | Catalog/journal integrity is authenticated, but full DWG checkpoint bytes still require the deployment encryption/quota policy |
| Single verified active bundle | 5 | Deployment must reject duplicate active bundle versions and unsigned/version-drifted installs |

## Reporting

Security-relevant findings go to the project security reviewer before any code change.
Do not open a public issue containing customer drawing data, file paths or project names.
