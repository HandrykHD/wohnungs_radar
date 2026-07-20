"""ÖPNV-Fahrzeit zur TUM über die offizielle MVG-API.

Liefert eine Tür-zu-Tür-Verbindung mit Gesamtdauer, Zahl der Umstiege und einer
kurzen Linien-Zusammenfassung (z.B. ``"U6"`` oder ``"Tram 27 → U3"``).

Wichtige Eigenheit der MVG-Antwort: Jeder Knoten (auch der Zielknoten) trägt das
Feld ``plannedDeparture``. Die **Ankunftszeit** ist daher das ``plannedDeparture``
des ``to``-Knotens der letzten Teilstrecke (verifiziert am 20.07.2026).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlmodel import Session

from app.config import Config
from app.enrich.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

_PEDESTRIAN = "PEDESTRIAN"


@dataclass(frozen=True)
class TransitResult:
    minutes: float
    changes: int
    summary: str


async def transit_to_campus(
    session: Session,
    client: httpx.AsyncClient,
    config: Config,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> TransitResult | None:
    """Schnellste ÖPNV-Verbindung von ``origin`` zum Campus bestimmen (mit Cache).

    Returns:
        ``TransitResult`` oder ``None``, wenn keine Verbindung gefunden wird bzw.
        die MVG-API nicht erreichbar ist.
    """
    key = f"{origin[0]:.5f},{origin[1]:.5f}->{destination[0]:.5f},{destination[1]:.5f}"
    cached = cache_get(session, "transit", key, config.geo.cache_ttl_days)
    if cached is not None:
        return _from_payload(cached)

    params = {
        "originLatitude": origin[0],
        "originLongitude": origin[1],
        "destinationLatitude": destination[0],
        "destinationLongitude": destination[1],
    }
    try:
        response = await client.get(f"{config.geo.mvg_api_url}/routes", params=params)
        response.raise_for_status()
        connections = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("MVG-Routing nicht erreichbar: %s", exc)
        return None

    best = _pick_fastest(connections)
    if best is None:
        cache_set(session, "transit", key, {"found": False})
        return None

    cache_set(session, "transit", key, {"found": True, **best.__dict__})
    return best


def _pick_fastest(connections: list) -> TransitResult | None:
    """Aus allen MVG-Verbindungen die zeitlich kürzeste auswählen und parsen."""
    if not isinstance(connections, list):
        return None

    best: TransitResult | None = None
    for conn in connections:
        parsed = _parse_connection(conn)
        if parsed is not None and (best is None or parsed.minutes < best.minutes):
            best = parsed
    return best


def _parse_connection(conn: dict) -> TransitResult | None:
    """Eine einzelne MVG-Verbindung in ein ``TransitResult`` überführen."""
    parts = conn.get("parts") or []
    if not parts:
        return None

    departure = _parse_time(parts[0].get("from", {}).get("plannedDeparture"))
    # Ankunft = plannedDeparture des Zielknotens der letzten Teilstrecke.
    arrival = _parse_time(parts[-1].get("to", {}).get("plannedDeparture"))
    if departure is None or arrival is None:
        return None

    minutes = (arrival - departure).total_seconds() / 60.0
    if minutes < 0:
        return None

    ride_labels: list[str] = []
    for part in parts:
        line = part.get("line") or {}
        if line.get("transportType") != _PEDESTRIAN:
            label = line.get("label")
            if label:
                ride_labels.append(label)

    changes = max(0, len(ride_labels) - 1)
    summary = " → ".join(ride_labels) if ride_labels else "nur Fußweg"
    return TransitResult(minutes=round(minutes, 1), changes=changes, summary=summary)


def _parse_time(raw: str | None) -> datetime | None:
    """ISO-8601-Zeitstempel der MVG (mit Zeitzone) parsen."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _from_payload(payload: dict) -> TransitResult | None:
    if not payload.get("found"):
        return None
    return TransitResult(
        minutes=payload["minutes"], changes=payload["changes"], summary=payload["summary"]
    )
