"""Testadapter ohne Netzzugriff.

Erzeugt realistisch aussehende Münchner Angebote. Zweck:

* Frontend, Dedup, Scoring und Benachrichtigungen entwickeln und testen, ohne
  ein Portal anzufassen,
* nachvollziehbare Daten in den Tests,
* funktionierende Demo, solange die echten Adapter deaktiviert sind.

Die Adressen sind echte Münchner Straßen, damit Geocoding (M2) etwas Sinnvolles
zurückliefert.
"""

from __future__ import annotations

import random
from datetime import timedelta

from app.models import Listing, ListingType, make_fingerprint, utcnow
from app.sources.base import SourceAdapter

# (Straße, Stadtteil, Objekttyp)
_ADDRESSES: list[tuple[str, str, ListingType]] = [
    ("Boltzmannstraße 15, 85748 Garching", "Garching", ListingType.WG_ROOM),
    ("Schleißheimer Straße 210, 80797 München", "Schwabing", ListingType.WG_ROOM),
    ("Türkenstraße 58, 80799 München", "Maxvorstadt", ListingType.APARTMENT),
    ("Freisinger Landstraße 42, 80939 München", "Freimann", ListingType.WG_ROOM),
    ("Lerchenauer Straße 12, 80809 München", "Milbertshofen", ListingType.APARTMENT),
    ("Nymphenburger Straße 130, 80636 München", "Neuhausen", ListingType.APARTMENT),
    ("Lichtenbergstraße 8, 85748 Garching", "Garching", ListingType.WG_ROOM),
    ("Hohenzollernstraße 91, 80796 München", "Schwabing", ListingType.WG_ROOM),
]


class DummySource(SourceAdapter):
    """Liefert deterministisch erzeugte Beispielangebote."""

    name = "dummy"
    label = "Testdaten"

    def __init__(self, config, seed: int = 42) -> None:  # noqa: ANN001
        super().__init__(config)
        # Fester Seed: gleiche Angebote bei jedem Lauf. Genau das prüft nebenbei,
        # ob der Dedup-Mechanismus greift — beim zweiten Lauf darf nichts Neues
        # entstehen.
        self._random = random.Random(seed)

    async def fetch(self) -> list[Listing]:
        """Beispielangebote erzeugen (kein Netzzugriff)."""
        listings: list[Listing] = []
        now = utcnow()

        for index, (address, district, listing_type) in enumerate(_ADDRESSES):
            url = f"https://example.invalid/wohnungsradar/demo/{index + 1}"
            external_id = f"demo-{index + 1}"

            if listing_type is ListingType.WG_ROOM:
                size = self._random.uniform(12, 26)
                rent_warm = self._random.uniform(450, 950)
                rooms = 1.0
                title = f"WG-Zimmer in {district}, {size:.0f} m²"
            else:
                size = self._random.uniform(24, 55)
                rent_warm = self._random.uniform(800, 1500)
                rooms = float(self._random.choice([1.0, 1.5, 2.0]))
                title = f"{rooms:g}-Zimmer-Wohnung in {district}, {size:.0f} m²"

            listings.append(
                Listing(
                    fingerprint=make_fingerprint(self.name, url, external_id),
                    source=self.name,
                    external_id=external_id,
                    url=url,
                    title=title,
                    listing_type=listing_type,
                    rent_warm=round(rent_warm, 2),
                    rent_cold=round(rent_warm * 0.8, 2),
                    size_sqm=round(size, 1),
                    rooms=rooms,
                    district=district,
                    address=address,
                    available_from="sofort",
                    posted_at=now - timedelta(hours=index * 3),
                )
            )

        return listings
