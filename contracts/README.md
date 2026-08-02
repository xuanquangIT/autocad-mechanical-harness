# Contracts

JSON Schemas validated at both the MCP boundary and the adapter boundary.

## Generated files

Everything except `ipc-envelope.schema.json` is generated from the Pydantic models:

```powershell
uv run python scripts/generate_schemas.py           # regenerate
uv run python scripts/generate_schemas.py --check   # CI gate
```

Edit the model, not the schema. Commit the regenerated files: non-Python clients and
the C# bridge validate against them.

`ipc-envelope.schema.json` is hand-written because it is the contract two languages
share and has no single Python model.

## Versioning

- Semantic versioning. `SCHEMA_VERSION` in `cad_harness.domain.models.base` is the
  current `major.minor`.
- A **minor** bump may only add optional fields.
- A **major** bump is required to change a field's meaning or remove one.
- The server supports the current and previous major during migration; adapters reject
  unknown majors outright.
- Models use `extra="forbid"`. An unrecognised field means the peer is on a different
  contract version, which is a rejection rather than something to ignore.

## Hashing

Canonicalization for the plan hash is defined in `cad_harness.domain.canonical` and is
tied to the schema version: sorted keys, no insignificant whitespace, floats normalized
to 9 decimals, arrays in semantic order. Volatile fields (`plan_hash`, timestamps,
`request_id`) and instance identifiers (`plan_id`, `job_id`) are excluded, so the same
spec compiled twice hashes identically.
