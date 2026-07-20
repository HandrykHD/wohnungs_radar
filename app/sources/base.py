"""Gemeinsames Interface für alle Portal-Adapter.

Jedes Portal wird als eigene Klasse implementiert, die von :class:`SourceAdapter`
erbt. Dadurch lassen sich Quellen einzeln in der ``config.yaml`` abschalten, und
ein defekter Adapter (Layout-Änderung, Bot-Wall, Netzfehler) kann die App nicht
zum Absturz bringen — siehe :func:`run_adapter_safely`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from app.config import Config
from app.models import Listing

logger = logging.getLogger(__name__)


class SourceUnavailable(Exception):
    """Die Quelle ist temporär nicht abrufbar (Netzfehler, Rate-Limit, Bot-Wall)."""


class PoliteClient:
    """HTTP-Client, der sich an Anstandsregeln hält.

    Konkret:

    * fester Mindestabstand zwischen zwei Requests derselben Quelle,
    * exponentielles Backoff mit Jitter bei 429/5xx,
    * ehrlicher, kontaktierbarer User-Agent,
    * sofortiger Abbruch bei 403 — das ist eine bewusste Ablehnung des
      Betreibers, die man nicht durch Wiederholen "lösen" darf.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._delay = config.scraping.request_delay_seconds
        self._max_retries = config.scraping.max_retries
        self._client = httpx.AsyncClient(
            headers=config.http_headers(),
            timeout=config.scraping.request_timeout_seconds,
            follow_redirects=True,
        )

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        """GET mit Delay, Retry und Backoff.

        Raises:
            SourceUnavailable: Wenn der Abruf endgültig fehlschlägt oder der
                Betreiber uns aktiv aussperrt.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            # Vor *jedem* Request warten, nicht nur zwischen Retries: so hält der
            # Adapter das Tempo auch bei einer langen Seitenliste ein.
            await asyncio.sleep(self._delay)

            try:
                response = await self._client.get(url, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("Netzfehler bei %s (Versuch %d): %s", url, attempt + 1, exc)
            else:
                if response.status_code == 403:
                    # Klare Absage — nicht umgehen, nicht wiederholen.
                    raise SourceUnavailable(
                        f"{url} antwortet mit 403 (Zugriff verweigert). "
                        "Die Quelle blockiert automatisierte Abrufe."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                    logger.warning(
                        "HTTP %d bei %s (Versuch %d)", response.status_code, url, attempt + 1
                    )
                else:
                    response.raise_for_status()
                    return response

            # Exponentielles Backoff mit Jitter, damit parallele Adapter nicht
            # im Gleichtakt erneut anklopfen.
            if attempt < self._max_retries - 1:
                backoff = self._delay * (2**attempt) + random.uniform(0, 1)
                logger.debug("Warte %.1fs vor erneutem Versuch", backoff)
                await asyncio.sleep(backoff)

        raise SourceUnavailable(
            f"{url} nach {self._max_retries} Versuchen nicht erreichbar: {last_error}"
        )


class SourceAdapter(ABC):
    """Basisklasse für Portal-Adapter.

    Unterklassen implementieren :meth:`fetch` und liefern **ungespeicherte**
    :class:`~app.models.Listing`-Objekte mit gesetztem ``fingerprint``. Speichern
    und Deduplizieren übernimmt der Scheduler.
    """

    #: Kennung, identisch zum Schlüssel in ``config.yaml`` unter ``sources``.
    name: ClassVar[str] = "base"
    #: Anzeigename für Frontend und Logs.
    label: ClassVar[str] = "Basis"

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    async def fetch(self) -> list[Listing]:
        """Aktuelle Angebote der Quelle abrufen.

        Returns:
            Liste ungespeicherter Angebote. Leere Liste ist ein gültiges Ergebnis.

        Raises:
            SourceUnavailable: Wenn die Quelle nicht abrufbar ist.
        """
        raise NotImplementedError


async def run_adapter_safely(adapter: SourceAdapter) -> list[Listing]:
    """Adapter ausführen und dabei jeden Fehler isolieren.

    Ein einzelnes kaputtes Portal darf den Sammel-Lauf der anderen nicht
    abbrechen. Fehler werden geloggt, das Ergebnis ist dann eine leere Liste.
    """
    try:
        listings = await adapter.fetch()
    except SourceUnavailable as exc:
        logger.warning("Quelle '%s' nicht verfügbar: %s", adapter.name, exc)
        return []
    except Exception:
        logger.exception("Quelle '%s' ist mit einem unerwarteten Fehler abgebrochen", adapter.name)
        return []

    logger.info("Quelle '%s': %d Angebote abgerufen", adapter.name, len(listings))
    return listings
