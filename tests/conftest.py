"""Gemeinsame Test-Fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import Config


@pytest.fixture
def engine():
    """In-Memory-SQLite mit allen Tabellen.

    ``StaticPool`` wäre bei mehreren Verbindungen nötig; die Tests nutzen genau
    eine Session, daher reicht die einfache Variante.
    """
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(test_engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture
def session(engine) -> Iterator[Session]:  # noqa: ANN001
    with Session(engine) as test_session:
        yield test_session


@pytest.fixture
def config() -> Config:
    """Config aus der echten config.yaml des Projekts.

    Bewusst die echte Datei: so schlagen die Tests fehl, wenn jemand die Config
    kaputt macht — genau das soll auffallen.
    """
    from app.config import load_config

    return load_config(Path(__file__).resolve().parent.parent / "config.yaml")
