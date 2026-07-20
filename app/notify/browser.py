"""Browser-Kanal: Web Notifications API im offenen Chrome-Fenster.

Das Backend kann eine Browser-Notification nicht selbst auslösen. Deshalb legt
dieser Kanal die Meldung in eine kurze In-Memory-Warteschlange, die das Frontend
periodisch per ``GET /api/notifications/pending`` abholt und über die Web
Notifications API anzeigt. Klick auf die Notification öffnet das Angebot.

Die Queue lebt bewusst nur im Speicher: Geht sie beim Neustart verloren, ist das
unkritisch (der Desktop-Kanal hat bei geschlossenem Fenster ohnehin übernommen).
Sie ist gedeckelt, damit sie bei dauerhaft geschlossenem Fenster nicht wächst.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict

from app.notify.base import Notification, NotificationChannel

logger = logging.getLogger(__name__)

# Höchstens so viele unausgelieferte Browser-Meldungen vorhalten. Ältere fallen
# hinten heraus (der Desktop-Kanal hat sie dann bereits abgedeckt).
_MAX_QUEUE = 50
_queue: deque[Notification] = deque(maxlen=_MAX_QUEUE)


class BrowserChannel(NotificationChannel):
    """Reiht Meldungen zur Abholung durch das Frontend ein."""

    name = "browser"

    async def send(self, notification: Notification) -> bool:
        _queue.append(notification)
        return True


def drain_pending() -> list[dict]:
    """Alle wartenden Browser-Meldungen entnehmen und als JSON-Dicts liefern.

    Wird vom Frontend-Poll aufgerufen. Nach der Entnahme ist die Queue leer — die
    Anzeige (und der Klick-zum-Öffnen) passiert dann im Browser.
    """
    items = [asdict(_queue.popleft()) for _ in range(len(_queue))]
    if items:
        logger.debug("%d Browser-Benachrichtigung(en) ausgeliefert", len(items))
    return items


def clear_queue() -> None:
    """Queue leeren (für Tests)."""
    _queue.clear()
