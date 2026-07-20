"""Periodisches Einsammeln der Angebote via APScheduler."""

from __future__ import annotations

import asyncio
import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Config
from app.db import session_scope, set_state, upsert_listing
from app.models import Listing, utcnow
from app.sources import build_enabled_adapters
from app.sources.base import run_adapter_safely

logger = logging.getLogger(__name__)

#: Host für den Konnektivitätstest. Antwortet mit HTTP 204 ohne Body und wird
#: nicht durch Captive Portals verfälscht.
_CONNECTIVITY_URL = "https://connectivitycheck.gstatic.com/generate_204"


async def is_online(timeout: float = 5.0) -> bool:
    """Prüfen, ob eine Internetverbindung besteht.

    Ohne Netz soll die App *sauber pausieren* statt jeden Adapter in einen
    Timeout laufen zu lassen und das Log mit Stacktraces zu fluten.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_CONNECTIVITY_URL)
        return response.status_code in (200, 204)
    except httpx.HTTPError:
        return False


async def collect_once(config: Config) -> dict[str, int]:
    """Einen vollständigen Sammel-Lauf ausführen.

    Ablauf: alle aktiven Adapter parallel abrufen (jeder für sich fehlerisoliert),
    Ergebnisse deduplizieren und speichern.

    Returns:
        Statistik mit den Schlüsseln ``fetched``, ``new`` und ``updated``.
    """
    adapters = build_enabled_adapters(config)
    if not adapters:
        return {"fetched": 0, "new": 0, "updated": 0}

    # Der Dummy-Adapter braucht kein Netz — nur bei echten Quellen abbrechen.
    needs_network = any(adapter.name != "dummy" for adapter in adapters)
    if needs_network and not await is_online():
        logger.info("Offline — Sammel-Lauf wird übersprungen, nächster Versuch nach Intervall.")
        return {"fetched": 0, "new": 0, "updated": 0}

    results = await asyncio.gather(*(run_adapter_safely(adapter) for adapter in adapters))
    fetched: list[Listing] = [listing for batch in results for listing in batch]

    new_count = 0
    updated_count = 0

    with session_scope() as session:
        for listing in fetched:
            _, is_new = upsert_listing(session, listing)
            if is_new:
                new_count += 1
            else:
                updated_count += 1
        set_state(session, "last_run_at", utcnow().isoformat())

    logger.info(
        "Sammel-Lauf beendet: %d abgerufen, %d neu, %d aktualisiert",
        len(fetched),
        new_count,
        updated_count,
    )
    return {"fetched": len(fetched), "new": new_count, "updated": updated_count}


class CollectorScheduler:
    """Kapselt den APScheduler und den periodischen Sammel-Job."""

    JOB_ID = "collect"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._scheduler = AsyncIOScheduler(timezone="Europe/Berlin")

    def start(self) -> None:
        """Scheduler starten und den Sammel-Job registrieren.

        ``coalesce=True`` fasst verpasste Läufe (Laptop im Standby) zu einem
        einzigen zusammen, statt sie beim Aufwachen alle nachzuholen.
        ``max_instances=1`` verhindert überlappende Läufe, falls ein Portal hängt.
        """
        interval = self._config.scraping.poll_interval_minutes

        self._scheduler.add_job(
            self._run_job,
            trigger=IntervalTrigger(minutes=interval),
            id=self.JOB_ID,
            name="Angebote sammeln",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.start()
        logger.info("Scheduler gestartet, Intervall: %d Minuten", interval)

    async def _run_job(self) -> None:
        """Job-Wrapper — fängt alles ab, damit der Scheduler nie stirbt."""
        try:
            await collect_once(self._config)
        except Exception:
            logger.exception("Sammel-Lauf mit unerwartetem Fehler abgebrochen")

    async def trigger_now(self) -> dict[str, int]:
        """Sammel-Lauf sofort ausführen (für den Button im Frontend)."""
        return await collect_once(self._config)

    def shutdown(self) -> None:
        """Scheduler geordnet herunterfahren."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler gestoppt")
