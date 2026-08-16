"""Fail-closed Alembic upgrades for harness-owned SQLite databases.

Release 0.2.2 development databases were created without an Alembic version row.
They may be adopted only when their complete SQLite structure matches an Alembic-built
``e6b3f91a2c40`` reference database. Unknown schemas are never stamped by inference.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _REPOSITORY_ROOT / "alembic.ini"
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_DATABASE_ENV = "CAD_HARNESS_SQLITE_PATH"
_UNVERSIONED_BASELINE = "e6b3f91a2c40"
_ALEMBIC_LOCK = threading.RLock()
_LEGACY_BACKFILL_COLUMNS = {
    ("effort_records", "pilot_run_id"),
    ("operation_metrics", "pilot_run_id"),
}
_LEGACY_BACKFILL_DEFAULT = re.compile(r"\s+DEFAULT\s+'legacy'(?=\s|$)", re.IGNORECASE)


class DatabaseSchemaError(RuntimeError):
    """The SQLite schema cannot be proven safe for the requested migration."""


@dataclass(frozen=True, slots=True)
class _DatabaseInspection:
    revision: str | None
    has_version_table: bool
    structure: tuple[Any, ...]
    semantic_structure: tuple[Any, ...]

    @property
    def is_empty(self) -> bool:
        return not self.structure and not self.has_version_table


def _configuration(database: Path) -> Config:
    # Repository checkouts use the logging configuration in alembic.ini. Installed
    # wheels intentionally have no repository root, so use an in-memory Config there;
    # the packaged migration directory below remains the authoritative script source.
    configuration = Config(str(_ALEMBIC_INI)) if _ALEMBIC_INI.is_file() else Config()
    configuration.set_main_option("script_location", str(_MIGRATIONS))
    # env.py also receives the exact path below. Keeping the URL aligned prevents a
    # future environment implementation from silently falling back to another file.
    escaped_url = f"sqlite:///{database.as_posix()}".replace("%", "%%")
    configuration.set_main_option("sqlalchemy.url", escaped_url)
    return configuration


@contextmanager
def _exact_alembic_database(database: Path) -> Iterator[Config]:
    """Serialize and restore Alembic's legacy environment-based path handoff."""

    with _ALEMBIC_LOCK:
        previous = os.environ.get(_DATABASE_ENV)
        os.environ[_DATABASE_ENV] = str(database)
        try:
            yield _configuration(database)
        finally:
            if previous is None:
                os.environ.pop(_DATABASE_ENV, None)
            else:
                os.environ[_DATABASE_ENV] = previous


def _head_revision() -> str:
    configuration = _configuration(Path("schema-head-placeholder.db"))
    heads = ScriptDirectory.from_config(configuration).get_heads()
    if len(heads) != 1:
        raise DatabaseSchemaError(f"Exactly one Alembic head is required; found {len(heads)}")
    return heads[0]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_ddl(value: str | None) -> str | None:
    return None if value is None else " ".join(value.split())


def _normalize_default(table_name: str, column_name: str, value: Any) -> Any:
    if (table_name, column_name) in _LEGACY_BACKFILL_COLUMNS and value in {
        None,
        "'legacy'",
    }:
        # The 0.2.2 migration used this server default only to backfill existing
        # rows. Base.metadata.create_all() correctly omitted it for new databases.
        return None
    return value


def _split_table_definition(ddl: str) -> tuple[list[str], str]:
    """Split CREATE TABLE into top-level definitions and trailing table options."""

    start = ddl.find("(")
    if start < 0:
        raise DatabaseSchemaError("A table definition has no column list")

    definitions: list[str] = []
    item_start = start + 1
    depth = 1
    quote: str | None = None
    index = start + 1
    while index < len(ddl):
        character = ddl[index]
        if quote is not None:
            if quote == "]":
                if character == "]":
                    quote = None
            elif character == quote:
                if index + 1 < len(ddl) and ddl[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                definitions.append(ddl[item_start:index])
                return definitions, ddl[index + 1 :]
        elif character == "," and depth == 1:
            definitions.append(ddl[item_start:index])
            item_start = index + 1
        index += 1

    raise DatabaseSchemaError("A table definition has an unbalanced column list")


def _canonical_table_ddl(table_name: str, ddl: str | None) -> tuple[Any, ...] | None:
    if ddl is None:
        return None
    definitions, suffix = _split_table_definition(ddl)
    canonical_definitions: list[str] = []
    for definition in definitions:
        normalized = _normalize_ddl(definition)
        if normalized is None:
            continue
        first_token = normalized.split(maxsplit=1)[0].strip('"`[]')
        if (table_name, first_token) in _LEGACY_BACKFILL_COLUMNS:
            normalized = _LEGACY_BACKFILL_DEFAULT.sub("", normalized)
            normalized = _normalize_ddl(normalized) or ""
        canonical_definitions.append(normalized)
    return (
        tuple(sorted(canonical_definitions, key=lambda item: (item.casefold(), item))),
        _normalize_ddl(suffix) or "",
    )


def _canonical_foreign_keys(rows: tuple[Any, ...]) -> tuple[Any, ...]:
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(tuple(row))

    constraints: list[tuple[Any, ...]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda row: int(row[1]))
        first = ordered[0]
        constraints.append(
            (
                str(first[2]),
                str(first[5]),
                str(first[6]),
                str(first[7]),
                tuple((str(row[3]), str(row[4])) for row in ordered),
            )
        )
    return tuple(sorted(constraints, key=repr))


def _canonical_index(index: tuple[Any, ...]) -> tuple[Any, ...]:
    stored_name, unique, origin, partial, index_columns, index_sql = index
    key_columns = tuple(
        (
            row[2],
            int(row[3]),
            row[4],
        )
        for row in sorted(index_columns, key=lambda row: int(row[0]))
        if int(row[5]) == 1
    )
    return (
        stored_name,
        int(unique),
        str(origin),
        int(partial),
        key_columns,
        index_sql,
    )


def _semantic_structure(structure: tuple[Any, ...]) -> tuple[Any, ...]:
    canonical: list[tuple[Any, ...]] = []
    for item in structure:
        if item[0] == "__schema_object__":
            canonical.append(item)
            continue
        table_name, columns, foreign_keys, indexes, table_sql = item
        canonical_columns = tuple(
            sorted(
                (
                    str(row[1]),
                    " ".join(str(row[2]).split()).upper(),
                    int(row[3]),
                    _normalize_default(str(table_name), str(row[1]), row[4]),
                    int(row[5]),
                    int(row[6]),
                )
                for row in columns
            )
        )
        canonical.append(
            (
                table_name,
                canonical_columns,
                _canonical_foreign_keys(foreign_keys),
                tuple(sorted((_canonical_index(index) for index in indexes), key=repr)),
                _canonical_table_ddl(str(table_name), table_sql),
            )
        )
    return tuple(canonical)


def _connect_read_only(database: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseSchemaError("The database cannot be opened read-only as SQLite") from exc


def _inspect_database(database: Path) -> _DatabaseInspection:
    if not database.exists() or database.stat().st_size == 0:
        return _DatabaseInspection(
            revision=None,
            has_version_table=False,
            structure=(),
            semantic_structure=(),
        )
    if not database.is_file():
        raise DatabaseSchemaError("The database path is not a regular file")

    try:
        with closing(_connect_read_only(database)) as connection:
            object_rows = connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            has_version_table = any(
                row[0] == "table" and row[1] == "alembic_version" for row in object_rows
            )
            revision: str | None = None
            if has_version_table:
                rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
                if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0].strip():
                    raise DatabaseSchemaError(
                        "The Alembic version table must contain exactly one revision"
                    )
                revision = rows[0][0]

            tables = sorted(
                row[1] for row in object_rows if row[0] == "table" and row[1] != "alembic_version"
            )
            table_structures: list[tuple[Any, ...]] = []
            for table_name in tables:
                quoted = _quote_identifier(table_name)
                columns = tuple(connection.execute(f"PRAGMA table_xinfo({quoted})").fetchall())
                foreign_keys = tuple(
                    sorted(connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall())
                )
                indexes: list[tuple[Any, ...]] = []
                for index_row in connection.execute(f"PRAGMA index_list({quoted})").fetchall():
                    _, index_name, unique, origin, partial = index_row[:5]
                    index_quoted = _quote_identifier(str(index_name))
                    index_columns = tuple(
                        connection.execute(f"PRAGMA index_xinfo({index_quoted})").fetchall()
                    )
                    stored_name = (
                        None if str(index_name).startswith("sqlite_autoindex_") else index_name
                    )
                    index_sql_row = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (index_name,),
                    ).fetchone()
                    indexes.append(
                        (
                            stored_name,
                            int(unique),
                            str(origin),
                            int(partial),
                            index_columns,
                            _normalize_ddl(index_sql_row[0] if index_sql_row else None),
                        )
                    )
                table_sql = next(
                    (row[3] for row in object_rows if row[0] == "table" and row[1] == table_name),
                    None,
                )
                table_structures.append(
                    (
                        table_name,
                        columns,
                        foreign_keys,
                        tuple(sorted(indexes, key=repr)),
                        _normalize_ddl(table_sql),
                    )
                )

            auxiliary = tuple(
                ("__schema_object__", row[0], row[1], row[2], _normalize_ddl(row[3]))
                for row in object_rows
                if row[0] in {"trigger", "view"}
            )
            structure = (*table_structures, *auxiliary)
            return _DatabaseInspection(
                revision=revision,
                has_version_table=has_version_table,
                structure=structure,
                semantic_structure=_semantic_structure(structure),
            )
    except DatabaseSchemaError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseSchemaError("The database schema is not readable SQLite") from exc


def _upgrade(database: Path, revision: str) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with _exact_alembic_database(database) as configuration:
        command.upgrade(configuration, revision)


def _stamp_and_upgrade(database: Path, stamped_revision: str) -> None:
    with _exact_alembic_database(database) as configuration:
        command.stamp(configuration, stamped_revision)
        command.upgrade(configuration, "head")


def _reference_inspection(revision: str) -> _DatabaseInspection:
    with TemporaryDirectory(prefix="cad-harness-schema-") as directory:
        database = Path(directory) / "reference.db"
        _upgrade(database, revision)
        return _inspect_database(database)


def _require_semantic_structure(
    inspection: _DatabaseInspection,
    *,
    expected_revision: str,
) -> None:
    try:
        expected = _reference_inspection(expected_revision).semantic_structure
    except Exception as exc:
        if isinstance(exc, DatabaseSchemaError):
            raise
        raise DatabaseSchemaError(
            f"Cannot build the trusted Alembic schema for revision {expected_revision}"
        ) from exc
    if inspection.semantic_structure != expected:
        raise DatabaseSchemaError(
            "Database structure does not semantically match its trusted Alembic revision; "
            "refusing to stamp or migrate it"
        )


def assert_database_current(database_path: Path | str) -> str:
    """Prove that one SQLite file semantically matches the sole Alembic head."""

    database = Path(database_path).expanduser().resolve()
    inspection = _inspect_database(database)
    head = _head_revision()
    if not inspection.has_version_table or inspection.revision != head:
        raise DatabaseSchemaError(f"Database revision is not current; expected {head}")
    _require_semantic_structure(inspection, expected_revision=head)
    return head


def upgrade_database(database_path: Path | str) -> str:
    """Safely upgrade a new, versioned, or trusted unversioned-v0.2.2 database."""

    database = Path(database_path).expanduser().resolve()
    inspection = _inspect_database(database)
    head = _head_revision()

    if inspection.is_empty:
        _upgrade(database, "head")
        return assert_database_current(database)

    if inspection.has_version_table:
        assert inspection.revision is not None
        _require_semantic_structure(inspection, expected_revision=inspection.revision)
        _upgrade(database, "head")
        return assert_database_current(database)

    _require_semantic_structure(inspection, expected_revision=_UNVERSIONED_BASELINE)
    _stamp_and_upgrade(database, _UNVERSIONED_BASELINE)
    current = assert_database_current(database)
    if current != head:  # Defensive: assert_database_current already enforces this.
        raise DatabaseSchemaError(f"Database stopped at unexpected revision {current}")
    return current


__all__ = [
    "DatabaseSchemaError",
    "assert_database_current",
    "upgrade_database",
]
