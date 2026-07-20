"""Gewichtete Gesamtbewertung (0–100) eines Angebots.

Vier Komponenten, jeweils auf 0..1 normiert und mit den Gewichten aus
``config.scoring_weights`` kombiniert:

* **distance_to_tum** — kürzere ÖPNV-Fahrzeit zum Campus ist besser,
* **rent**            — günstiger (relativ zum Typ-Budget) ist besser,
* **size**            — größer (relativ zur Typ-Mindestgröße) ist besser,
* **surroundings**    — Supermarkt/Apotheke/Haltestelle in Gehweite ist besser.

Fehlt die Datengrundlage einer Komponente (z.B. noch keine ÖPNV-Auskunft), wird
sie **weggelassen** und das Gewicht auf die übrigen Komponenten umverteilt. So
wird ein Angebot weder für fehlende Fremddaten bestraft noch dafür belohnt.
"""

from __future__ import annotations

from app.config import Config
from app.models import Listing


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def distance_score(transit_minutes: float | None, max_minutes: float) -> float | None:
    """1.0 bei 0 min, linear fallend auf 0.0 bei ``max_minutes`` (und darüber)."""
    if transit_minutes is None or max_minutes <= 0:
        return None
    return _clamp(1.0 - transit_minutes / max_minutes)


def rent_score(rent: float | None, max_rent: float) -> float | None:
    """1.0 bei Miete 0, linear fallend auf 0.0 bei ``max_rent`` (und darüber).

    Bezugsgröße ist das Budget des jeweiligen Objekttyps.
    """
    if rent is None or max_rent <= 0:
        return None
    return _clamp(1.0 - rent / max_rent)


def size_score(size_sqm: float | None, min_size: float) -> float | None:
    """0.5 bei der Mindestgröße, 1.0 ab der doppelten Mindestgröße.

    Unterhalb der Mindestgröße fällt der Wert Richtung 0 — solche Angebote werden
    ohnehin meist herausgefiltert, sollen aber im Ranking klar zurückfallen.
    """
    if size_sqm is None or min_size <= 0:
        return None
    return _clamp(size_sqm / (2.0 * min_size))


def surroundings_score(listing: Listing, radius_m: float) -> float | None:
    """Mittelwert der Nähe zu Supermarkt, Apotheke und Haltestelle.

    Je näher (relativ zum Suchradius), desto besser. Es zählen nur die
    tatsächlich gefundenen POIs; wurde keiner gefunden, gibt es keinen Beitrag.
    """
    if radius_m <= 0:
        return None
    distances = [listing.supermarket_m, listing.pharmacy_m, listing.transit_stop_m]
    present = [d for d in distances if d is not None]
    if not present:
        return None
    return sum(_clamp(1.0 - d / radius_m) for d in present) / len(present)


def compute_score(listing: Listing, config: Config) -> float | None:
    """Gesamtscore (0–100) aus den vorhandenen Komponenten berechnen.

    Returns:
        Score gerundet auf eine Nachkommastelle oder ``None``, wenn keine einzige
        Komponente Daten hat (dann ist keine sinnvolle Bewertung möglich).
    """
    limits = config.search.limits_for(listing.listing_type)
    weights = config.scoring_weights.normalized()

    components: dict[str, float] = {}

    dist = distance_score(listing.transit_minutes, config.search.max_transit_minutes_to_tum)
    if dist is not None:
        components["distance_to_tum"] = dist

    rent = rent_score(listing.effective_rent(), limits.max_warm_rent)
    if rent is not None:
        components["rent"] = rent

    size = size_score(listing.size_sqm, limits.min_size_sqm)
    if size is not None:
        components["size"] = size

    surroundings = surroundings_score(listing, config.geo.poi_search_radius_m)
    if surroundings is not None:
        components["surroundings"] = surroundings

    if not components:
        return None

    # Gewichte auf die vorhandenen Komponenten beschränken und neu normalisieren.
    active_weight = sum(weights[name] for name in components)
    if active_weight <= 0:
        # Alle aktiven Komponenten haben Gewicht 0 → gleichgewichtet mitteln.
        return round(100.0 * sum(components.values()) / len(components), 1)

    score = sum(components[name] * weights[name] for name in components) / active_weight
    return round(100.0 * score, 1)
