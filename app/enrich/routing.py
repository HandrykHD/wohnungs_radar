"""Entfernung und Fahrzeit zu Fuß/Rad zwischen zwei Punkten.

Hintergrund zur Wahl der Methode: Der öffentliche OSRM-Demo-Server hat nur das
Auto-Profil geladen und liefert für Fuß/Rad heimlich Autozeiten (verifiziert am
20.07.2026). Deshalb schätzt die App Fuß-/Radzeiten aus der Luftlinie mit einem
Umwegfaktor und realistischen Geschwindigkeiten. Wer exakte Werte braucht, kann
einen OpenRouteService-Key hinterlegen (``ORS_API_KEY``) — dann werden echte
Routen abgefragt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

import httpx
from sqlmodel import Session

from app.config import Config
from app.enrich.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class RouteResult:
    distance_km_crow: float
    walk_minutes: float
    bike_minutes: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Luftlinie zwischen zwei Koordinaten in Kilometern (Haversine-Formel)."""
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def estimate_minutes(distance_km: float, speed_kmh: float, detour_factor: float) -> float:
    """Fahrzeit aus Distanz und Geschwindigkeit schätzen.

    Der Umwegfaktor rechnet die Luftlinie auf einen realistischen Weg hoch (Wege
    verlaufen nicht schnurgerade).
    """
    if speed_kmh <= 0:
        return 0.0
    return (distance_km * detour_factor) / speed_kmh * 60.0


async def route_foot_bike(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> RouteResult:
    """Luftlinie sowie Fuß-/Radzeit von ``origin`` zum Ziel bestimmen (mit Cache).

    Ohne ORS-Key wird geschätzt (kein Netzzugriff nötig); mit Key werden echte
    Routen bei OpenRouteService abgefragt. Die Luftlinie wird immer exakt
    berechnet.
    """
    crow_km = haversine_km(origin[0], origin[1], destination[0], destination[1])

    enrich = config.enrichment
    walk = estimate_minutes(crow_km, enrich.walk_speed_kmh, enrich.detour_factor)
    bike = estimate_minutes(crow_km, enrich.bike_speed_kmh, enrich.detour_factor)

    if config.secrets.ors_api_key:
        ors = await _route_via_ors(session, client, config, origin, destination)
        if ors is not None:
            walk, bike = ors

    return RouteResult(
        distance_km_crow=round(crow_km, 3),
        walk_minutes=round(walk, 1),
        bike_minutes=round(bike, 1),
    )


async def route_car(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> float | None:
    """Autofahrzeit über den öffentlichen OSRM-Demo-Server (mit Cache).

    Der Demo-Server hat genau das Auto-Profil geladen — für Fuß/Rad unbrauchbar
    (siehe Modul-Docstring), fürs Auto aber die korrekte, kostenlose Quelle.

    Returns:
        Fahrzeit in Minuten oder ``None`` bei Fehler (wird nicht gecacht, damit
        der nächste Lauf es erneut versucht).
    """
    key = f"{origin[0]:.5f},{origin[1]:.5f}->{destination[0]:.5f},{destination[1]:.5f}"
    cached = cache_get(session, "car", key, config.geo.cache_ttl_days)
    if cached is not None:
        return cached["minutes"] if cached.get("ok") else None

    url = (
        f"{config.geo.osrm_url}/route/v1/driving/"
        f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"  # lon,lat!
    )
    try:
        response = await client.get(url, params={"overview": "false"})
        response.raise_for_status()
        duration_s = response.json()["routes"][0]["duration"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("OSRM-Autorouting fehlgeschlagen: %s", exc)
        return None

    minutes = round(duration_s / 60.0, 1)
    cache_set(session, "car", key, {"ok": True, "minutes": minutes})
    return minutes


async def _route_via_ors(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> tuple[float, float] | None:
    """Exakte Fuß-/Radzeit über OpenRouteService (nur mit Key).

    Returns:
        ``(walk_minutes, bike_minutes)`` oder ``None`` bei Fehler/Nichterreichbarkeit.
    """
    key = f"{origin[0]:.5f},{origin[1]:.5f}->{destination[0]:.5f},{destination[1]:.5f}"
    cached = cache_get(session, "route", key, config.geo.cache_ttl_days)
    if cached is not None:
        return (cached["walk"], cached["bike"]) if cached.get("ok") else None

    minutes: dict[str, float] = {}
    for profile, label in (("foot-walking", "walk"), ("cycling-regular", "bike")):
        params = {
            "api_key": config.secrets.ors_api_key,
            "start": f"{origin[1]},{origin[0]}",  # ORS erwartet lon,lat
            "end": f"{destination[1]},{destination[0]}",
        }
        try:
            response = await client.get(
                f"https://api.openrouteservice.org/v2/directions/{profile}", params=params
            )
            response.raise_for_status()
            summary = response.json()["features"][0]["properties"]["summary"]
            minutes[label] = summary["duration"] / 60.0
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            logger.warning("ORS-Routing (%s) fehlgeschlagen: %s", profile, exc)
            cache_set(session, "route", key, {"ok": False})
            return None

    cache_set(session, "route", key, {"ok": True, "walk": minutes["walk"], "bike": minutes["bike"]})
    return minutes["walk"], minutes["bike"]
