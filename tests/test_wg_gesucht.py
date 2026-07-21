"""Tests für den WG-Gesucht-Parser.

Getestet wird gegen eine synthetische Fixture, deren DOM-Struktur exakt dem
verifizierten echten Markup entspricht (siehe Kommentar in der Fixture). So
laufen die Tests offline, deterministisch und ohne echte Personendaten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import ListingType
from app.sources.wg_gesucht import (
    _CATEGORIES,
    WgGesuchtSource,
    _build_address,
    _parse_price,
    _parse_size,
    _split_location,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wg_gesucht_search.html"


@pytest.fixture
def parsed(config):  # noqa: ANN001
    """Alle Karten der Fixture als WG-Zimmer geparst, indexiert nach external_id."""
    html = FIXTURE.read_text(encoding="utf-8")
    source = WgGesuchtSource(config)
    listings = source._parse_page(html, _CATEGORIES["wg_room"][0])
    return {listing.external_id: listing for listing in listings}


# --- Preis-Parser -----------------------------------------------------------


def test_parse_price_einfach() -> None:
    assert _parse_price("650 €") == 650.0


def test_parse_price_mit_tausendertrennzeichen() -> None:
    """Deutsches Format: der Punkt ist Tausender, nicht Dezimal."""
    assert _parse_price("1.100 €") == 1100.0


def test_parse_price_leer_oder_none() -> None:
    assert _parse_price(None) is None
    assert _parse_price("VB") is None


# --- Größen-Parser ----------------------------------------------------------


def test_parse_size_ganzzahl() -> None:
    assert _parse_size("18 m²") == 18.0


def test_parse_size_nachkomma_deutsch() -> None:
    assert _parse_size("8,5 m²") == 8.5


def test_parse_size_none() -> None:
    assert _parse_size(None) is None


# --- Standort-Zerlegung -----------------------------------------------------


def test_split_location_normalfall() -> None:
    district, street = _split_location("3er WG | München Schwabing | Hohenzollernstraße 12")
    assert district == "Schwabing"
    assert street == "Hohenzollernstraße 12"


def test_split_location_ohne_stadtteil() -> None:
    district, street = _split_location("5er WG | München | Lützelsteiner Straße")
    assert district is None
    assert street == "Lützelsteiner Straße"


def test_split_location_leer() -> None:
    assert _split_location(None) == (None, None)


def test_build_address_vollstaendige_strasse() -> None:
    assert _build_address("Hohenzollernstraße 12", "Schwabing") == "Hohenzollernstraße 12, München"


def test_build_address_abgeschnittene_strasse_faellt_auf_stadtteil_zurueck() -> None:
    # Listenseite kürzt lange Namen mit "…"/"..." → unbrauchbar für Geocoding.
    assert _build_address("Johann-Sebastian-Bach-Str…", "Neuhausen-Nymphenburg") == (
        "Neuhausen-Nymphenburg, München"
    )
    assert _build_address("Colmarer Str. 3 , 81379 M...", "Obersendling") == (
        "Obersendling, München"
    )


def test_build_address_ohne_alles() -> None:
    assert _build_address(None, None) is None


# --- Vollständiges Karten-Parsing gegen die Fixture -------------------------


def test_parst_alle_karten(parsed) -> None:  # noqa: ANN001
    assert set(parsed) == {"1001", "1002", "1003", "1004", "1005"}


def test_normalfall_vollstaendig(parsed) -> None:  # noqa: ANN001
    listing = parsed["1001"]
    assert listing.title == "Helles Zimmer in netter WG"
    assert listing.rent_warm == 650.0
    assert listing.size_sqm == 18.0
    assert listing.district == "Schwabing"
    assert listing.address == "Hohenzollernstraße 12, München"
    assert listing.available_from == "01.10.2026"
    assert listing.listing_type is ListingType.WG_ROOM
    assert listing.rooms == 1.0
    assert listing.url == "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen-Schwabing.1001.html"
    assert listing.source == "wg_gesucht"


def test_tausender_und_nachkomma(parsed) -> None:  # noqa: ANN001
    listing = parsed["1002"]
    assert listing.rent_warm == 1100.0
    assert listing.size_sqm == 8.5


def test_zeitraum_und_muenchen_muenchen(parsed) -> None:  # noqa: ANN001
    """Zeitraum bleibt als Rohtext erhalten; 'München München' → kein Stadtteil."""
    listing = parsed["1003"]
    assert listing.available_from == "01.08.2026 - 30.09.2026"
    assert listing.district is None


def test_karte_ohne_stadtteil(parsed) -> None:  # noqa: ANN001
    listing = parsed["1004"]
    assert listing.district is None
    assert listing.address == "Lützelsteiner Straße, München"


def test_premium_karte_mit_querystring_url(parsed) -> None:  # noqa: ANN001
    """Auch die Premium-Karte mit Query-String muss sauber erfasst werden."""
    listing = parsed["1005"]
    assert listing.external_id == "1005"
    assert "asset_id=1005" in listing.url
    assert listing.rent_warm == 820.0


def test_apartment_kategorie_setzt_keine_zimmerzahl(config) -> None:  # noqa: ANN001
    """Bei Wohnungen ist die Zimmerzahl in der Liste nicht verlässlich → None."""
    html = FIXTURE.read_text(encoding="utf-8")
    source = WgGesuchtSource(config)
    listings = source._parse_page(html, _CATEGORIES["apartment"][1])
    assert all(listing.listing_type is ListingType.APARTMENT for listing in listings)
    assert all(listing.rooms is None for listing in listings)


def test_url_schema(config) -> None:  # noqa: ANN001
    """Such-URLs folgen dem verifizierten Muster stadt.kategorie.typ.seite."""
    source = WgGesuchtSource(config)
    wg = _CATEGORIES["wg_room"][0]
    assert (
        source._search_url(wg, 0) == "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.90.0.1.0.html"
    )
    assert (
        source._search_url(wg, 2) == "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.90.0.1.2.html"
    )
