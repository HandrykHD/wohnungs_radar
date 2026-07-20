"""Geocoding von Adressen über OpenStreetMap Nominatim.

Wandelt eine Adresse (``"Türkenstraße 58, München"``) in Koordinaten um und
liefert nebenbei den Stadtteil (``address.suburb``) mit. Ergebnisse werden
gecacht; Nominatim erlaubt max. 1 Anfrage/Sekunde, daher wird vor jeder echten
Abfrage eine Pause eingehalten.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from sqlmodel import Session

from app.config import Config
from app.enrich.cache import cache_get, cache_set
from app.models import normalize_district

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    district: str | None


async def geocode(
    session: Session, client: httpx.AsyncClient, config: Config, address: str
) -> GeocodeResult | None:
    """Adresse zu Koordinaten auflösen (mit Cache).

    Returns:
        ``GeocodeResult`` oder ``None``, wenn Nominatim nichts findet bzw. der
        Dienst nicht erreichbar ist. Ein Nichtfund wird gecacht, damit dieselbe
        unauflösbare Adresse nicht bei jedem Lauf erneut angefragt wird.
    """
    key = address.strip().casefold()
    cached = cache_get(session, "geocode", key, config.geo.cache_ttl_days)
    if cached is not None:
        return _from_payload(cached)
    # Explizit gecachter Nichtfund (payload == None) — cache_get liefert dann
    # None und wir würden erneut anfragen; daher unterscheiden wir über einen
    # Sentinel im Payload (siehe _to_payload).

    await asyncio.sleep(config.geo.nominatim_delay_seconds)

    params = {
        "q": address,
        "format": "jsonv2",
        "limit": "1",
        "addressdetails": "1",
        "countrycodes": "de",
    }
    try:
        response = await client.get(f"{config.geo.nominatim_url}/search", params=params)
        response.raise_for_status()
        results = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Geocoding fehlgeschlagen für %r: %s", address, exc)
        return None

    if not results:
        cache_set(session, "geocode", key, {"found": False})
        return None

    top = results[0]
    addr = top.get("address", {})
    # Stadtteil bevorzugt aus suburb, sonst city_district/borough.
    district = normalize_district(
        addr.get("suburb") or addr.get("city_district") or addr.get("borough")
    )
    payload = {
        "found": True,
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "district": district,
    }
    cache_set(session, "geocode", key, payload)
    return _from_payload(payload)


def _from_payload(payload: dict) -> GeocodeResult | None:
    """Cache-/API-Payload in ein ``GeocodeResult`` überführen."""
    if not payload.get("found"):
        return None
    return GeocodeResult(lat=payload["lat"], lon=payload["lon"], district=payload.get("district"))
