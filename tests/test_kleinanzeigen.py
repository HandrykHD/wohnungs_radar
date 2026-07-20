"""Tests für den Kleinanzeigen-Parser (gegen synthetische Fixture)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import ListingType
from app.sources.kleinanzeigen import (
    KleinanzeigenSource,
    _parse_location,
    _parse_price,
    _parse_rooms,
    _parse_size,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kleinanzeigen_search.html"


@pytest.fixture
def parsed(config):  # noqa: ANN001
    html = FIXTURE.read_text(encoding="utf-8")
    listings = KleinanzeigenSource(config)._parse_page(html)
    return {listing.external_id: listing for listing in listings}


# --- Einzelparser -----------------------------------------------------------


def test_parse_price_nimmt_erste_zahl() -> None:
    """Bei 'VB' mit durchgestrichenem Zweitpreis zählt die erste Zahl."""
    assert _parse_price("1.450 € VB 1.600 €") == 1450.0
    assert _parse_price("980 €") == 980.0


def test_parse_price_leer() -> None:
    assert _parse_price(None) is None
    assert _parse_price("VB") is None


def test_parse_size_aus_titel() -> None:
    assert _parse_size("Möblierte Wohnung 45 m² zur Zwischenmiete") == 45.0
    assert _parse_size("Wohnung ohne Größenangabe") is None


def test_parse_rooms_aus_titel() -> None:
    assert _parse_rooms("Schöne 2-Zimmer-Wohnung") == 2.0
    assert _parse_rooms("Helle 3 Zi Wohnung") == 3.0
    assert _parse_rooms("Wohnung ohne Zimmerangabe") is None


def test_parse_location_plz_und_stadtteil() -> None:
    plz, district = _parse_location("80637 Neuhausen")
    assert plz == "80637"
    assert district == "Neuhausen"


def test_parse_location_entfernt_zero_width_space() -> None:
    """Kleinanzeigen setzt Zero-Width-Spaces in Bindestrich-Stadtteile."""
    plz, district = _parse_location("81543 Untergiesing-​Harlaching")
    assert plz == "81543"
    assert district == "Untergiesing-Harlaching"


# --- Vollständiges Parsing gegen die Fixture --------------------------------


def test_gesuch_wird_uebersprungen(parsed) -> None:  # noqa: ANN001
    """'sucht Wohnung' ist ein Gesuch und darf nicht als Angebot erscheinen."""
    assert "2004" not in parsed


def test_nur_echte_angebote(parsed) -> None:  # noqa: ANN001
    assert set(parsed) == {"2001", "2002", "2003", "2005"}


def test_normalfall(parsed) -> None:  # noqa: ANN001
    listing = parsed["2001"]
    assert listing.rent_warm == 1150.0
    assert listing.rooms == 2.0
    assert listing.district == "Neuhausen"
    assert listing.address == "80637 Neuhausen, München"
    assert listing.listing_type is ListingType.APARTMENT
    assert (
        listing.url == "https://www.kleinanzeigen.de/s-anzeige/schoene-2-zimmer-wohnung/2001-203-1"
    )
    assert listing.source == "kleinanzeigen"


def test_vb_preis_und_flaeche(parsed) -> None:  # noqa: ANN001
    listing = parsed["2002"]
    assert listing.rent_warm == 1450.0
    assert listing.size_sqm == 45.0
    assert listing.district == "Untergiesing-Harlaching"


def test_wg_zimmer_erkannt(parsed) -> None:  # noqa: ANN001
    listing = parsed["2003"]
    assert listing.listing_type is ListingType.WG_ROOM
    assert listing.rent_warm == 720.0


def test_ohne_groesse_und_zimmer(parsed) -> None:  # noqa: ANN001
    listing = parsed["2005"]
    assert listing.size_sqm is None
    assert listing.rooms is None
    assert listing.rent_warm == 980.0


def test_url_schema(config) -> None:  # noqa: ANN001
    source = KleinanzeigenSource(config)
    assert source._search_url(1).endswith("/s-wohnung-mieten/anzeige:angebote/muenchen/c203l6411")
    assert "seite:2" in source._search_url(2)
