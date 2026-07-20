"""Benachrichtigungen auslösen: welche Angebote, über welchen Kanal, genau einmal.

Kernregeln:

* **Ein Kanal pro Angebot.** Ist das Fenster sichtbar (frischer Heartbeat), geht
  die Meldung als Browser-Notification; sonst als Desktop-Toast — das ist der vom
  Auftrag geforderte „Fallback, wenn das Fenster nicht im Vordergrund ist".
* **Keine Doppel-Benachrichtigungen.** ``Listing.notified_at`` wird nach dem
  Versand gesetzt und schließt jede weitere Meldung zu diesem Angebot aus.
* **Kein Erststart-Schwall.** Beim ersten Lauf mit Datenbestand werden alle
  bereits vorhandenen Angebote still als benachrichtigt markiert (Baseline) —
  danach meldet die App nur echte Neuzugänge.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from app.config import Config
from app.db import get_state, session_scope, set_state
from app.models import Listing, utcnow
from app.notify.base import Notification
from app.notify.browser import BrowserChannel
from app.notify.desktop import DesktopChannel
from app.queries import matches_config_criteria

logger = logging.getLogger(__name__)

_BASELINE_KEY = "notify_baseline_done"
_HEARTBEAT_KEY = "last_heartbeat_at"
_VISIBLE_KEY = "page_visible"

# Kanäle: Browser ist zustandslos (Modul-Queue), Desktop hält Zustand
# (gefundene powershell.exe / Deaktivierung) und ist daher ein Singleton.
_browser = BrowserChannel()
_desktop = DesktopChannel()


def record_heartbeat(session: Session, visible: bool) -> None:
    """Lebenszeichen des offenen Fensters samt Sichtbarkeit festhalten."""
    set_state(session, _HEARTBEAT_KEY, utcnow().isoformat())
    set_state(session, _VISIBLE_KEY, "1" if visible else "0")


def _page_is_visible(session: Session, config: Config) -> bool:
    """Gilt das Fenster gerade als sichtbar und ansprechbar?

    Nur dann, wenn der letzte Heartbeat jung genug ist **und** die Seite als
    sichtbar gemeldet wurde. Ohne Heartbeat (Fenster zu) ist das Ergebnis ``False``
    → Desktop-Fallback.
    """
    raw = get_state(session, _HEARTBEAT_KEY)
    if raw is None or get_state(session, _VISIBLE_KEY) != "1":
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return utcnow() - last <= timedelta(seconds=config.notifications.heartbeat_stale_seconds)


def _eligible_listings(session: Session, config: Config) -> list[Listing]:
    """Angereicherte, noch nicht gemeldete Angebote, die die Kriterien erfüllen.

    Es werden nur angereicherte Angebote betrachtet, damit Filter (ÖPNV-Zeit) und
    Score aussagekräftig sind. Sortiert nach Score (beste zuerst), damit bei
    vielen Treffern die relevantesten zuerst gemeldet werden.
    """
    candidates = session.exec(
        select(Listing)
        .where(col(Listing.notified_at).is_(None))
        .where(col(Listing.enriched_at).is_not(None))
        .order_by(col(Listing.score).desc().nulls_last())
    ).all()

    min_score = config.notifications.min_score
    result: list[Listing] = []
    for listing in candidates:
        if not matches_config_criteria(listing, config):
            continue
        if listing.score is not None and listing.score < min_score:
            continue
        result.append(listing)
    return result


def _run_baseline_if_needed(session: Session) -> bool:
    """Beim ersten Lauf mit Bestand alles still als benachrichtigt markieren.

    Returns:
        ``True``, wenn gerade die Baseline gesetzt wurde (dann in diesem Lauf
        keine echten Meldungen mehr). ``False`` im Normalbetrieb.
    """
    if get_state(session, _BASELINE_KEY) == "1":
        return False

    unnotified = session.exec(select(Listing).where(col(Listing.notified_at).is_(None))).all()

    # Solange noch gar nichts gesammelt wurde, die Baseline aufschieben — sonst
    # würde der erste echte Sammel-Lauf fälschlich als „alles neu" gemeldet.
    if not unnotified:
        return False

    now = utcnow()
    for listing in unnotified:
        listing.notified_at = now
        session.add(listing)
    set_state(session, _BASELINE_KEY, "1")
    logger.info(
        "Benachrichtigungs-Baseline gesetzt: %d Bestandsangebote stumm markiert", len(unnotified)
    )
    return True


async def dispatch_notifications(config: Config) -> dict[str, int]:
    """Fällige Benachrichtigungen über den jeweils passenden Kanal versenden.

    Returns:
        Zähler ``{"browser", "desktop", "skipped"}``.
    """
    if not config.notifications.enabled:
        return {"browser": 0, "desktop": 0, "skipped": 0}

    with session_scope() as session:
        if _run_baseline_if_needed(session):
            return {"browser": 0, "desktop": 0, "skipped": 0}

        eligible = _eligible_listings(session, config)
        if not eligible:
            return {"browser": 0, "desktop": 0, "skipped": 0}

        use_browser = config.notifications.browser and _page_is_visible(session, config)
        limit = config.notifications.max_per_run

        browser_count = 0
        desktop_count = 0
        skipped = 0

        for listing in eligible[:limit]:
            notification = Notification.from_listing(listing)
            sent = False

            if use_browser:
                sent = await _browser.send(notification)
                if sent:
                    browser_count += 1
            elif config.notifications.desktop:
                sent = await _desktop.send(notification)
                if sent:
                    desktop_count += 1

            if sent:
                # Genau jetzt markieren — verhindert jede weitere Meldung.
                listing.notified_at = utcnow()
                session.add(listing)
            else:
                skipped += 1

        if browser_count or desktop_count:
            logger.info(
                "Benachrichtigt: %d Browser, %d Desktop (%d übersprungen)",
                browser_count,
                desktop_count,
                skipped,
            )

        return {"browser": browser_count, "desktop": desktop_count, "skipped": skipped}
