"""Laden und Validieren der Anwendungskonfiguration.

Die Konfiguration kommt aus zwei Quellen:

* ``config.yaml`` — alle nicht-geheimen Einstellungen, versioniert.
* ``.env``       — ausschließlich Secrets/API-Keys, nie versioniert.

Die YAML-Struktur wird über Pydantic-Modelle validiert, damit ein Tippfehler in
der Config beim Start auffällt und nicht erst zur Laufzeit im Scheduler.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Projektwurzel = Elternverzeichnis des "app"-Pakets.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CampusPoint(BaseModel):
    """Ein Referenzpunkt, zu dem Entfernungen berechnet werden."""

    label: str
    address: str
    lat: float | None = None
    lon: float | None = None
    enabled: bool = True


class TumCampusConfig(BaseModel):
    primary: CampusPoint
    secondary: CampusPoint | None = None


class TypeLimits(BaseModel):
    """Budget- und Größengrenzen für einen Objekttyp."""

    max_warm_rent: float
    min_size_sqm: float


class SearchConfig(BaseModel):
    listing_types: list[str] = ["wg_room", "apartment"]
    wg_room: TypeLimits
    apartment: TypeLimits
    districts: list[str] = []
    max_transit_minutes_to_tum: int = 40
    max_radius_km_from_munich: float = 25.0

    def limits_for(self, listing_type: str) -> TypeLimits:
        """Gibt die Limits für einen Objekttyp zurück.

        Unbekannte Typen fallen auf die (großzügigeren) Wohnungs-Limits zurück,
        damit ein neuer Portal-Typ nicht versehentlich alles wegfiltert.
        """
        return self.wg_room if listing_type == "wg_room" else self.apartment


class ScoringWeights(BaseModel):
    distance_to_tum: float = 0.4
    rent: float = 0.3
    size: float = 0.2
    surroundings: float = 0.1

    def normalized(self) -> dict[str, float]:
        """Gewichte auf Summe 1.0 normalisieren.

        Erlaubt es, in der config.yaml beliebige Zahlen zu vergeben (z.B. 4/3/2/1)
        statt penibel auf 1.0 zu addieren.
        """
        raw = self.model_dump()
        total = sum(raw.values())
        if total <= 0:
            # Entartete Config: gleichmäßig verteilen statt durch null zu teilen.
            return dict.fromkeys(raw, 1 / len(raw))
        return {key: value / total for key, value in raw.items()}


class SourcesConfig(BaseModel):
    dummy: bool = True
    wg_gesucht: bool = False
    immoscout: bool = False
    kleinanzeigen: bool = False


class ScrapingConfig(BaseModel):
    poll_interval_minutes: int = 15
    request_delay_seconds: float = 3.0
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    max_pages_per_run: int = 3
    user_agent: str = "WohnungsRadar/0.1"


class GeoConfig(BaseModel):
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    nominatim_delay_seconds: float = 1.1
    osrm_url: str = "https://router.project-osrm.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    # Ausweich-Spiegel, falls der Hauptserver mit 429/504 überlastet ist.
    overpass_fallback_urls: list[str] = Field(
        default_factory=lambda: ["https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
    )
    overpass_delay_seconds: float = 1.5
    mvg_api_url: str = "https://www.mvg.de/api/bgw-pt/v3"
    poi_search_radius_m: int = 1000
    cache_ttl_days: int = 90


class EnrichmentConfig(BaseModel):
    enabled: bool = True
    max_per_run: int = 20
    walk_speed_kmh: float = 4.8
    bike_speed_kmh: float = 15.0
    detour_factor: float = 1.35


class NotificationsConfig(BaseModel):
    enabled: bool = True
    browser: bool = True
    desktop: bool = True
    telegram: bool = False
    min_score: float = 0.0
    max_per_run: int = 5
    # Ein Heartbeat des offenen Fensters gilt bis zu so viele Sekunden als frisch.
    # Ist er frisch UND die Seite sichtbar, geht die Meldung in den Browser, sonst
    # als Desktop-Toast (der „Fallback, wenn das Fenster nicht im Vordergrund ist").
    heartbeat_stale_seconds: int = 60


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    database_path: str = "data/wohnungsradar.db"
    log_path: str = "logs/wohnungsradar.log"
    log_level: str = "INFO"

    def resolved_database_path(self) -> Path:
        """Absoluter DB-Pfad; relative Angaben zählen ab der Projektwurzel."""
        return _resolve_path(self.database_path)

    def resolved_log_path(self) -> Path:
        """Absoluter Log-Pfad; relative Angaben zählen ab der Projektwurzel."""
        return _resolve_path(self.log_path)


class Secrets(BaseModel):
    """Werte aus .env. Niemals loggen, niemals ans Frontend geben."""

    osm_contact_email: str = ""
    ors_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


class Config(BaseModel):
    tum_campus: TumCampusConfig
    search: SearchConfig
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    geo: GeoConfig = Field(default_factory=GeoConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    secrets: Secrets = Field(default_factory=Secrets)

    def http_headers(self) -> dict[str, str]:
        """Gemeinsame HTTP-Header für alle ausgehenden Requests.

        Ein ehrlicher, kontaktierbarer User-Agent ist bei OSM-Diensten Pflicht und
        bei Portalen schlicht anständig: Betreiber können uns bei Problemen
        zuordnen und blockieren, statt raten zu müssen.
        """
        agent = self.scraping.user_agent
        if self.secrets.osm_contact_email:
            agent = f"{agent} ({self.secrets.osm_contact_email})"
        return {
            "User-Agent": agent,
            "Accept-Language": "de-DE,de;q=0.9",
        }


def _resolve_path(raw: str) -> Path:
    """Pfad expandieren (``~``) und relative Pfade an die Projektwurzel binden."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_secrets() -> Secrets:
    """Secrets aus der Umgebung bzw. .env lesen."""
    load_dotenv(PROJECT_ROOT / ".env")
    return Secrets(
        osm_contact_email=os.getenv("OSM_CONTACT_EMAIL", ""),
        ors_api_key=os.getenv("ORS_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )


def load_config(path: Path | None = None) -> Config:
    """Config aus YAML + .env laden und validieren.

    Args:
        path: Optionaler Pfad zur YAML-Datei. Default: ``config.yaml`` im Projekt.

    Raises:
        FileNotFoundError: Wenn die YAML-Datei fehlt.
        pydantic.ValidationError: Bei ungültiger Struktur.
    """
    config_path = path or (PROJECT_ROOT / "config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(
            f"Konfigurationsdatei nicht gefunden: {config_path}\n"
            "Lege config.yaml an (Vorlage liegt im Repository)."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    raw["secrets"] = _load_secrets().model_dump()
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Prozessweite Config-Instanz (gecacht).

    Wird als FastAPI-Dependency und von den Scheduler-Jobs genutzt. Der Cache
    lässt sich in Tests mit ``get_config.cache_clear()`` zurücksetzen.
    """
    return load_config()
