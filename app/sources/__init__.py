"""Portal-Adapter — Registry.

Neue Quelle hinzufügen:

1. Modul in ``app/sources/`` anlegen, von :class:`~app.sources.base.SourceAdapter`
   erben und ``name``/``label`` setzen.
2. Klasse unten in ``_REGISTRY`` eintragen.
3. Schlüssel (identisch zu ``name``) in ``config.yaml`` unter ``sources`` ergänzen.
"""

from __future__ import annotations

import logging

from app.config import Config
from app.sources.base import SourceAdapter
from app.sources.dummy import DummySource
from app.sources.kleinanzeigen import KleinanzeigenSource
from app.sources.wg_gesucht import WgGesuchtSource

logger = logging.getLogger(__name__)

#: Alle bekannten Adapter, Schlüssel = Config-Schlüssel unter ``sources``.
_REGISTRY: dict[str, type[SourceAdapter]] = {
    DummySource.name: DummySource,
    WgGesuchtSource.name: WgGesuchtSource,
    KleinanzeigenSource.name: KleinanzeigenSource,
}


def register(adapter_class: type[SourceAdapter]) -> None:
    """Adapter-Klasse nachträglich registrieren (z.B. aus Tests)."""
    _REGISTRY[adapter_class.name] = adapter_class


def available_sources() -> dict[str, type[SourceAdapter]]:
    """Alle registrierten Adapter-Klassen."""
    return dict(_REGISTRY)


def build_enabled_adapters(config: Config) -> list[SourceAdapter]:
    """Adapter-Instanzen für alle in der Config aktivierten Quellen bauen."""
    enabled_flags = config.sources.model_dump()
    adapters: list[SourceAdapter] = []

    for key, is_enabled in enabled_flags.items():
        if not is_enabled:
            continue
        adapter_class = _REGISTRY.get(key)
        if adapter_class is None:
            logger.warning(
                "Quelle '%s' ist in der Config aktiviert, aber kein Adapter registriert.", key
            )
            continue
        adapters.append(adapter_class(config))

    if not adapters:
        logger.warning("Keine Quelle aktiv — es werden keine Angebote gesammelt.")

    return adapters
