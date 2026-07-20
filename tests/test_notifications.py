"""Tests für die Benachrichtigungs-Dispatch-Logik.

Der Fokus liegt auf den drei Kernregeln aus dem Auftrag: keine Erststart-Lawine
(Baseline), Kanalwahl nach Fenster-Sichtbarkeit und keine Doppel-Benachrichtigung.
Die Kanäle selbst (Browser-Queue, Desktop-Toast) werden durch Zähl-Fakes ersetzt,
damit die Tests offline und ohne Windows laufen.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.notify.dispatch as dispatch
from app.db import get_state, session_scope
from app.models import Listing, ListingType, make_fingerprint, utcnow
from app.notify.base import Notification


@pytest.fixture
def engine_bound(engine, monkeypatch):  # noqa: ANN001
    """Die globale Engine auf die Test-Engine umbiegen (dispatch nutzt session_scope)."""
    import app.db as db

    monkeypatch.setattr(db, "_engine", engine)
    return engine


class _CountingChannel:
    """Fake-Kanal, der nur zählt und die letzten Meldungen merkt."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> bool:
        if self.ok:
            self.sent.append(notification)
        return self.ok


@pytest.fixture
def channels(monkeypatch):  # noqa: ANN001
    """Browser- und Desktop-Kanal durch Fakes ersetzen."""
    browser = _CountingChannel()
    desktop = _CountingChannel()
    monkeypatch.setattr(dispatch, "_browser", browser)
    monkeypatch.setattr(dispatch, "_desktop", desktop)
    return browser, desktop


def _make_enriched_listing(session, **overrides):  # noqa: ANN001
    """Ein angereichertes, kriteriengerechtes WG-Zimmer in die DB legen."""
    data = {
        "source": "wg_gesucht",
        "url": "https://www.wg-gesucht.de/x.1.html",
        "external_id": "1",
        "title": "Schönes Zimmer",
        "listing_type": ListingType.WG_ROOM,
        "rent_warm": 600.0,
        "size_sqm": 18.0,
        "district": "Schwabing",
        "transit_minutes": 25.0,
        "score": 70.0,
        "enriched_at": utcnow(),
    }
    data.update(overrides)
    data.setdefault("fingerprint", make_fingerprint("wg_gesucht", data["url"], data["external_id"]))
    listing = Listing(**data)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def _set_visible(session, visible: bool) -> None:
    dispatch.record_heartbeat(session, visible)
    session.commit()


# --- Baseline ---------------------------------------------------------------


async def test_baseline_meldet_bestand_nicht(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    """Erster Lauf mit Bestand: nichts melden, aber alles als benachrichtigt markieren."""
    browser, desktop = channels
    with session_scope() as session:
        _make_enriched_listing(session)
        _set_visible(session, True)

    stats = await dispatch.dispatch_notifications(config)

    assert stats == {"browser": 0, "desktop": 0, "skipped": 0}
    assert not browser.sent and not desktop.sent
    with session_scope() as session:
        assert get_state(session, "notify_baseline_done") == "1"
        listing = session.get(Listing, 1)
        assert listing.notified_at is not None  # stumm baseline-markiert


async def test_neues_angebot_nach_baseline_wird_gemeldet(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    """Nach gesetzter Baseline löst ein echter Neuzugang eine Meldung aus."""
    browser, _ = channels
    with session_scope() as session:
        _make_enriched_listing(session, external_id="1", url="https://x/1")  # wird baseline
        _set_visible(session, True)
    await dispatch.dispatch_notifications(config)  # setzt Baseline

    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2")

    stats = await dispatch.dispatch_notifications(config)
    assert stats["browser"] == 1
    assert len(browser.sent) == 1
    assert browser.sent[0].url == "https://x/2"


# --- Kanalwahl --------------------------------------------------------------


async def test_sichtbares_fenster_meldet_in_browser(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    browser, desktop = channels
    await _seed_with_baseline(config)
    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2")
        _set_visible(session, True)

    await dispatch.dispatch_notifications(config)
    assert len(browser.sent) == 1
    assert len(desktop.sent) == 0


async def test_verstecktes_fenster_meldet_auf_desktop(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    browser, desktop = channels
    await _seed_with_baseline(config)
    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2")
        _set_visible(session, False)

    await dispatch.dispatch_notifications(config)
    assert len(browser.sent) == 0
    assert len(desktop.sent) == 1


async def test_abgelaufener_heartbeat_meldet_auf_desktop(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    """Kein frischer Heartbeat (Fenster zu) → Desktop-Fallback."""
    browser, desktop = channels
    await _seed_with_baseline(config)
    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2")
        # Heartbeat künstlich veralten lassen.
        stale = (utcnow() - timedelta(seconds=300)).isoformat()
        from app.db import set_state

        set_state(session, "last_heartbeat_at", stale)
        set_state(session, "page_visible", "1")
        session.commit()

    await dispatch.dispatch_notifications(config)
    assert len(browser.sent) == 0
    assert len(desktop.sent) == 1


# --- Keine Doppel-Benachrichtigung ------------------------------------------


async def test_keine_doppelte_benachrichtigung(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    """Ein zweiter Dispatch-Lauf darf dasselbe Angebot nicht erneut melden."""
    browser, _ = channels
    await _seed_with_baseline(config)
    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2")
        _set_visible(session, True)

    first = await dispatch.dispatch_notifications(config)
    second = await dispatch.dispatch_notifications(config)

    assert first["browser"] == 1
    assert second["browser"] == 0
    assert len(browser.sent) == 1


async def test_max_per_run_begrenzt(engine_bound, channels, config) -> None:  # noqa: ANN001
    """Nicht mehr als max_per_run Meldungen pro Lauf (Lawinenschutz)."""
    browser, _ = channels
    await _seed_with_baseline(config)
    limit = config.notifications.max_per_run
    with session_scope() as session:
        for i in range(limit + 3):
            _make_enriched_listing(session, external_id=f"n{i}", url=f"https://x/n{i}")
        _set_visible(session, True)

    stats = await dispatch.dispatch_notifications(config)
    assert stats["browser"] == limit
    assert len(browser.sent) == limit


# --- Filter/Score-Gate ------------------------------------------------------


async def test_nicht_passendes_angebot_wird_nicht_gemeldet(
    engine_bound, channels, config
) -> None:  # noqa: ANN001
    """Über Budget (WG-Zimmer > max_warm_rent) → keine Meldung."""
    browser, desktop = channels
    await _seed_with_baseline(config)
    with session_scope() as session:
        _make_enriched_listing(session, external_id="2", url="https://x/2", rent_warm=5000.0)
        _set_visible(session, True)

    stats = await dispatch.dispatch_notifications(config)
    assert stats["browser"] == 0 and stats["desktop"] == 0
    assert not browser.sent and not desktop.sent


# --- Hilfen -----------------------------------------------------------------


async def _seed_with_baseline(config) -> None:  # noqa: ANN001
    """Baseline vorbereiten: ein Bestandsangebot anlegen und Baseline setzen."""
    with session_scope() as session:
        _make_enriched_listing(session, external_id="seed", url="https://x/seed")
    await dispatch.dispatch_notifications(config)
