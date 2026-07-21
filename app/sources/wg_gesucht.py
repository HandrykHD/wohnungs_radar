"""Adapter für WG-Gesucht (https://www.wg-gesucht.de).

Liest ausschließlich die **öffentlichen HTML-Suchergebnisseiten** — nicht die per
``robots.txt`` gesperrte interne ``/api/``-Schnittstelle. Siehe DATA_SOURCES.md
für die rechtlichen Rahmenbedingungen und die selbst gesetzten Rate-Limits.

Die Seiten sind server-gerendert; ein Browser (Playwright) ist nicht nötig,
``httpx`` + ``selectolax`` genügen.

URL-Schema der Suche (am 20.07.2026 verifiziert)::

    https://www.wg-gesucht.de/<slug>-in-Muenchen.<stadt>.<kategorie>.<typ>.<seite>.html
                                                  90        0..3       1       0..N

* ``stadt``     90 = München (dieses Projekt ist auf München beschränkt),
* ``kategorie`` 0 = WG-Zimmer, 1 = 1-Zimmer-Wohnung, 2 = Wohnung, 3 = Haus,
* ``typ``       1 = zu vermieten,
* ``seite``     0-indexiert.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from app.models import Listing, ListingType, make_fingerprint, normalize_district, utcnow
from app.sources.base import PoliteClient, SourceAdapter, SourceUnavailable

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.wg-gesucht.de"
_MUNICH_CITY_ID = 90


@dataclass(frozen=True)
class _Category:
    """Eine WG-Gesucht-Suchkategorie und ihre Zuordnung zu unserem Objekttyp."""

    slug: str
    category_id: int
    listing_type: ListingType


# Zuordnung unserer config-``listing_types`` zu den WG-Gesucht-Kategorien.
# "apartment" umfasst bei WG-Gesucht zwei Kategorien (1-Zimmer + Wohnung).
_CATEGORIES: dict[str, tuple[_Category, ...]] = {
    "wg_room": (_Category("wg-zimmer", 0, ListingType.WG_ROOM),),
    "apartment": (
        _Category("1-zimmer-wohnungen", 1, ListingType.APARTMENT),
        _Category("wohnungen", 2, ListingType.APARTMENT),
    ),
}


def _clean(text: str | None) -> str | None:
    """Whitespace normalisieren; leere Strings zu ``None``."""
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


def _parse_price(raw: str | None) -> float | None:
    """Mietangabe wie ``"1.100 €"`` oder ``"895 €"`` in eine Zahl wandeln.

    Deutsches Format: ``.`` ist Tausendertrennzeichen. Cent-Angaben bei Mieten
    kommen praktisch nicht vor, daher werden nur die Ziffern vor einem etwaigen
    Komma ausgewertet.
    """
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw.split(",")[0])
    return float(digits) if digits else None


def _parse_size(raw: str | None) -> float | None:
    """``"13 m²"`` → ``13.0``. Auch ``"8,5 m²"`` wird korrekt gelesen."""
    if not raw:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    return float(match.group(1).replace(",", ".")) if match else None


def _parse_available_from(raw: str | None) -> str | None:
    """Verfügbarkeitsangabe säubern.

    WG-Gesucht zeigt entweder ein Datum (``"01.10.2026"``) oder einen Zeitraum
    (``"01.08.2026 - 30.09.2026"``, = befristete Zwischenmiete). Der Rohtext wird
    unverändert übernommen — der Zeitraum ist eine nützliche Information.
    """
    return _clean(raw)


def _split_location(raw: str | None) -> tuple[str | None, str | None]:
    """Die Standortzeile in (Stadtteil, Straße) zerlegen.

    Beispiele::

        "5er WG | München Sendling | Karwendelstraße 40"
        "München Lehel | Prinzregentenstraße"

    Vorgehen: Die Teile werden an ``|`` getrennt. Der Teil mit ``"München"``
    liefert den Stadtteil; der unmittelbar folgende Teil die Straße. So ist die
    Logik unabhängig davon, ob eine führende ``"Ner WG"``-Angabe existiert.
    """
    if not raw:
        return None, None

    parts = [p.strip() for p in raw.split("|") if p.strip()]
    district: str | None = None
    street: str | None = None

    for index, part in enumerate(parts):
        if "münchen" in part.casefold():
            district = normalize_district(part)
            if index + 1 < len(parts):
                street = parts[index + 1]
            break

    # Fallback: keine "München"-Zeile gefunden → letzten Teil als Straße nehmen.
    if district is None and parts:
        street = parts[-1]

    return district, street


def _build_address(street: str | None, district: str | None) -> str | None:
    """Geocodierbare Adresse aus Straße/Stadtteil bauen.

    Lange Straßennamen kürzt die WG-Gesucht-Listenseite mit ``…``/``...`` ab. So
    ein Torso (``"Johann-Sebastian-Bach-Str…"``) ist für Nominatim unbrauchbar →
    dann lieber den zuverlässig geparsten Stadtteil nehmen, das liefert
    wenigstens einen Viertel-Mittelpunkt für Score und TUM-Fahrzeit.
    """
    if street and ("…" in street or "..." in street):
        street = None
    location = street or district
    return f"{location}, München" if location else None


class WgGesuchtSource(SourceAdapter):
    """Sammelt WG-Zimmer und Wohnungen aus der WG-Gesucht-Münchensuche."""

    name = "wg_gesucht"
    label = "WG-Gesucht"

    def _search_url(self, category: _Category, page: int) -> str:
        """Such-URL für eine Kategorie und (0-indexierte) Seite bauen."""
        return (
            f"{_BASE_URL}/{category.slug}-in-Muenchen."
            f"{_MUNICH_CITY_ID}.{category.category_id}.1.{page}.html"
        )

    def _categories_to_fetch(self) -> list[_Category]:
        """Aus den aktivierten ``listing_types`` die WG-Gesucht-Kategorien ableiten."""
        categories: list[_Category] = []
        for listing_type in self.config.search.listing_types:
            categories.extend(_CATEGORIES.get(listing_type, ()))
        return categories

    async def fetch(self) -> list[Listing]:
        """Alle konfigurierten Kategorien seitenweise abrufen und parsen.

        Deduplizierung über den Fingerprint übernimmt später der Scheduler; hier
        wird nur innerhalb eines Laufs entdoppelt, damit dieselbe Premium-Anzeige
        (erscheint auf jeder Seite) nicht mehrfach verarbeitet wird.
        """
        max_pages = self.config.scraping.max_pages_per_run
        seen_ids: set[str] = set()
        listings: list[Listing] = []

        async with PoliteClient(self.config) as client:
            for category in self._categories_to_fetch():
                for page in range(max_pages):
                    url = self._search_url(category, page)
                    try:
                        response = await client.get(url)
                    except SourceUnavailable:
                        # Sperre/Netzfehler: diese Kategorie abbrechen, andere
                        # dürfen es weiter versuchen. run_adapter_safely fängt
                        # ein endgültiges Scheitern ohnehin ab.
                        raise

                    page_listings = self._parse_page(response.text, category)
                    if not page_listings:
                        # Leere Seite = Ende der Ergebnisse; weitere Seiten sparen.
                        logger.debug("%s Seite %d leer — Kategorie beendet", category.slug, page)
                        break

                    for listing in page_listings:
                        if listing.external_id in seen_ids:
                            continue
                        seen_ids.add(listing.external_id or listing.fingerprint)
                        listings.append(listing)

        return listings

    def _parse_page(self, html: str, category: _Category) -> list[Listing]:
        """Alle Angebotskarten einer Ergebnisseite parsen.

        Ein Fehler in einer einzelnen Karte darf die übrigen nicht verlieren —
        jede Karte wird daher isoliert verarbeitet.
        """
        tree = HTMLParser(html)
        cards = tree.css("div.offer_list_item")
        results: list[Listing] = []

        for card in cards:
            try:
                listing = self._parse_card(card, category)
            except Exception:
                logger.exception("Konnte eine WG-Gesucht-Karte nicht parsen — übersprungen")
                continue
            if listing is not None:
                results.append(listing)

        return results

    def _parse_card(self, card: Node, category: _Category) -> Listing | None:
        """Eine einzelne Angebotskarte in ein :class:`Listing` überführen."""
        external_id = card.attributes.get("data-id")
        link = card.css_first("h2.truncate_title a")
        if external_id is None or link is None:
            return None

        href = link.attributes.get("href")
        if not href:
            return None
        url = urljoin(_BASE_URL, href)
        title = _clean(link.text()) or "WG-Gesucht-Angebot"

        # Zeile mit Preis / Datum / Größe.
        middle = card.css_first("div.row.middle")
        rent = size = available_from = None
        if middle is not None:
            rent = _parse_price(_clean(_node_text(middle, "div.col-xs-3 b")))
            size = _parse_size(_clean(_node_text(middle, "div.col-xs-3.text-right b")))
            available_from = _parse_available_from(_node_text(middle, "div.col-xs-5"))

        district, street = _split_location(_node_text(card, "div.col-xs-11 span"))
        address = _build_address(street, district)

        return Listing(
            fingerprint=make_fingerprint(self.name, url, external_id),
            source=self.name,
            external_id=external_id,
            url=url,
            title=title,
            listing_type=category.listing_type,
            # Der in der Liste gezeigte Preis ist die Gesamtmiete (warm).
            rent_warm=rent,
            size_sqm=size,
            # Ein WG-Zimmer ist genau ein Zimmer; bei Wohnungen ist die
            # Zimmerzahl in der Liste nicht verlässlich ausgewiesen.
            rooms=1.0 if category.listing_type is ListingType.WG_ROOM else None,
            district=district,
            address=address,
            available_from=available_from,
            posted_at=None,  # Listenansicht nennt kein verlässliches Einstelldatum
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )


def _node_text(parent: Node, selector: str) -> str | None:
    """Text des ersten Treffers eines Selektors unterhalb ``parent`` (oder ``None``)."""
    node = parent.css_first(selector)
    return node.text() if node is not None else None
