"""SQLModel-Tabellen für Angebote, Geo-Cache und App-Zustand."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """``now()`` in UTC, aber **ohne** tzinfo (naiv).

    SQLite kennt keinen Zeitzonen-Typ und liefert gespeicherte Werte grundsätzlich
    naiv zurück. Würde man tz-bewusste Werte speichern, vergliche man nach dem
    Roundtrip aware gegen naive Datetimes — das wirft im Betrieb einen
    ``TypeError``. Deshalb ist die Konvention im ganzen Projekt: **naives UTC**
    speichern, erst bei der Anzeige (``main._format_datetime``) als UTC
    interpretieren und in die lokale Zeit umrechnen.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class ListingStatus(StrEnum):
    """Bearbeitungsstand eines Angebots aus Nutzersicht."""

    NEU = "neu"
    GESEHEN = "gesehen"
    FAVORISIERT = "favorisiert"
    AUSGEBLENDET = "ausgeblendet"


class ListingType(StrEnum):
    """Objekttyp — bestimmt, welche Budget-/Größenlimits greifen."""

    WG_ROOM = "wg_room"
    APARTMENT = "apartment"


def make_fingerprint(source: str, url: str, external_id: str | None = None) -> str:
    """Stabilen Dedup-Schlüssel für ein Angebot bilden.

    Priorität hat die portal-eigene ID: dieselbe Anzeige ist über mehrere
    URL-Varianten erreichbar (Tracking-Parameter, Session-IDs, geänderte
    Titel-Slugs), die ID bleibt aber gleich. Nur wenn keine ID vorliegt, wird auf
    die normalisierte URL zurückgefallen — ohne Query-String und Fragment, da
    genau dort die volatilen Parameter sitzen.

    Args:
        source: Portal-Kennung, z.B. ``"wg_gesucht"``.
        url: Vollständige Angebots-URL.
        external_id: Vom Portal vergebene Anzeigen-ID, falls extrahierbar.

    Returns:
        Hex-SHA256 über ``source`` und den stabilsten verfügbaren Identifikator.
    """
    if external_id:
        basis = f"{source}:id:{external_id}"
    else:
        parts = urlsplit(url.strip().lower())
        # Trailing Slash entfernen, damit /angebot/123 und /angebot/123/ gleich sind.
        path = parts.path.rstrip("/")
        basis = f"{source}:url:{parts.netloc}{path}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalize_district(raw: str | None) -> str | None:
    """Stadtteil-Angaben der Portale auf einen schlanken Namen bringen.

    Die Portale liefern sehr uneinheitliche Strings, z.B.
    ``"München Schwabing-West"`` oder ``"85748 Garching bei München"``.
    Entfernt werden Postleitzahlen, das Präfix ``München`` und Zusätze wie
    ``bei München``.
    """
    if not raw:
        return None
    text = re.sub(r"\b\d{5}\b", " ", raw)  # PLZ raus
    text = re.sub(r"\bbei München\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*München\s*[-,]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s,;]+", " ", text).strip(" -,")
    return text or None


class Listing(SQLModel, table=True):
    """Ein gesammeltes Wohnungs-/WG-Angebot samt Anreicherung."""

    __tablename__ = "listing"

    id: int | None = Field(default=None, primary_key=True)

    # --- Identität / Dedup ---------------------------------------------------
    # Eindeutiger Index: der Dedup-Mechanismus verlässt sich darauf, dass die
    # Datenbank ein zweites Einfügen desselben Angebots ablehnt.
    fingerprint: str = Field(index=True, unique=True)
    source: str = Field(index=True)
    external_id: str | None = Field(default=None, index=True)
    url: str

    # --- Basisdaten ----------------------------------------------------------
    title: str
    listing_type: ListingType = Field(default=ListingType.WG_ROOM, index=True)
    rent_warm: float | None = None
    rent_cold: float | None = None
    size_sqm: float | None = None
    rooms: float | None = None
    district: str | None = Field(default=None, index=True)
    address: str | None = None
    available_from: str | None = None
    posted_at: datetime | None = None

    # --- Geo (M2) ------------------------------------------------------------
    lat: float | None = None
    lon: float | None = None
    geocoded_at: datetime | None = None

    # --- Entfernungen zum primären Campus (M2) -------------------------------
    distance_km_crow: float | None = None  # Luftlinie
    walk_minutes: float | None = None
    bike_minutes: float | None = None
    transit_minutes: float | None = None
    transit_changes: int | None = None
    transit_summary: str | None = None  # z.B. "U6 → Bus 230, 2x umsteigen"

    # --- Umgebung (M2), Distanz in Metern ------------------------------------
    supermarket_m: float | None = None
    pharmacy_m: float | None = None
    transit_stop_m: float | None = None
    transit_stop_name: str | None = None

    # --- Bewertung -----------------------------------------------------------
    score: float | None = Field(default=None, index=True)
    enriched_at: datetime | None = None

    # --- Zustand -------------------------------------------------------------
    status: ListingStatus = Field(default=ListingStatus.NEU, index=True)
    first_seen_at: datetime = Field(default_factory=utcnow, index=True)
    last_seen_at: datetime = Field(default_factory=utcnow)
    notified_at: datetime | None = None

    def rent_display(self) -> str:
        """Miete für die Tabelle aufbereiten (warm bevorzugt, kalt als Fallback)."""
        if self.rent_warm:
            return f"{self.rent_warm:.0f} € warm"
        if self.rent_cold:
            return f"{self.rent_cold:.0f} € kalt"
        return "k. A."

    def effective_rent(self) -> float | None:
        """Für Filter/Scoring verwendete Miete.

        Fehlt die Warmmiete, wird die Kaltmiete mit einem pauschalen Aufschlag
        von 20 % geschätzt — sonst würden Angebote ohne Warmmiete jeden
        Budget-Filter mühelos unterlaufen und das Ranking verzerren.
        """
        if self.rent_warm:
            return self.rent_warm
        if self.rent_cold:
            return self.rent_cold * 1.2
        return None


class GeoCache(SQLModel, table=True):
    """Cache für teure externe Geo-Abfragen.

    Adressen von Angeboten ändern sich nicht — jede Nominatim-, OSRM-, Overpass-
    oder HAFAS-Antwort wird daher unter einem Schlüssel abgelegt und
    wiederverwendet. Das schont fremde Server und macht Re-Runs schnell.
    """

    __tablename__ = "geo_cache"

    id: int | None = Field(default=None, primary_key=True)
    # kind = "geocode" | "route" | "poi" | "transit"
    kind: str = Field(index=True)
    cache_key: str = Field(index=True, unique=True)
    payload_json: str
    created_at: datetime = Field(default_factory=utcnow)


class AppState(SQLModel, table=True):
    """Einfacher Key-Value-Speicher für App-Zustand.

    Genutzt u.a. für ``last_visit_at`` (Badge "neue Angebote seit letztem Besuch")
    und den Zeitpunkt des letzten erfolgreichen Sammel-Laufs.
    """

    __tablename__ = "app_state"

    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=utcnow)
