"""Nächstgelegene Umgebungs-POIs über die OpenStreetMap Overpass API.

Ermittelt für ein Angebot die Distanz (Luftlinie) zum nächsten Supermarkt, zur
nächsten Apotheke und zur nächsten ÖPNV-Haltestelle innerhalb eines Suchradius.

Hinweis: Overpass lehnt Anfragen ohne aussagekräftigen User-Agent mit HTTP 406
ab (verifiziert am 20.07.2026) — der gemeinsame Client setzt ihn.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from sqlmodel import Session

from app.config import Config
from app.enrich.cache import cache_get, cache_set
from app.enrich.routing import haversine_km

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoiResult:
    supermarket_m: float | None
    pharmacy_m: float | None
    transit_stop_m: float | None
    transit_stop_name: str | None


def _build_query(lat: float, lon: float, radius_m: int) -> str:
    """Overpass-QL bauen: Supermärkte, Apotheken und Haltestellen im Umkreis."""
    around = f"(around:{radius_m},{lat},{lon})"
    return (
        "[out:json][timeout:25];"
        "("
        f'node["shop"="supermarket"]{around};'
        f'node["amenity"="pharmacy"]{around};'
        f'node["highway"="bus_stop"]{around};'
        f'node["public_transport"="station"]{around};'
        f'node["railway"="station"]{around};'
        f'node["railway"="tram_stop"]{around};'
        ");"
        "out body;"
    )


async def nearby_pois(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    lat: float,
    lon: float,
) -> PoiResult | None:
    """Nächste POIs je Kategorie bestimmen (mit Cache).

    Returns:
        ``PoiResult`` bei Erfolg (auch wenn nichts gefunden wurde → Felder
        ``None``, wird gecacht). ``None`` signalisiert einen **behebbaren**
        Fehler (Overpass gerade überlastet/rate-limitiert) — dann werden die
        POI-Felder nicht gesetzt und beim nächsten Lauf erneut versucht.
    """
    radius = config.geo.poi_search_radius_m
    key = f"{lat:.5f},{lon:.5f},r{radius}"
    cached = cache_get(session, "poi", key, config.geo.cache_ttl_days)
    if cached is not None:
        return PoiResult(**cached)

    query = _build_query(lat, lon, radius)
    delay = config.geo.overpass_delay_seconds

    # Overpass ist ein stark ausgelasteter Gratis-Dienst und antwortet unter Last
    # mit 429/504. Strategie: vor jeder Anfrage eine Pause; bei Überlast nacheinander
    # den Hauptserver und die konfigurierten Spiegel probieren.
    endpoints = [config.geo.overpass_url, *config.geo.overpass_fallback_urls]
    for index, endpoint in enumerate(endpoints):
        await asyncio.sleep(delay * (index + 1))
        try:
            response = await client.post(endpoint, data={"data": query})
            response.raise_for_status()
            elements = response.json().get("elements", [])
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (429, 504) and index < len(endpoints) - 1:
                logger.info("Overpass %d bei %s — nächster Spiegel", status, endpoint)
                continue
            logger.warning("Overpass-Fehler (%.4f,%.4f): %s", lat, lon, exc)
            return None
        except (httpx.HTTPError, ValueError) as exc:
            if index < len(endpoints) - 1:
                logger.info("Overpass %s nicht erreichbar — nächster Spiegel", endpoint)
                continue
            logger.warning("Overpass nicht erreichbar (%.4f,%.4f): %s", lat, lon, exc)
            return None

        result = _nearest_per_category(lat, lon, elements)
        cache_set(session, "poi", key, result.__dict__)
        return result

    return None


def _nearest_per_category(lat: float, lon: float, elements: list) -> PoiResult:
    """Aus den Overpass-Elementen je Kategorie das nächste bestimmen."""
    best_super: float | None = None
    best_pharm: float | None = None
    best_stop: float | None = None
    best_stop_name: str | None = None

    for element in elements:
        el_lat = element.get("lat")
        el_lon = element.get("lon")
        if el_lat is None or el_lon is None:
            continue
        tags = element.get("tags", {})
        distance_m = haversine_km(lat, lon, el_lat, el_lon) * 1000.0

        if tags.get("shop") == "supermarket":
            if best_super is None or distance_m < best_super:
                best_super = distance_m
        elif tags.get("amenity") == "pharmacy":
            if best_pharm is None or distance_m < best_pharm:
                best_pharm = distance_m
        else:
            # alles Übrige ist eine Haltestelle/Station
            if best_stop is None or distance_m < best_stop:
                best_stop = distance_m
                best_stop_name = tags.get("name")

    return PoiResult(
        supermarket_m=round(best_super, 1) if best_super is not None else None,
        pharmacy_m=round(best_pharm, 1) if best_pharm is not None else None,
        transit_stop_m=round(best_stop, 1) if best_stop is not None else None,
        transit_stop_name=best_stop_name,
    )
