"""Tests für die gefilterte Angebotsliste (build_query/fetch_listings)."""

from __future__ import annotations

from sqlmodel import Session

from app.models import Listing, ListingStatus, ListingType, make_fingerprint
from app.queries import ListingFilters, fetch_listings


def _listing(url: str, **overrides) -> Listing:
    """Testangebot mit sinnvollen Defaults bauen."""
    data = {
        "source": "wg_gesucht",
        "url": url,
        "title": "Testangebot",
        "listing_type": ListingType.WG_ROOM,
        "rent_warm": 650.0,
        "size_sqm": 18.0,
        "district": "Schwabing",
    }
    data.update(overrides)
    data.setdefault("fingerprint", make_fingerprint(data["source"], data["url"], None))
    return Listing(**data)


def test_favorit_ueberlebt_alle_suchfilter(engine) -> None:  # noqa: ANN001
    """Ein Favorit erscheint auch, wenn er Miete/Größe/Fahrzeit/Stadtteil reißt."""
    with Session(engine) as session:
        session.add(
            _listing(
                "https://example.com/fav",
                status=ListingStatus.FAVORISIERT,
                rent_warm=1500.0,
                size_sqm=10.0,
                district="Pasing",
                transit_minutes=90.0,
            )
        )
        session.add(_listing("https://example.com/teuer", rent_warm=1500.0))
        session.commit()

        filters = ListingFilters(
            max_rent=900,
            min_size=15,
            districts=["Schwabing"],
            max_transit_minutes=40,
        )
        urls = [listing.url for listing in fetch_listings(session, filters)]

    assert "https://example.com/fav" in urls
    # Nicht-Favoriten werden weiterhin normal gefiltert.
    assert "https://example.com/teuer" not in urls


def test_ausgeblendet_bleibt_ausgeblendet(engine) -> None:  # noqa: ANN001
    with Session(engine) as session:
        session.add(_listing("https://example.com/weg", status=ListingStatus.AUSGEBLENDET))
        session.commit()
        assert fetch_listings(session, ListingFilters()) == []
