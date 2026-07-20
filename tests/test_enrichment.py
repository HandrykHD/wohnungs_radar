"""Tests für Distanzberechnung, Fahrzeit-Schätzung und Scoring."""

from __future__ import annotations

import pytest

from app.enrich.routing import estimate_minutes, haversine_km
from app.enrich.scoring import (
    compute_score,
    distance_score,
    rent_score,
    size_score,
    surroundings_score,
)
from app.enrich.transit import _parse_connection
from app.models import Listing, ListingType, make_fingerprint

# TUM Garching (Boltzmannstr. 3) und Türkenstraße 58 als reale Bezugspunkte.
TUM = (48.262541, 11.668114)
TUERKENSTR = (48.1497198, 11.5757258)


# --- Haversine --------------------------------------------------------------


def test_haversine_gleicher_punkt_ist_null() -> None:
    assert haversine_km(*TUM, *TUM) == pytest.approx(0.0, abs=1e-9)


def test_haversine_symmetrisch() -> None:
    hin = haversine_km(*TUM, *TUERKENSTR)
    zurueck = haversine_km(*TUERKENSTR, *TUM)
    assert hin == pytest.approx(zurueck, abs=1e-9)


def test_haversine_bekannte_distanz() -> None:
    """Türkenstraße → TUM Garching ist ~14,3 km Luftlinie (Straßenweg ~16,7 km)."""
    km = haversine_km(*TUERKENSTR, *TUM)
    assert 14.0 < km < 14.6


def test_haversine_ein_breitengrad_ist_rund_111km() -> None:
    assert haversine_km(48.0, 11.0, 49.0, 11.0) == pytest.approx(111.2, abs=0.5)


# --- Fahrzeit-Schätzung -----------------------------------------------------


def test_estimate_minutes_grundfall() -> None:
    """5 km bei 15 km/h ohne Umweg = 20 Minuten."""
    assert estimate_minutes(5.0, 15.0, 1.0) == pytest.approx(20.0)


def test_estimate_minutes_umwegfaktor_erhoeht_zeit() -> None:
    ohne = estimate_minutes(5.0, 15.0, 1.0)
    mit = estimate_minutes(5.0, 15.0, 1.35)
    assert mit == pytest.approx(ohne * 1.35)


def test_estimate_minutes_null_geschwindigkeit() -> None:
    assert estimate_minutes(5.0, 0.0, 1.35) == 0.0


# --- Score-Komponenten ------------------------------------------------------


def test_distance_score_grenzen() -> None:
    assert distance_score(0.0, 40.0) == 1.0
    assert distance_score(40.0, 40.0) == 0.0
    assert distance_score(20.0, 40.0) == pytest.approx(0.5)


def test_distance_score_ueber_maximum_ist_null() -> None:
    assert distance_score(60.0, 40.0) == 0.0


def test_distance_score_ohne_daten() -> None:
    assert distance_score(None, 40.0) is None


def test_rent_score_guenstiger_ist_besser() -> None:
    assert rent_score(0.0, 900.0) == 1.0
    assert rent_score(900.0, 900.0) == 0.0
    assert rent_score(450.0, 900.0) == pytest.approx(0.5)


def test_rent_score_ueber_budget_ist_null() -> None:
    assert rent_score(1200.0, 900.0) == 0.0


def test_size_score_mindestgroesse_ist_halb() -> None:
    assert size_score(15.0, 15.0) == pytest.approx(0.5)
    assert size_score(30.0, 15.0) == 1.0


def test_size_score_unter_minimum_faellt_ab() -> None:
    assert size_score(7.5, 15.0) == pytest.approx(0.25)


def test_surroundings_score_naeher_ist_besser(config) -> None:  # noqa: ANN001
    radius = config.geo.poi_search_radius_m
    near = _listing(supermarket_m=0.0, pharmacy_m=0.0, transit_stop_m=0.0)
    far = _listing(supermarket_m=radius, pharmacy_m=radius, transit_stop_m=radius)
    assert surroundings_score(near, radius) == 1.0
    assert surroundings_score(far, radius) == 0.0


def test_surroundings_score_nur_vorhandene_zaehlen(config) -> None:  # noqa: ANN001
    radius = config.geo.poi_search_radius_m
    listing = _listing(supermarket_m=0.0, pharmacy_m=None, transit_stop_m=None)
    # Nur der Supermarkt zählt → voller Wert trotz fehlender anderer POIs.
    assert surroundings_score(listing, radius) == 1.0


def test_surroundings_score_ohne_pois_ist_none(config) -> None:  # noqa: ANN001
    assert surroundings_score(_listing(), config.geo.poi_search_radius_m) is None


# --- Gesamtscore ------------------------------------------------------------


def _listing(**overrides) -> Listing:
    data = {
        "source": "test",
        "url": "https://x.de/1",
        "external_id": "1",
        "title": "Test",
        "listing_type": ListingType.WG_ROOM,
    }
    data.update(overrides)
    data.setdefault("fingerprint", make_fingerprint("test", data["url"], data["external_id"]))
    return Listing(**data)


def test_compute_score_ohne_daten_ist_none(config) -> None:  # noqa: ANN001
    assert compute_score(_listing(), config) is None


def test_compute_score_perfektes_angebot_nahe_100(config) -> None:  # noqa: ANN001
    """Sehr günstig, groß, uni-nah, beste Umgebung → Score nahe 100."""
    listing = _listing(
        rent_warm=1.0,
        size_sqm=100.0,
        transit_minutes=0.0,
        supermarket_m=0.0,
        pharmacy_m=0.0,
        transit_stop_m=0.0,
    )
    score = compute_score(listing, config)
    assert score is not None
    assert score > 95.0


def test_compute_score_schlechtes_angebot_nahe_null(config) -> None:  # noqa: ANN001
    limits = config.search.wg_room
    listing = _listing(
        rent_warm=limits.max_warm_rent,
        size_sqm=0.1,
        transit_minutes=config.search.max_transit_minutes_to_tum,
        supermarket_m=config.geo.poi_search_radius_m,
        pharmacy_m=config.geo.poi_search_radius_m,
        transit_stop_m=config.geo.poi_search_radius_m,
    )
    score = compute_score(listing, config)
    assert score is not None
    assert score < 5.0


def test_compute_score_teildaten_normalisiert(config) -> None:  # noqa: ANN001
    """Mit nur Miete+Größe (kein Geo) soll trotzdem ein sinnvoller Score entstehen."""
    listing = _listing(rent_warm=450.0, size_sqm=30.0)  # beide "gut"
    score = compute_score(listing, config)
    assert score is not None
    # rent_score=0.5, size_score=1.0 → gewichtet & normalisiert über zwei
    # Komponenten liegt der Score klar im oberen Bereich.
    assert 50.0 < score <= 100.0


def test_compute_score_nutzt_typspezifische_limits(config) -> None:  # noqa: ANN001
    """1200€ ist fürs WG-Zimmer über Budget (rent_score 0), für die Wohnung ok.

    Die Größen sind so gewählt, dass beide den vollen size_score erreichen
    (jeweils ≥ doppelte Typ-Mindestgröße) — so isoliert der Vergleich die Miete.
    """
    wg = _listing(listing_type=ListingType.WG_ROOM, rent_warm=1200.0, size_sqm=30.0)
    apart = _listing(listing_type=ListingType.APARTMENT, rent_warm=1200.0, size_sqm=50.0)
    assert compute_score(apart, config) > compute_score(wg, config)


# --- MVG-Verbindungsparser --------------------------------------------------


def test_parse_connection_dauer_und_umstiege() -> None:
    """Ankunft = plannedDeparture des letzten to-Knotens; Umstiege = Fahrten-1."""
    conn = {
        "parts": [
            {
                "from": {"plannedDeparture": "2026-07-20T23:09:00+02:00"},
                "to": {"plannedDeparture": "2026-07-20T23:20:00+02:00"},
                "line": {"label": "Fussweg", "transportType": "PEDESTRIAN"},
            },
            {
                "from": {"plannedDeparture": "2026-07-20T23:20:00+02:00"},
                "to": {"plannedDeparture": "2026-07-20T23:42:00+02:00"},
                "line": {"label": "U6", "transportType": "UBAHN"},
            },
        ]
    }
    result = _parse_connection(conn)
    assert result is not None
    assert result.minutes == pytest.approx(33.0)
    assert result.changes == 0
    assert result.summary == "U6"


def test_parse_connection_mit_umstieg() -> None:
    conn = {
        "parts": [
            {
                "from": {"plannedDeparture": "2026-07-20T10:00:00+02:00"},
                "to": {"plannedDeparture": "2026-07-20T10:10:00+02:00"},
                "line": {"label": "Tram 27", "transportType": "TRAM"},
            },
            {
                "from": {"plannedDeparture": "2026-07-20T10:12:00+02:00"},
                "to": {"plannedDeparture": "2026-07-20T10:30:00+02:00"},
                "line": {"label": "U6", "transportType": "UBAHN"},
            },
        ]
    }
    result = _parse_connection(conn)
    assert result is not None
    assert result.minutes == pytest.approx(30.0)
    assert result.changes == 1
    assert result.summary == "Tram 27 → U6"


def test_parse_connection_leer_ist_none() -> None:
    assert _parse_connection({"parts": []}) is None
