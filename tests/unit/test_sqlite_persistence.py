"""Focused unit coverage for the production SQLite persistence layer."""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from apps.mcp_server.context import build_context
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import CadJob, JobState
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import Checkpoint
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)
from cad_harness.domain.ports.repositories import JobStore
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.retry import RetryPolicy
from cad_harness.persistence.schema_migration import (
    DatabaseSchemaError,
    assert_database_current,
    upgrade_database,
)
from cad_harness.persistence.sql_audit_sink import SqlAuditSink
from cad_harness.persistence.sql_job_store import SqlJobStore


def _store(database: Path) -> SqlJobStore:
    engine = build_engine(database)
    create_all(engine)
    return SqlJobStore(build_session_factory(engine))


def _persist_planned_job(
    store: SqlJobStore,
    *,
    job_id: str,
    plan_id: str,
) -> tuple[CadJob, OperationPlan]:
    job = CadJob(
        job_id=job_id,
        document_id="doc_same_canonical_plan",
        expected_revision="sha256:same-revision",
    )
    store.save_job(job)
    job = job.transition_to(JobState.SPEC_ACCEPTED)
    store.save_job(job)
    plan = OperationPlan(
        plan_id=plan_id,
        job_id=job.job_id,
        document_id=job.document_id,
        expected_revision=job.expected_revision,
        profile_ref="demo-profile@1.0",
    ).with_hash()
    store.save_plan(plan)
    job = job.transition_to(JobState.PLANNED, plan_id=plan.plan_id, plan_hash=plan.plan_hash)
    store.save_job(job)
    return job, plan


def test_engine_enables_wal_foreign_keys_and_bounded_busy_timeout(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "pragmas.db")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 250


def test_retry_does_not_hide_non_lock_operational_errors() -> None:
    expected = OperationalError("write", {}, Exception("disk I/O error"))
    with pytest.raises(OperationalError) as caught:
        RetryPolicy().run(lambda: (_ for _ in ()).throw(expected))
    assert caught.value is expected


def test_retry_exhaustion_is_actionable() -> None:
    with pytest.raises(HarnessError) as caught:
        RetryPolicy(sleep=lambda _delay: None).run(
            lambda: (_ for _ in ()).throw(
                OperationalError("write", {}, Exception("database is locked"))
            )
        )
    assert caught.value.required_action
    assert caught.value.details["attempts"] == 5


def test_sql_job_store_implements_full_port(
    tmp_path: Path, base_plate_spec: dict[str, Any]
) -> None:
    store = _store(tmp_path / "full-port.db")
    assert isinstance(store, JobStore)

    job = CadJob(job_id="job_sql", document_id="doc_sql", expected_revision="rev_1")
    store.save_job(job)
    spec = DrawingSpec.model_validate(
        {
            **base_plate_spec,
            "spec_id": "spec_sql",
            "document_id": job.document_id,
            "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
        }
    )
    version = store.save_spec(job.job_id, spec)
    job = job.transition_to(JobState.SPEC_ACCEPTED).model_copy(
        update={"spec_id": spec.spec_id, "spec_version": version}
    )
    store.save_job(job)

    plan = OperationPlan(
        plan_id="plan_sql",
        job_id=job.job_id,
        document_id=job.document_id,
        expected_revision=job.expected_revision,
        profile_ref="demo-profile@1.0",
    ).with_hash()
    store.save_plan(plan)
    remediation_payload = {
        "plan": plan.model_dump(mode="json"),
        "audit_id": "audit_sql",
        "operation_sources": [],
        "selected_findings": [["DUPLICATE_ENTITY", "entity:sql"]],
        "technical_inputs": {},
    }
    store.save_remediation(
        job_id=job.job_id,
        plan_hash=str(plan.plan_hash),
        payload=remediation_payload,
    )
    job = job.transition_to(JobState.PLANNED, plan_id=plan.plan_id, plan_hash=plan.plan_hash)
    store.save_job(job)
    report = ValidationReport(
        validation_id="validation_sql",
        job_id=job.job_id,
        stage=ValidationStage.PRE_COMMIT,
        plan_hash=plan.plan_hash,
        findings=(Finding(rule_id="SQL", severity=Severity.WARNING, message="stored"),),
    )
    store.save_validation(report)

    now = datetime.now(UTC)
    approval = ApprovalRecord(
        approval_id="approval_sql",
        job_id=job.job_id,
        document_id=job.document_id,
        expected_revision=job.expected_revision,
        plan_hash=str(plan.plan_hash),
        approved_by="engineer",
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    store.save_approval(approval)
    store.record_execution(
        job_id=job.job_id,
        idempotency_key="key_sql",
        request_digest="digest_sql",
        result={"status": "committed", "value": 1},
    )
    store.map_entity(
        document_id=job.document_id,
        feature_id="feature:sql",
        operation_id="op:sql",
        entity_ref="entity:sql",
        revision="rev_2",
        expected_layer="0",
    )
    store.save_checkpoint(
        Checkpoint(
            checkpoint_id="checkpoint_sql",
            job_id=job.job_id,
            revision="rev_1",
            artifact_ref="checkpoints/sql.dwg",
        )
    )

    assert store.get_job(job.job_id) is not None
    assert store.get_spec(job.job_id) == spec
    assert store.get_plan(job.job_id) == plan
    assert store.entity_mappings_for(job.document_id)[0].expected_layer == "0"
    assert store.get_remediation(job.job_id) == (str(plan.plan_hash), remediation_payload)
    assert store.get_validation(job.job_id) == report
    assert store.get_approval(approval.approval_id) == approval
    assert store.find_execution(job_id=job.job_id, idempotency_key="key_sql") == (
        "digest_sql",
        {"status": "committed", "value": 1},
    )
    assert store.entity_mappings_for(job.document_id)[0].operation_id == "op:sql"
    store.revoke_approvals_for_job(job.job_id)
    assert store.get_approval(approval.approval_id) is None


def test_sqlite_scopes_repeated_canonical_plan_hashes_to_their_jobs(tmp_path: Path) -> None:
    store = _store(tmp_path / "repeated-plan-hash.db")

    first_job, first_plan = _persist_planned_job(
        store,
        job_id="job_same_plan_a",
        plan_id="plan_same_plan_a",
    )
    second_job, second_plan = _persist_planned_job(
        store,
        job_id="job_same_plan_b",
        plan_id="plan_same_plan_b",
    )

    assert first_plan.plan_hash == second_plan.plan_hash
    assert store.get_job(first_job.job_id) == first_job
    assert store.get_job(second_job.job_id) == second_job
    assert store.get_plan(first_job.job_id) == first_plan
    assert store.get_plan(second_job.job_id) == second_plan


def test_runtime_restart_restores_validated_job_and_findings(
    tmp_path: Path, base_plate_spec: dict[str, Any]
) -> None:
    config_path = tmp_path / "runtime.yaml"
    database = tmp_path / "runtime.db"
    config_path.write_text(
        "\n".join(
            [
                "app:",
                "  environment: development",
                "storage:",
                f"  sqlite_path: '{database.as_posix()}'",
                f"  preview_directory: '{(tmp_path / 'previews').as_posix()}'",
                f"  checkpoint_directory: '{(tmp_path / 'checkpoints').as_posix()}'",
                f"  export_directory: '{(tmp_path / 'exports').as_posix()}'",
                "observability:",
                "  log_level: WARNING",
                "  log_json: false",
            ]
        ),
        encoding="utf-8",
    )
    first_context = build_context(config_path)
    assert isinstance(first_context.service.store, SqlJobStore)
    assert isinstance(first_context.service.audit, SqlAuditSink)

    job = first_context.service.create_job()
    submitted = first_context.service.submit_spec(job.job_id, base_plate_spec)
    first_context.service.preview(job.job_id)
    report = first_context.service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    stored_before = first_context.service.store.get_job(job.job_id)
    assert stored_before is not None and stored_before.state is JobState.VALIDATED

    restarted_context = build_context(config_path)
    restored = restarted_context.service.store.get_job(job.job_id)
    restored_report = restarted_context.service.store.get_validation(job.job_id)
    assert restored is not None
    assert restored.state is JobState.VALIDATED
    assert restored.plan_hash == submitted["plan_hash"]
    assert restored.expected_revision == job.expected_revision
    assert restored_report is not None
    assert restored_report.findings == report.findings
    assert isinstance(restarted_context.service.audit, SqlAuditSink)
    assert restarted_context.service.audit.verify_chain(job.job_id)


def test_finalize_commit_persists_checkpoint_and_replay_evidence_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "checkpoint-replay.db"
    store = _store(database)
    created = CadJob(
        job_id="job_checkpoint_replay",
        document_id="doc_checkpoint_replay",
        expected_revision="revision-before",
    )
    store.save_job(created)
    terminal = created.model_copy(
        update={
            "state": JobState.FAILED,
            "expected_revision": "revision-after",
            "checkpoint_id": "checkpoint-replay",
        }
    )
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-replay",
        job_id=created.job_id,
        revision="revision-before",
        artifact_ref="adapter-checkpoint://checkpoint-replay",
    )

    store.finalize_commit(
        terminal,
        lease_id="lease-not-present",
        now=datetime.now(UTC),
        mappings=(),
        idempotency_key="post-commit-failure",
        request_digest="digest-post-commit",
        result={"status": "committed", "checkpoint_id": checkpoint.checkpoint_id},
        checkpoint=checkpoint,
    )

    restarted = _store(database)
    restored = restarted.get_job(created.job_id)
    assert restored is not None
    assert restored.state is JobState.FAILED
    assert restored.expected_revision == "revision-after"
    assert restored.checkpoint_id == checkpoint.checkpoint_id
    assert restarted.find_execution(
        job_id=created.job_id, idempotency_key="post-commit-failure"
    ) == (
        "digest-post-commit",
        {"status": "committed", "checkpoint_id": checkpoint.checkpoint_id},
    )


def test_domain_has_no_sqlalchemy_imports() -> None:
    domain = Path(__file__).parents[2] / "src" / "cad_harness" / "domain"
    violations: list[str] = []
    for source_path in domain.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(
                module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in modules
            ):
                violations.append(str(source_path.relative_to(domain)))
    assert violations == []


def _metadata_snapshot(database: Path) -> dict[str, object]:
    inspector = inspect(build_engine(database))
    snapshot: dict[str, object] = {}
    for table in sorted(inspector.get_table_names()):
        snapshot[table] = {
            "columns": [
                (column["name"], str(column["type"]), column["nullable"])
                for column in inspector.get_columns(table)
            ],
            "primary_key": inspector.get_pk_constraint(table),
            "foreign_keys": inspector.get_foreign_keys(table),
            "indexes": inspector.get_indexes(table),
            "unique_constraints": inspector.get_unique_constraints(table),
        }
    return snapshot


def _upgrade_to_revision(
    database: Path,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    root = Path(__file__).parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), revision)


def _create_legacy_v022_create_all_database(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = build_engine(database)
    create_all(engine)
    engine.dispose()
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    root = Path(__file__).parents[2]
    configuration = Config(str(root / "alembic.ini"))
    command.stamp(configuration, "head")
    command.downgrade(configuration, "e6b3f91a2c40")
    with sqlite3.connect(database) as connection:
        # The downgrade names this constraint; v0.2.2 create_all() emitted the
        # same unique invariant anonymously, as did the real live-com database.
        connection.executescript(
            """
            ALTER TABLE plans RENAME TO plans_with_named_unique;
            CREATE TABLE plans (
                plan_id VARCHAR(64) NOT NULL,
                job_id VARCHAR(64) NOT NULL,
                schema_version VARCHAR(16) NOT NULL,
                plan_json JSON NOT NULL,
                plan_hash VARCHAR(80) NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (plan_id),
                UNIQUE (plan_hash),
                FOREIGN KEY(job_id) REFERENCES jobs (job_id)
            );
            INSERT INTO plans
                (plan_id, job_id, schema_version, plan_json, plan_hash, created_at)
            SELECT plan_id, job_id, schema_version, plan_json, plan_hash, created_at
            FROM plans_with_named_unique;
            DROP TABLE plans_with_named_unique;
            """
        )
    _remove_alembic_version(database)


def _remove_alembic_version(database: Path, *sql: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE alembic_version")
        for statement in sql:
            connection.execute(statement)


def _file_digest(database: Path) -> str:
    return hashlib.sha256(database.read_bytes()).hexdigest()


def _data_snapshot(
    database: Path,
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[tuple[Any, ...], ...]]]:
    selected_columns: dict[str, tuple[str, ...]] = {}
    rows_by_table: dict[str, tuple[tuple[Any, ...], ...]] = {}
    with sqlite3.connect(database) as connection:
        tables = sorted(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name != 'alembic_version'
                """
            ).fetchall()
        )
        for table in tables:
            if columns_by_table is None:
                quoted_table = '"' + table.replace('"', '""') + '"'
                columns = tuple(
                    str(row[1]) for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
                )
            else:
                columns = columns_by_table[table]
            selected_columns[table] = columns
            quoted_columns = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
            quoted_table = '"' + table.replace('"', '""') + '"'
            rows = connection.execute(f"SELECT {quoted_columns} FROM {quoted_table}").fetchall()
            rows_by_table[table] = tuple(sorted((tuple(row) for row in rows), key=repr))
    return selected_columns, rows_by_table


@pytest.mark.parametrize("existing_empty_file", [False, True])
def test_safe_migration_upgrades_new_or_empty_exact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_empty_file: bool,
) -> None:
    database = tmp_path / "intended" / "harness.db"
    if existing_empty_file:
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("VACUUM")
    decoy = tmp_path / "must-not-be-opened.db"
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(decoy))

    revision = upgrade_database(database)

    assert revision == "a41d6b8c9e20"
    assert assert_database_current(database) == revision
    assert not decoy.exists()
    assert os.environ["CAD_HARNESS_SQLITE_PATH"] == str(decoy)


def test_safe_migration_upgrades_a_structurally_exact_stale_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "stale-versioned.db"
    _upgrade_to_revision(database, "e6b3f91a2c40", monkeypatch)

    assert upgrade_database(database) == "a41d6b8c9e20"
    assert assert_database_current(database) == "a41d6b8c9e20"


def test_safe_migration_adopts_only_semantically_exact_unversioned_v022_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "unversioned-v022.db"
    _create_legacy_v022_create_all_database(database, monkeypatch)
    with sqlite3.connect(database) as connection:
        effort_columns = connection.execute("PRAGMA table_xinfo(effort_records)").fetchall()
        assert effort_columns[1][1:5] == ("pilot_run_id", "VARCHAR(128)", 1, None)
        connection.execute(
            """
            INSERT INTO documents (document_id, path_hash, current_revision, last_seen_at)
            VALUES ('doc-preserved', 'sha256:path', 'sha256:revision', '2026-08-16 00:00:00')
            """
        )
    assert upgrade_database(database) == "a41d6b8c9e20"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT current_revision FROM documents WHERE document_id = 'doc-preserved'"
        ).fetchone() == ("sha256:revision",)
    assert assert_database_current(database) == "a41d6b8c9e20"


def test_safe_migration_upgrades_copy_of_live_com_v022_database_without_data_loss(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[2] / "data" / "live-com" / "harness.db"
    if not source.exists():
        pytest.skip("local ignored live-com v0.2.2 database is not available")
    database = tmp_path / "live-com-v022-copy.db"
    with (
        sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as source_connection,
        sqlite3.connect(database) as target_connection,
    ):
        source_connection.backup(target_connection)
    with sqlite3.connect(database) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone():
            pytest.skip("local live-com database is no longer unversioned v0.2.2")
    original_columns, before = _data_snapshot(database)

    assert upgrade_database(database) == "a41d6b8c9e20"

    _, after = _data_snapshot(database, original_columns)
    assert after == before
    assert assert_database_current(database) == "a41d6b8c9e20"


@pytest.mark.parametrize(
    "drift_sql",
    [
        "DROP TABLE remediation_selections",
        "ALTER TABLE documents ADD COLUMN unexpected TEXT",
    ],
)
def test_safe_migration_rejects_partial_or_drifted_unversioned_schema_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_sql: str,
) -> None:
    database = tmp_path / f"drift-{hashlib.sha256(drift_sql.encode()).hexdigest()[:8]}.db"
    _create_legacy_v022_create_all_database(database, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(drift_sql)
    before = _file_digest(database)

    with pytest.raises(DatabaseSchemaError, match="refusing to stamp or migrate"):
        upgrade_database(database)

    assert _file_digest(database) == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
            ).fetchone()
            is None
        )


def test_safe_migration_rejects_drifted_versioned_schema_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "drifted-versioned.db"
    _upgrade_to_revision(database, "e6b3f91a2c40", monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE documents ADD COLUMN unexpected TEXT")
    before = _file_digest(database)

    with pytest.raises(DatabaseSchemaError, match="refusing to stamp or migrate"):
        upgrade_database(database)

    assert _file_digest(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "e6b3f91a2c40",
        )


def test_safe_migration_rejects_unknown_schema_and_revision_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown_schema = tmp_path / "unknown-schema.db"
    with sqlite3.connect(unknown_schema) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, payload TEXT)")
    schema_before = _file_digest(unknown_schema)
    with pytest.raises(DatabaseSchemaError, match="refusing to stamp or migrate"):
        upgrade_database(unknown_schema)
    assert _file_digest(unknown_schema) == schema_before

    unknown_revision = tmp_path / "unknown-revision.db"
    _upgrade_to_revision(unknown_revision, "e6b3f91a2c40", monkeypatch)
    with sqlite3.connect(unknown_revision) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'unknown_revision'")
    revision_before = _file_digest(unknown_revision)
    with pytest.raises(DatabaseSchemaError, match="trusted Alembic schema"):
        upgrade_database(unknown_revision)
    assert _file_digest(unknown_revision) == revision_before


def test_alembic_upgrade_head_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "migration.db"
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    root = Path(__file__).parents[2]
    configuration = Config(str(root / "alembic.ini"))

    command.upgrade(configuration, "head")
    first = _metadata_snapshot(database)
    command.upgrade(configuration, "head")
    second = _metadata_snapshot(database)

    assert second == first
    assert {
        "writer_leases",
        "takeoff_reports",
        "drawing_audits",
        "effort_records",
        "baseline_cases",
        "operation_metrics",
    } <= first.keys()
    inspector = inspect(build_engine(database))
    unique_constraints = inspector.get_unique_constraints("writer_leases")
    assert any(item["column_names"] == ["document_id"] for item in unique_constraints)


def test_plan_hash_migration_preserves_rows_and_allows_cross_job_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "duplicate-plan-hash-migration.db"
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    root = Path(__file__).parents[2]
    configuration = Config(str(root / "alembic.ini"))
    command.upgrade(configuration, "e6b3f91a2c40")

    legacy_engine = build_engine(database)
    legacy_store = SqlJobStore(build_session_factory(legacy_engine))
    first_job, first_plan = _persist_planned_job(
        legacy_store,
        job_id="job_migrated_plan_a",
        plan_id="plan_migrated_plan_a",
    )
    legacy_engine.dispose()

    command.upgrade(configuration, "head")
    migrated_engine = build_engine(database)
    migrated_store = SqlJobStore(build_session_factory(migrated_engine))
    second_job, second_plan = _persist_planned_job(
        migrated_store,
        job_id="job_migrated_plan_b",
        plan_id="plan_migrated_plan_b",
    )

    assert first_plan.plan_hash == second_plan.plan_hash
    assert migrated_store.get_job(first_job.job_id) == first_job
    assert migrated_store.get_job(second_job.job_id) == second_job
    inspector = inspect(migrated_engine)
    assert not any(
        constraint["column_names"] == ["plan_hash"]
        for constraint in inspector.get_unique_constraints("plans")
    )
    assert any(
        index["name"] == "ix_plans_job_id_plan_hash"
        and index["column_names"] == ["job_id", "plan_hash"]
        and not index["unique"]
        for index in inspector.get_indexes("plans")
    )


def test_pilot_metrics_migration_preserves_legacy_baseline_and_rekeys_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pilot-migration.db"
    monkeypatch.setenv("CAD_HARNESS_SQLITE_PATH", str(database))
    root = Path(__file__).parents[2]
    configuration = Config(str(root / "alembic.ini"))
    command.upgrade(configuration, "8d4f2c7a1b90")

    engine = build_engine(database)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO documents (
                    document_id, path_hash, current_revision, last_seen_at
                ) VALUES ('doc-pilot', 'sha256:path', 'sha256:revision', '2026-08-09 00:00:00')
                """
            )
        )
        for job_id in ("job-legacy-a", "job-legacy-b"):
            connection.execute(
                text(
                    """
                    INSERT INTO jobs (
                        job_id, document_id, state, expected_revision, current_spec_version,
                        created_at, updated_at
                    ) VALUES (
                        :job_id, 'doc-pilot', 'failed', 'sha256:revision', 1,
                        '2026-08-09 00:00:00', '2026-08-09 00:00:00'
                    )
                    """
                ),
                {"job_id": job_id},
            )
        connection.execute(
            text(
                """
                INSERT INTO baseline_cases (
                    case_id, capability_group, work_label, manual_minutes,
                    manual_measured_by, manual_measurement_biased,
                    manual_measured_in_single_session, created_at
                ) VALUES (
                    'same-case', 'B', 've_moi', 10.0, 'engineer-1', 0, 1,
                    '2026-08-09 00:00:00'
                )
                """
            )
        )
        for record_id, job_id, completed in (
            ("effort-legacy-a", "job-legacy-a", 0),
            ("effort-legacy-b", "job-legacy-b", 1),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO effort_records (
                        record_id, case_id, job_id, harness_minutes, idle_minutes_excluded,
                        manual_fixup_minutes, spec_change_count, entities_created,
                        entities_manually_edited, first_preview_clean, completed, created_at
                    ) VALUES (
                        :record_id, 'same-case', :job_id, 2.0, 0.0, 0.0, 0, 0, 0, 0,
                        :completed, '2026-08-09 00:00:00'
                    )
                    """
                ),
                {"record_id": record_id, "job_id": job_id, "completed": completed},
            )

    command.upgrade(configuration, "head")
    with engine.begin() as connection:
        legacy = connection.execute(
            text(
                "SELECT baseline_record_id, pilot_run_id, case_id "
                "FROM baseline_cases WHERE case_id = 'same-case'"
            )
        ).one()
        connection.execute(
            text(
                """
                INSERT INTO baseline_cases (
                    baseline_record_id, case_id, pilot_run_id, capability_group,
                    work_label, manual_minutes, manual_measured_by,
                    manual_measurement_biased, manual_measured_in_single_session,
                    created_at
                ) VALUES (
                    'run-b:same-case', 'same-case', 'run-b', 'B', 've_moi',
                    10.0, 'engineer-1', 0, 1, '2026-08-09 00:00:00'
                )
                """
            )
        )
        efforts = connection.execute(
            text(
                "SELECT record_id, pilot_run_id, failure_reason FROM effort_records "
                "ORDER BY record_id"
            )
        ).all()

    assert legacy == ("legacy:same-case", "legacy", "same-case")
    assert efforts == [
        ("effort-legacy-a", "legacy", "unsupported_feature"),
        ("effort-legacy-b", "legacy:effort-legacy-b", None),
    ]
    inspector = inspect(engine)
    assert inspector.get_pk_constraint("baseline_cases")["constrained_columns"] == [
        "baseline_record_id"
    ]
    assert any(
        constraint["column_names"] == ["pilot_run_id", "case_id"]
        for constraint in inspector.get_unique_constraints("baseline_cases")
    )
