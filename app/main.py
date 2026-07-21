"""FastAPI-Anwendung: Routen, Templates, App-Start."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import Config, get_config
from app.db import count_new_since, get_session, get_state, init_engine, session_scope, set_state
from app.enrich.pipeline import enrich_one
from app.logging_setup import setup_logging
from app.models import Listing, ListingStatus, utcnow
from app.notify.browser import drain_pending
from app.notify.dispatch import record_heartbeat
from app.queries import DEFAULT_SORT, ListingFilters, fetch_listings
from app.scheduler import CollectorScheduler

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

#: Vom Lifespan gesetzt, damit Routen den Sammel-Lauf anstoßen können.
scheduler: CollectorScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start und geordnetes Herunterfahren der Anwendung."""
    global scheduler

    config = get_config()
    setup_logging(config.server.resolved_log_path(), config.server.log_level)
    logger.info("Wohnungs-Radar startet …")

    init_engine(config.server.resolved_database_path())

    scheduler = CollectorScheduler(config)
    scheduler.start()

    yield

    if scheduler is not None:
        scheduler.shutdown()
    logger.info("Wohnungs-Radar beendet.")


app = FastAPI(title="Wohnungs- & WG-Radar München", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _format_datetime(value: datetime | None) -> str:
    """Zeitstempel für die Anzeige aufbereiten (naives UTC → lokale Zeit).

    Die DB liefert naive Datetimes (siehe ``models.utcnow``). Wir markieren sie
    daher explizit als UTC, bevor wir in die lokale Zeitzone umrechnen — sonst
    würde ``astimezone`` sie fälschlich als Lokalzeit deuten.
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%d.%m. %H:%M")


templates.env.filters["dt"] = _format_datetime
# Campus-Adresse als Template-Global: Ziel für den Google-Maps-Routenlink in
# der „Zur TUM"-Spalte (konfigurierbar über tum_campus.primary).
templates.env.globals["campus_address"] = get_config().tum_campus.primary.address


ConfigDep = Annotated[Config, Depends(get_config)]
SessionDep = Annotated[Session, Depends(get_session)]


def _filters_from_query(
    config: Config,
    max_rent: float | None,
    min_size: float | None,
    districts: str | None,
    max_transit: int | None,
    listing_type: str | None,
    sort: str,
    only_favorites: bool,
    include_hidden: bool,
) -> ListingFilters:
    """Query-Parameter in ein :class:`ListingFilters` übersetzen.

    Nicht gesetzte Parameter fallen auf die Defaults aus der ``config.yaml``
    zurück, damit der erste Seitenaufruf schon sinnvoll gefiltert ist.
    """
    defaults = ListingFilters.from_config_defaults(config)
    return ListingFilters(
        max_rent=max_rent if max_rent is not None else defaults.max_rent,
        min_size=min_size if min_size is not None else defaults.min_size,
        districts=(
            [part.strip() for part in districts.split(",") if part.strip()]
            if districts is not None
            else defaults.districts
        ),
        max_transit_minutes=(
            max_transit if max_transit is not None else defaults.max_transit_minutes
        ),
        listing_type=listing_type or None,
        only_favorites=only_favorites,
        include_hidden=include_hidden,
        sort=sort,
    )


# --- Seiten -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    config: ConfigDep,
    session: SessionDep,
    max_rent: float | None = Query(None),
    min_size: float | None = Query(None),
    districts: str | None = Query(None),
    max_transit: int | None = Query(None),
    listing_type: str | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    only_favorites: bool = Query(False),
    include_hidden: bool = Query(False),
) -> HTMLResponse:
    """Hauptansicht mit Filterleiste und Angebotstabelle."""
    filters = _filters_from_query(
        config,
        max_rent,
        min_size,
        districts,
        max_transit,
        listing_type,
        sort,
        only_favorites,
        include_hidden,
    )
    listings = fetch_listings(session, filters)

    # Badge "neu seit letztem Besuch" berechnen, danach den Besuchszeitpunkt
    # fortschreiben — beim nächsten Aufruf zählt nur, was seitdem dazukam.
    last_visit_raw = get_state(session, "last_visit_at")
    last_visit = datetime.fromisoformat(last_visit_raw) if last_visit_raw else None
    new_since_visit = count_new_since(session, last_visit)
    set_state(session, "last_visit_at", utcnow().isoformat())
    session.commit()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "listings": listings,
            "filters": filters,
            "config": config,
            "new_since_visit": new_since_visit,
            "last_run_at": get_state(session, "last_run_at"),
        },
    )


@app.get("/map", response_class=HTMLResponse)
async def map_view(request: Request, config: ConfigDep) -> HTMLResponse:
    """Kartenansicht: Angebote als Marker plus TUM-Campus-Marker.

    Die Marker-Daten holt sich die Seite per JSON von ``/api/listings`` (dieselbe
    Filterung wie die Tabelle); hier werden nur die Campus-Koordinaten aus der
    Config an das Template übergeben.
    """
    primary = config.tum_campus.primary
    secondary = config.tum_campus.secondary
    campus = [{"label": primary.label, "lat": primary.lat, "lon": primary.lon}]
    if secondary and secondary.enabled and secondary.lat and secondary.lon:
        campus.append({"label": secondary.label, "lat": secondary.lat, "lon": secondary.lon})

    return templates.TemplateResponse(
        request=request,
        name="map.html",
        context={"campus_points": campus},
    )


@app.get("/partials/listings", response_class=HTMLResponse)
async def listings_partial(
    request: Request,
    config: ConfigDep,
    session: SessionDep,
    max_rent: float | None = Query(None),
    min_size: float | None = Query(None),
    districts: str | None = Query(None),
    max_transit: int | None = Query(None),
    listing_type: str | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    only_favorites: bool = Query(False),
    include_hidden: bool = Query(False),
) -> HTMLResponse:
    """Nur die Tabellenzeilen — von HTMX für Filter/Sortierung nachgeladen."""
    filters = _filters_from_query(
        config,
        max_rent,
        min_size,
        districts,
        max_transit,
        listing_type,
        sort,
        only_favorites,
        include_hidden,
    )
    return templates.TemplateResponse(
        request=request,
        name="_listing_rows.html",
        context={"listings": fetch_listings(session, filters)},
    )


# --- API --------------------------------------------------------------------


@app.get("/api/listings")
async def api_listings(
    config: ConfigDep,
    session: SessionDep,
    max_rent: float | None = Query(None),
    min_size: float | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    include_hidden: bool = Query(False),
) -> list[Listing]:
    """Angebote als JSON (für Kartenansicht und externe Nutzung)."""
    filters = _filters_from_query(
        config, max_rent, min_size, None, None, None, sort, False, include_hidden
    )
    return fetch_listings(session, filters)


@app.post("/api/listings/{listing_id}/status", response_class=HTMLResponse)
async def set_listing_status(
    request: Request,
    listing_id: int,
    session: SessionDep,
    status: Annotated[str, Form()],
) -> HTMLResponse:
    """Status eines Angebots setzen (Favorit / Ausblenden / Gesehen).

    Antwortet mit der neu gerenderten Tabellenzeile, die HTMX an Ort und Stelle
    austauscht — kein Reload, keine verlorene Scrollposition.
    """
    try:
        new_status = ListingStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unbekannter Status: {status}") from exc

    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Angebot nicht gefunden")

    listing.status = new_status
    session.add(listing)
    session.commit()
    session.refresh(listing)

    # Frischer Favorit ohne Anreicherung: sofort geocodieren, damit er direkt
    # auf der Karte erscheint, statt in der Warteschlange zu warten.
    if new_status == ListingStatus.FAVORISIERT and listing.enriched_at is None:
        asyncio.create_task(enrich_one(get_config(), listing.id))

    return templates.TemplateResponse(
        request=request,
        name="_listing_row.html",
        context={"listing": listing},
    )


@app.post("/api/collect")
async def api_collect() -> JSONResponse:
    """Sammel-Lauf sofort auslösen (Button im Frontend)."""
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler noch nicht bereit")
    stats = await scheduler.trigger_now()
    return JSONResponse(stats)


@app.post("/api/heartbeat", status_code=204)
async def api_heartbeat(session: SessionDep, visible: bool = Query(True)) -> Response:
    """Lebenszeichen des offenen Fensters (mit Sichtbarkeit).

    Steuert die Kanalwahl: Bei frischem Heartbeat und sichtbarer Seite gehen neue
    Treffer als Browser-Notification, sonst als Desktop-Toast.
    """
    record_heartbeat(session, visible)
    session.commit()
    return Response(status_code=204)


@app.get("/api/notifications/pending")
async def api_notifications_pending() -> JSONResponse:
    """Wartende Browser-Benachrichtigungen abholen (vom Frontend gepollt)."""
    return JSONResponse(drain_pending())


@app.get("/health")
async def health() -> dict[str, object]:
    """Health-Check für Startskript und Überwachung."""
    with session_scope() as session:
        total = len(session.exec(select(Listing)).all())
        last_run = get_state(session, "last_run_at")

    return {
        "status": "ok",
        "listings": total,
        "last_run_at": last_run,
        "scheduler_running": scheduler is not None,
    }
