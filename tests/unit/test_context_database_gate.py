from __future__ import annotations

import sqlite3
from pathlib import Path

import apps.mcp_server.context as context_module
import pytest
from apps.cli.__main__ import main as cli_main

import cad_harness.persistence.schema_migration as migration_module
from cad_harness.persistence.schema_migration import (
    DatabaseSchemaError,
    assert_database_current,
    upgrade_database,
)


def _config(tmp_path: Path, database: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            (
                "adapter:",
                "  type: fake",
                "storage:",
                f"  sqlite_path: '{database.as_posix()}'",
                f"  preview_directory: '{(tmp_path / 'previews').as_posix()}'",
                f"  checkpoint_directory: '{(tmp_path / 'checkpoints').as_posix()}'",
                "compatibility:",
                f"  matrix_path: '{(Path('config/compatibility.yaml').resolve()).as_posix()}'",
                "pilot:",
                f"  thresholds_path: '{(Path('config/pilot.yaml').resolve()).as_posix()}'",
            )
        ),
        encoding="utf-8",
    )
    return config


def test_new_database_is_initialized_before_adapter_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "new.db"
    adapter_built = False
    real_build_adapter = context_module.build_adapter

    def observe_build_adapter(*args: object, **kwargs: object) -> object:
        nonlocal adapter_built
        with sqlite3.connect(database) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision is not None
        adapter_built = True
        return real_build_adapter(*args, **kwargs)

    monkeypatch.setattr(context_module, "build_adapter", observe_build_adapter)

    context_module.build_context(_config(tmp_path, database))

    assert adapter_built


def test_stale_database_fails_before_adapter_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "unknown.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unknown_customer_table (id INTEGER PRIMARY KEY)")

    def forbidden_adapter(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("adapter must not be constructed before the database gate")

    monkeypatch.setattr(context_module, "build_adapter", forbidden_adapter)

    with pytest.raises(DatabaseSchemaError, match="cad-harness --config"):
        context_module.build_context(_config(tmp_path, database))


def test_packaged_migrations_work_without_repository_alembic_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "packaged.db"
    monkeypatch.setattr(migration_module, "_ALEMBIC_INI", tmp_path / "missing-alembic.ini")

    revision = upgrade_database(database)

    assert revision == assert_database_current(database)


def test_cli_redacts_unsafe_database_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "customer-secret-name.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unknown_customer_table (id INTEGER PRIMARY KEY)")
    config = _config(tmp_path, database)
    monkeypatch.setattr("sys.argv", ["cad-harness", "--config", str(config), "migrate"])

    assert cli_main() == 1
    output = capsys.readouterr().out
    assert "DATABASE_SCHEMA_UNSAFE" in output
    assert database.name not in output
    assert "unknown_customer_table" not in output
