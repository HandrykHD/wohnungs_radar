"""Persistenter Cache für teure externe Geo-Abfragen.

Adressen von Angeboten ändern sich nicht — jede Nominatim-, Routing-, POI- oder
ÖPNV-Antwort wird daher unter einem stabilen Schlüssel in der ``GeoCache``-Tabelle
abgelegt und wiederverwendet. Das schont die (kostenlosen, fremden) Dienste und
macht wiederholte Läufe schnell.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlmodel import Session, select

from app.models import GeoCache, utcnow

logger = logging.getLogger(__name__)


def cache_get(session: Session, kind: str, key: str, ttl_days: int) -> Any | None:
    """Gecachten Wert lesen, sofern vorhanden und nicht abgelaufen.

    Args:
        session: DB-Session.
        kind: Grobkategorie (``"geocode"``, ``"route"``, ``"poi"``, ``"transit"``).
        key: Eindeutiger Schlüssel innerhalb der Kategorie.
        ttl_days: Höchstalter in Tagen; darüber gilt der Eintrag als veraltet.

    Returns:
        Das deserialisierte JSON oder ``None`` bei Fehltreffer/Ablauf.
    """
    full_key = f"{kind}:{key}"
    row = session.exec(select(GeoCache).where(GeoCache.cache_key == full_key)).first()
    if row is None:
        return None

    if utcnow() - row.created_at > timedelta(days=ttl_days):
        # Abgelaufen: löschen, damit er neu geholt wird.
        session.delete(row)
        session.flush()
        return None

    try:
        return json.loads(row.payload_json)
    except json.JSONDecodeError:
        logger.warning("Beschädigter Cache-Eintrag %s — wird verworfen", full_key)
        session.delete(row)
        session.flush()
        return None


def cache_set(session: Session, kind: str, key: str, payload: Any) -> None:
    """Wert im Cache ablegen (überschreibt einen vorhandenen Eintrag).

    ``payload`` muss JSON-serialisierbar sein. ``None`` wird ebenfalls gespeichert
    und gilt als gültiges Ergebnis ("hier gibt es nachweislich nichts") — so wird
    eine erfolglose, aber teure Abfrage nicht bei jedem Lauf wiederholt.
    """
    full_key = f"{kind}:{key}"
    row = session.exec(select(GeoCache).where(GeoCache.cache_key == full_key)).first()
    serialized = json.dumps(payload, ensure_ascii=False)

    if row is None:
        session.add(GeoCache(kind=kind, cache_key=full_key, payload_json=serialized))
    else:
        row.payload_json = serialized
        row.created_at = utcnow()
        session.add(row)
    session.flush()
