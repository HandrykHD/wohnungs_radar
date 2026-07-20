"""Periodisches Einsammeln der Angebote via APScheduler."""

from __future__ import annotations

import asyncio
import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Config
from app.db import session_scope, set_state, upsert_listing
from app.enrich.pipeline import enrich_pending
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


async def scrape_only(config: Config) -> dict[str, int]:
    """Nur sammeln und deduplizieren — ohne Anreicherung.

    Bewusst schnell gehalten: Diese Funktion beantwortet den „Jetzt suchen"-Button
    im Frontend, der nicht minutenlang auf Geocoding/ÖPNV warten soll. Die
    Anreicherung läuft davon entkoppelt (siehe :func:`enrich_once`).

    Returns:
        ``{"fetched", "new", "updated", "online"}``. ``online`` sagt dem Aufrufer,
        ob sich ein anschließender Anreicherungslauf lohnt.
    """
    adapters = build_enabled_adapters(config)
    if not adapters:
        return {"fetched": 0, "new": 0, "updated": 0, "online": 1}

    online_ok = await is_online()

    # Der Dummy-Adapter braucht kein Netz — nur bei echten Quellen abbrechen.
    needs_network = any(adapter.name != "dummy" for adapter in adapters)
    if needs_network and not online_ok:
        logger.info("Offline — Sammel-Lauf wird übersprungen, nächster Versuch nach Intervall.")
        return {"fetched": 0, "new": 0, "updated": 0, "online": 0}

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
    return {
        "fetched": len(fetched),
        "new": new_count,
        "updated": updated_count,
        "online": 1 if online_ok else 0,
    }


async def enrich_once(config: Config) -> dict[str, int]:
    """Einen Schwung offener Angebote anreichern (Geocoding, Distanzen, POIs, Score).

    Braucht Netz und kann je nach ``enrichment.max_per_run`` einige Minuten dauern
    (Nominatim 1 Req/s, Overpass-Pausen). Ein Fehler wird abgefangen, damit die
    App weiterläuft.
    """
    try:
        with session_scope() as session:
            return await enrich_pending(session, config)
    except Exception:
        logger.exception("Anreicherungsphase mit unerwartetem Fehler abgebrochen")
        return {"processed": 0, "deferred": 0}


async def collect_once(config: Config) -> dict[str, int]:
    """Vollständiger Lauf für den Scheduler: erst sammeln, dann anreichern.

    Läuft im Hintergrund-Job, daher darf die Anreicherung hier ruhig dauern.

    Returns:
        Kombinierte Statistik aus Sammeln und Anreicherung.
    """
    scrape = await scrape_only(config)
    enrich = {"processed": 0, "deferred": 0}
    if scrape.get("online"):
        enrich = await enrich_once(config)

    return {
        "fetched": scrape["fetched"],
        "new": scrape["new"],
        "updated": scrape["updated"],
        "enriched": enrich["processed"],
        "enrich_deferred": enrich["deferred"],
    }


class CollectorScheduler:
    """Kapselt den APScheduler und den periodischen Sammel-Job."""

    JOB_ID = "collect"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
        # Hintergrund-Task der manuellen Anreicherung (Button), damit parallele
        # Auslösungen sich nicht überlagern.
        self._enrich_task: asyncio.Task | None = None

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
        """Vom „Jetzt suchen"-Button ausgelöst: schnell sammeln, dann anreichern.

        Das Sammeln wird abgewartet und liefert sofort die Trefferzahl zurück; die
        (langsame) Anreicherung läuft als Hintergrund-Task weiter, damit der Button
        nicht minutenlang blockiert. Ein bereits laufender Anreicherungs-Task wird
        nicht ein zweites Mal gestartet.
        """
        stats = await scrape_only(self._config)

        if stats.get("online") and (self._enrich_task is None or self._enrich_task.done()):
            self._enrich_task = asyncio.create_task(enrich_once(self._config))

        return stats

    def shutdown(self) -> None:
        """Scheduler geordnet herunterfahren."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler gestoppt")
