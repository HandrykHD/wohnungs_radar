"""Tests für Fingerprinting und den Dedup-Mechanismus."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import select

from app.db import upsert_listing
from app.models import (
    Listing,
    ListingStatus,
    ListingType,
    make_fingerprint,
    normalize_district,
    utcnow,
)


def _listing(**overrides) -> Listing:
    """Testangebot mit sinnvollen Defaults bauen."""
    data = {
        "source": "wg_gesucht",
        "url": "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.12345.html",
        "external_id": "12345",
        "title": "WG-Zimmer in Schwabing",
        "listing_type": ListingType.WG_ROOM,
        "rent_warm": 650.0,
        "size_sqm": 18.0,
        "district": "Schwabing",
    }
    data.update(overrides)
    data.setdefault(
        "fingerprint", make_fingerprint(data["source"], data["url"], data.get("external_id"))
    )
    return Listing(**data)


# --- Fingerprint ------------------------------------------------------------


def test_fingerprint_ignoriert_tracking_parameter() -> None:
    """Query-Parameter dürfen nicht zu einem neuen Angebot führen."""
    plain = make_fingerprint("kleinanzeigen", "https://x.de/anzeige/99")
    tracked = make_fingerprint("kleinanzeigen", "https://x.de/anzeige/99?utm_source=mail&sid=abc")
    assert plain == tracked


def test_fingerprint_ignoriert_trailing_slash_und_grossschreibung() -> None:
    assert make_fingerprint("s", "https://X.de/Angebot/1/") == make_fingerprint(
        "s", "https://x.de/angebot/1"
    )


def test_fingerprint_bevorzugt_externe_id_vor_url() -> None:
    """Dieselbe ID unter geändertem Titel-Slug bleibt dasselbe Angebot."""
    first = make_fingerprint("wg_gesucht", "https://x.de/wg-zimmer-schwabing.777.html", "777")
    second = make_fingerprint("wg_gesucht", "https://x.de/wg-zimmer-muenchen-neu.777.html", "777")
    assert first == second


def test_fingerprint_trennt_verschiedene_quellen() -> None:
    """Dieselbe ID bei zwei Portalen sind zwei verschiedene Angebote."""
    assert make_fingerprint("a", "https://x.de/1", "1") != make_fingerprint(
        "b", "https://x.de/1", "1"
    )


def test_fingerprint_trennt_verschiedene_angebote() -> None:
    assert make_fingerprint("s", "https://x.de/1", "1") != make_fingerprint(
        "s", "https://x.de/2", "2"
    )


# --- Upsert / Dedup ---------------------------------------------------------


def test_erstes_einfuegen_meldet_neu(session) -> None:  # noqa: ANN001
    listing, is_new = upsert_listing(session, _listing())
    session.commit()

    assert is_new is True
    assert listing.id is not None
    assert listing.status is ListingStatus.NEU


def test_zweites_einfuegen_dupliziert_nicht(session) -> None:  # noqa: ANN001
    upsert_listing(session, _listing())
    session.commit()

    _, is_new = upsert_listing(session, _listing())
    session.commit()

    assert is_new is False
    assert len(session.exec(select(Listing)).all()) == 1


def test_wiederfund_ueberschreibt_nutzerstatus_nicht(session) -> None:  # noqa: ANN001
    """Kern der Anforderung: ein erneuter Fund darf Favorit/Gesehen nicht zurücksetzen."""
    stored, _ = upsert_listing(session, _listing())
    stored.status = ListingStatus.FAVORISIERT
    session.commit()

    upsert_listing(session, _listing())
    session.commit()

    assert session.exec(select(Listing)).one().status is ListingStatus.FAVORISIERT


def test_wiederfund_behaelt_first_seen_und_notified(session) -> None:  # noqa: ANN001
    """Sonst gäbe es bei jedem Lauf eine erneute Benachrichtigung."""
    original_time = utcnow() - timedelta(days=3)
    stored, _ = upsert_listing(session, _listing(first_seen_at=original_time))
    stored.notified_at = original_time
    session.commit()

    upsert_listing(session, _listing())
    session.commit()

    refreshed = session.exec(select(Listing)).one()
    assert refreshed.first_seen_at == original_time
    assert refreshed.notified_at == original_time
    assert refreshed.last_seen_at > original_time


def test_wiederfund_ergaenzt_fehlende_felder(session) -> None:  # noqa: ANN001
    """Listenansicht liefert weniger Felder als die Detailseite — die sollen nachwachsen."""
    upsert_listing(session, _listing(address=None, rooms=None))
    session.commit()

    upsert_listing(session, _listing(address="Hohenzollernstraße 1", rooms=1.0))
    session.commit()

    refreshed = session.exec(select(Listing)).one()
    assert refreshed.address == "Hohenzollernstraße 1"
    assert refreshed.rooms == 1.0


def test_wiederfund_ueberschreibt_vorhandene_enrichment_daten_nicht(
    session,
) -> None:  # noqa: ANN001
    """Geocoding ist teuer — einmal berechnet, bleibt es stehen."""
    stored, _ = upsert_listing(session, _listing())
    stored.lat, stored.lon, stored.score = 48.15, 11.58, 87.5
    session.commit()

    upsert_listing(session, _listing())
    session.commit()

    refreshed = session.exec(select(Listing)).one()
    assert (refreshed.lat, refreshed.lon, refreshed.score) == (48.15, 11.58, 87.5)


def test_verschiedene_angebote_werden_beide_gespeichert(session) -> None:  # noqa: ANN001
    upsert_listing(session, _listing(url="https://x.de/1", external_id="1"))
    upsert_listing(session, _listing(url="https://x.de/2", external_id="2"))
    session.commit()

    assert len(session.exec(select(Listing)).all()) == 2


# --- Hilfsfunktionen --------------------------------------------------------


def test_effective_rent_schaetzt_warmmiete_aus_kaltmiete() -> None:
    """Ohne Schätzung würden Angebote ohne Warmmiete jeden Budgetfilter unterlaufen."""
    assert _listing(rent_warm=None, rent_cold=500.0).effective_rent() == 600.0


def test_effective_rent_bevorzugt_warmmiete() -> None:
    assert _listing(rent_warm=700.0, rent_cold=500.0).effective_rent() == 700.0


def test_effective_rent_ohne_angabe_ist_none() -> None:
    assert _listing(rent_warm=None, rent_cold=None).effective_rent() is None


def test_normalize_district_entfernt_plz_und_praefix() -> None:
    assert normalize_district("München Schwabing-West") == "Schwabing-West"
    assert normalize_district("85748 Garching bei München") == "Garching"
    assert normalize_district(None) is None


def test_normalize_district_nur_muenchen_wird_none() -> None:
    """Ohne echten Stadtteil (nur die Stadt) soll None herauskommen."""
    assert normalize_district("München") is None
    # WG-Gesucht-Quirk: Stadtteil = Stadt wird doppelt geliefert.
    assert normalize_district("München München") is None
