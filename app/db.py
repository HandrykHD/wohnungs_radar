"""Datenbank-Engine, Session-Handling und Dedup-Logik."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import AppState, Listing, ListingStatus, utcnow

logger = logging.getLogger(__name__)

_engine: Engine | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """SQLite-Pragmas für den Dauerbetrieb setzen.

    ``WAL`` erlaubt gleichzeitiges Lesen (Web-Requests) und Schreiben
    (Scheduler-Job) ohne "database is locked". ``busy_timeout`` lässt kurze
    Sperren aussitzen, statt sofort mit einem Fehler abzubrechen — das ist auf
    dem DrvFs-Dateisystem von WSL2 spürbar wichtig.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_engine(database_path: Path) -> Engine:
    """Engine erzeugen und Tabellen anlegen (idempotent)."""
    global _engine

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        echo=False,
        # SQLite-Verbindungen sind an einen Thread gebunden; APScheduler-Jobs
        # laufen in einem anderen Thread als die FastAPI-Requests.
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _configure_sqlite)
    SQLModel.metadata.create_all(engine)

    # Mini-Migration: create_all legt nur fehlende Tabellen an, keine Spalten.
    # Nachträglich ergänzte Listing-Spalten hier per ALTER TABLE nachziehen.
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(listing)")}
        if "car_minutes" not in existing:
            conn.exec_driver_sql("ALTER TABLE listing ADD COLUMN car_minutes FLOAT")
            conn.commit()

    _engine = engine
    logger.info("Datenbank bereit: %s", database_path)
    return engine


def get_engine() -> Engine:
    """Die initialisierte Engine holen."""
    if _engine is None:
        raise RuntimeError("init_engine() muss vor get_engine() aufgerufen werden.")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session mit automatischem Commit/Rollback."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI-Dependency: Session pro Request."""
    with Session(get_engine()) as session:
        yield session


# --- Dedup ------------------------------------------------------------------


def upsert_listing(session: Session, incoming: Listing) -> tuple[Listing, bool]:
    """Angebot einfügen oder ein bestehendes auffrischen.

    Kern des Dedup-Mechanismus. Ist der Fingerprint bereits bekannt, wird das
    Angebot **nicht** dupliziert; stattdessen wird ``last_seen_at`` aktualisiert
    und fehlende Basisdaten werden ergänzt (Portale liefern in der Listenansicht
    oft weniger Felder als in der Detailansicht).

    Bewusst *nicht* überschrieben werden:
      * ``status``        — der Nutzer hat das Angebot vielleicht schon bewertet,
      * ``first_seen_at`` — der Erstkontakt bleibt der Erstkontakt,
      * ``notified_at``   — sonst gäbe es Doppel-Benachrichtigungen,
      * alle Enrichment-Felder — die Adresse ändert sich nicht, ein erneutes
        Geocoding wäre reine Verschwendung.

    Returns:
        ``(listing, is_new)`` — ``is_new`` ist nur beim ersten Auftauchen ``True``.
    """
    existing = session.exec(
        select(Listing).where(Listing.fingerprint == incoming.fingerprint)
    ).first()

    if existing is None:
        session.add(incoming)
        session.flush()  # vergibt die ID, ohne die Transaktion zu schließen
        return incoming, True

    existing.last_seen_at = utcnow()

    # Nur auffüllen, was bisher fehlt bzw. sich sinnvoll aktualisieren lässt.
    for field in (
        "title",
        "rent_warm",
        "rent_cold",
        "size_sqm",
        "rooms",
        "district",
        "address",
        "available_from",
        "posted_at",
        "url",
    ):
        new_value = getattr(incoming, field)
        if new_value is not None and getattr(existing, field) is None:
            setattr(existing, field, new_value)

    session.add(existing)
    return existing, False


# --- App-Zustand ------------------------------------------------------------


def get_state(session: Session, key: str, default: str | None = None) -> str | None:
    """Wert aus dem Key-Value-Zustand lesen."""
    row = session.get(AppState, key)
    return row.value if row else default


def set_state(session: Session, key: str, value: str) -> None:
    """Wert im Key-Value-Zustand schreiben."""
    row = session.get(AppState, key)
    if row is None:
        row = AppState(key=key, value=value)
    else:
        row.value = value
        row.updated_at = utcnow()
    session.add(row)


def count_new_since(session: Session, since: datetime | None) -> int:
    """Anzahl der Angebote zählen, die seit ``since`` neu dazugekommen sind."""
    statement = select(Listing).where(Listing.status == ListingStatus.NEU)
    if since is not None:
        statement = statement.where(Listing.first_seen_at > since)
    return len(session.exec(statement).all())
