# Database migrations

Alembic owns the schema on any machine that holds real job history.

## Commands

```powershell
# Generate a revision from model changes
uv run alembic revision --autogenerate -m "add writer lease table"

# Apply
uv run alembic upgrade head

# Inspect
uv run alembic current
uv run alembic history
```

The database URL comes from `CAD_HARNESS_SQLITE_PATH` when set, so migrations always
target the database the harness uses.

## Rules

- Never edit the schema by hand on a pilot machine. The audit chain in `audit_events`
  is only trustworthy if schema changes are recorded.
- Back up `harness.db` before every `upgrade` on a machine with real history.
- `render_as_batch` is enabled because SQLite cannot `ALTER` most constructs in place.
- Only ship a `downgrade` you have tested against representative data. An untested
  downgrade is worse than none.
- `cad-harness migrate` calls `create_all` and is for development only; it does not
  stamp a revision.
