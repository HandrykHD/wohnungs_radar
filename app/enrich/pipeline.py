"""Orchestrierung der Anreicherung: Geocode → Routing → ÖPNV → POIs → Score.

Der Scheduler ruft nach jedem Sammel-Lauf :func:`enrich_pending` auf. Dort werden
noch nicht angereicherte Angebote — schonend begrenzt auf ``max_per_run`` —
nacheinander verarbeitet. Nacheinander, weil vor allem Nominatim nur eine Anfrage
pro Sekunde erlaubt; ein paralleler Ansturm brächte nur IP-Sperren.
"""

from __future__ import annotations

import logging

import httpx
from sqlmodel import Session, col, select

from app.config import Config
from app.db import session_scope
from app.enrich.cache import cache_get, cache_set
from app.enrich.geocode import geocode
from app.enrich.pois import nearby_pois
from app.enrich.routing import route_foot_bike
from app.enrich.scoring import compute_score
from app.enrich.transit import transit_to_campus
from app.models import Listing, utcnow

logger = logging.getLogger(__name__)


async def resolve_campus(
    session: Session, client: httpx.AsyncClient, config: Config
) -> tuple[float, float] | None:
    """Zielkoordinaten des primären Campus bestimmen.

    Bevorzugt die in der Config hinterlegten ``lat``/``lon`` (dann ist kein Netz
    nötig). Fehlen sie, wird die Adresse einmalig geocodiert und das Ergebnis
    gecacht.
    """
    primary = config.tum_campus.primary
    if primary.lat is not None and primary.lon is not None:
        return primary.lat, primary.lon

    cached = cache_get(session, "campus", primary.address, config.geo.cache_ttl_days)
    if cached is not None:
        return (cached["lat"], cached["lon"]) if cached.get("found") else None

    result = await geocode(session, client, config, primary.address)
    if result is None:
        cache_set(session, "campus", primary.address, {"found": False})
        return None
    cache_set(
        session, "campus", primary.address, {"found": True, "lat": result.lat, "lon": result.lon}
    )
    return result.lat, result.lon


async def enrich_listing(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    listing: Listing,
    campus: tuple[float, float] | None,
) -> bool:
    """Ein einzelnes Angebot anreichern.

    Ablauf: Adresse geocodieren, dann Fuß-/Rad-Distanz, ÖPNV-Fahrzeit und
    Umgebungs-POIs bestimmen und zuletzt den Score berechnen. Jeder Schritt ist
    optional — schlägt ein externer Dienst fehl, bleibt das jeweilige Feld leer,
    ohne die übrigen zu verhindern.

    Returns:
        ``True``, wenn die Anreicherung als abgeschlossen gilt (``enriched_at``
        wird gesetzt). ``False``, wenn sie wegen eines behebbaren Fehlers (z.B.
        Geocoding-Adresse vorhanden, aber Dienst offline) erneut versucht werden
        soll.
    """
    # 1) Geocoding — nur wenn eine Adresse vorliegt und noch keine Koordinaten da sind.
    if (listing.lat is None or listing.lon is None) and listing.address:
        geo = await geocode(session, client, config, listing.address)
        if geo is None:
            # Adresse vorhanden, aber kein Ergebnis: könnte ein temporärer
            # Ausfall sein → später erneut versuchen.
            return False
        listing.lat, listing.lon = geo.lat, geo.lon
        listing.geocoded_at = utcnow()
        if geo.district and not listing.district:
            listing.district = geo.district

    # 2) Entfernungen/ÖPNV nur mit Koordinaten und bekanntem Ziel.
    if listing.lat is not None and listing.lon is not None and campus is not None:
        origin = (listing.lat, listing.lon)

        route = await route_foot_bike(session, client, config, origin, campus)
        listing.distance_km_crow = route.distance_km_crow
        listing.walk_minutes = route.walk_minutes
        listing.bike_minutes = route.bike_minutes

        transit = await transit_to_campus(session, client, config, origin, campus)
        if transit is not None:
            listing.transit_minutes = transit.minutes
            listing.transit_changes = transit.changes
            listing.transit_summary = transit.summary

        # POIs sind best effort: ist Overpass gerade überlastet (None), bleiben
        # die Felder leer und die Anreicherung wird trotzdem abgeschlossen — die
        # Umgebung ist die am geringsten gewichtete Score-Komponente.
        pois = await nearby_pois(session, client, config, listing.lat, listing.lon)
        if pois is not None:
            listing.supermarket_m = pois.supermarket_m
            listing.pharmacy_m = pois.pharmacy_m
            listing.transit_stop_m = pois.transit_stop_m
            listing.transit_stop_name = pois.transit_stop_name

    # 3) Score aus allem berechnen, was vorhanden ist.
    listing.score = compute_score(listing, config)
    listing.enriched_at = utcnow()
    session.add(listing)
    return True


async def enrich_one(config: Config, listing_id: int) -> bool:
    """Ein einzelnes Angebot sofort anreichern (z.B. frisch markierter Favorit).

    Läuft als eigener Hintergrund-Task außerhalb der normalen Warteschlange,
    damit ein Favorit nicht hinter hunderten unbearbeiteten Angeboten wartet,
    sondern direkt Koordinaten (→ Karte), Score und TUM-Zeit bekommt.

    Returns:
        ``True``, wenn das Angebot (jetzt oder schon vorher) angereichert ist.
    """
    try:
        with session_scope() as session:
            listing = session.get(Listing, listing_id)
            if listing is None:
                return False
            if listing.enriched_at is not None:
                return True

            async with httpx.AsyncClient(
                headers=config.http_headers(),
                timeout=config.scraping.request_timeout_seconds,
                follow_redirects=True,
            ) as client:
                campus = await resolve_campus(session, client, config)
                done = await enrich_listing(session, client, config, listing, campus)
            session.commit()
            return done
    except Exception:
        logger.exception("Sofort-Anreicherung für Angebot %s fehlgeschlagen", listing_id)
        return False


def _pending_query(limit: int):
    """Angebote ohne Anreicherung, älteste zuerst (die warten am längsten)."""
    return (
        select(Listing)
        .where(col(Listing.enriched_at).is_(None))
        .order_by(col(Listing.first_seen_at).asc())
        .limit(limit)
    )


async def enrich_pending(session: Session, config: Config) -> dict[str, int]:
    """Einen Schwung noch nicht angereicherter Angebote verarbeiten.

    Returns:
        Statistik mit ``processed`` (abgeschlossen) und ``deferred`` (auf später
        verschoben, weil ein Dienst gerade nicht antwortete).
    """
    if not config.enrichment.enabled:
        return {"processed": 0, "deferred": 0}

    listings = list(session.exec(_pending_query(config.enrichment.max_per_run)).all())
    if not listings:
        return {"processed": 0, "deferred": 0}

    processed = 0
    deferred = 0

    async with httpx.AsyncClient(
        headers=config.http_headers(),
        timeout=config.scraping.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        campus = await resolve_campus(session, client, config)
        if campus is None:
            logger.warning(
                "Campus-Koordinaten unbekannt — Entfernungen werden übersprungen. "
                "Prüfe tum_campus.primary in config.yaml."
            )

        for listing in listings:
            try:
                done = await enrich_listing(session, client, config, listing, campus)
            except Exception:
                logger.exception("Anreicherung für Angebot %s fehlgeschlagen", listing.id)
                deferred += 1
                continue

            if done:
                processed += 1
            else:
                deferred += 1
            # Zwischenspeichern, damit ein späterer Abbruch nicht alles verwirft.
            session.commit()

    logger.info("Anreicherung: %d verarbeitet, %d verschoben", processed, deferred)
    return {"processed": processed, "deferred": deferred}
