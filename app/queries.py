"""Aufbau der Angebots-Abfragen (Filter + Sortierung).

Bewusst von den Routen getrennt: dieselbe Filterlogik wird von der HTML-Ansicht,
der JSON-API und (ab M3) der Benachrichtigungsprüfung benutzt. Wären die Filter
in der Route, würde die Benachrichtigung andere Treffer liefern als die Tabelle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_
from sqlmodel import Session, col, select

from app.config import Config
from app.models import Listing, ListingStatus

#: Erlaubte Sortierschlüssel → (Spalte, absteigend?)
SORT_OPTIONS: dict[str, tuple[Any, bool]] = {
    "score": (col(Listing.score), True),
    "rent": (col(Listing.rent_warm), False),
    "size": (col(Listing.size_sqm), True),
    "newest": (col(Listing.first_seen_at), True),
    "transit": (col(Listing.transit_minutes), False),
}

DEFAULT_SORT = "newest"


@dataclass(slots=True)
class ListingFilters:
    """Vom Nutzer gesetzte Filter aus der Query-String der Anfrage."""

    max_rent: float | None = None
    min_size: float | None = None
    min_rooms: float | None = None
    districts: list[str] | None = None
    max_transit_minutes: int | None = None
    listing_type: str | None = None
    source: str | None = None
    include_hidden: bool = False
    only_favorites: bool = False
    sort: str = DEFAULT_SORT

    @classmethod
    def from_config_defaults(cls, config: Config) -> ListingFilters:
        """Startfilter aus der ``config.yaml`` ableiten.

        Als Mietobergrenze wird das *großzügigere* der beiden Typ-Limits gewählt,
        damit die Voreinstellung keine Wohnungen wegfiltert, obwohl der Nutzer
        laut Config beide Typen sucht. Die typgenaue Prüfung passiert in
        :func:`matches_config_criteria`.
        """
        search = config.search
        return cls(
            max_rent=max(search.wg_room.max_warm_rent, search.apartment.max_warm_rent),
            min_size=min(search.wg_room.min_size_sqm, search.apartment.min_size_sqm),
            districts=list(search.districts) or None,
            max_transit_minutes=search.max_transit_minutes_to_tum,
        )


def build_query(filters: ListingFilters):
    """SELECT-Statement für die gefilterte, sortierte Angebotsliste bauen."""
    statement = select(Listing)

    if not filters.include_hidden:
        statement = statement.where(Listing.status != ListingStatus.AUSGEBLENDET)

    if filters.only_favorites:
        statement = statement.where(Listing.status == ListingStatus.FAVORISIERT)

    # Kriterien (Miete, Größe, Fahrzeit, Stadtteil) werden gesammelt und am Ende
    # mit "ODER Favorit" verknüpft: ein bewusst markierter Favorit soll nie
    # durch die Suchfilter aus Tabelle oder Karte verschwinden.
    criteria = []

    if filters.max_rent is not None:
        # Angebote ohne Mietangabe werden NICHT weggefiltert — lieber ein
        # unvollständiges Angebot zeigen als einen Treffer verpassen.
        criteria.append(
            or_(col(Listing.rent_warm).is_(None), col(Listing.rent_warm) <= filters.max_rent)
        )

    if filters.min_size is not None:
        criteria.append(
            or_(col(Listing.size_sqm).is_(None), col(Listing.size_sqm) >= filters.min_size)
        )

    if filters.min_rooms is not None:
        criteria.append(
            or_(col(Listing.rooms).is_(None), col(Listing.rooms) >= filters.min_rooms)
        )

    if filters.max_transit_minutes is not None:
        # Noch nicht angereicherte Angebote (transit_minutes = NULL) bleiben
        # sichtbar, sonst wäre die Tabelle bis zum ersten Enrichment-Lauf leer.
        criteria.append(
            or_(
                col(Listing.transit_minutes).is_(None),
                col(Listing.transit_minutes) <= filters.max_transit_minutes,
            )
        )

    if filters.districts:
        district_clauses = [col(Listing.district).ilike(f"%{name}%") for name in filters.districts]
        criteria.append(or_(col(Listing.district).is_(None), or_(*district_clauses)))

    if criteria:
        statement = statement.where(
            or_(Listing.status == ListingStatus.FAVORISIERT, and_(*criteria))
        )

    if filters.listing_type:
        statement = statement.where(Listing.listing_type == filters.listing_type)

    if filters.source:
        statement = statement.where(Listing.source == filters.source)

    column, descending = SORT_OPTIONS.get(filters.sort, SORT_OPTIONS[DEFAULT_SORT])
    # NULL-Werte ans Ende: ein Angebot ohne Score gehört nicht an die Spitze.
    statement = statement.order_by(
        column.desc().nulls_last() if descending else column.asc().nulls_last()
    )

    return statement


def fetch_listings(session: Session, filters: ListingFilters, limit: int = 300) -> list[Listing]:
    """Gefilterte Angebote laden."""
    return list(session.exec(build_query(filters).limit(limit)).all())


def matches_config_criteria(listing: Listing, config: Config) -> bool:
    """Prüfen, ob ein Angebot die *typgenauen* Suchkriterien erfüllt.

    Grundlage für Benachrichtigungen (M3): hier wird das Limit des jeweiligen
    Objekttyps angewandt, nicht das großzügigere Sammel-Limit der Tabelle.
    """
    search = config.search

    if listing.listing_type not in search.listing_types:
        return False

    limits = search.limits_for(listing.listing_type)

    rent = listing.effective_rent()
    if rent is not None and rent > limits.max_warm_rent:
        return False

    if listing.size_sqm is not None and listing.size_sqm < limits.min_size_sqm:
        return False

    if (
        listing.transit_minutes is not None
        and listing.transit_minutes > search.max_transit_minutes_to_tum
    ):
        return False

    if search.districts and listing.district:
        haystack = listing.district.casefold()
        if not any(name.casefold() in haystack for name in search.districts):
            return False

    return True


