"""SQLite engine and session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from cad_harness.persistence.models import Base


def build_engine(sqlite_path: Path, *, echo: bool = False) -> Engine:
    """Create an engine with the pragmas a concurrent local workstation needs.

    WAL lets a read (an inspect) proceed while a write (a commit record) is in flight,
    and ``busy_timeout`` turns a transient lock into a wait rather than an error.
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{sqlite_path}", echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create tables directly.

    For development and tests only. Pilot and production machines run Alembic so the
    schema history stays auditable.
    """
    Base.metadata.create_all(engine)
