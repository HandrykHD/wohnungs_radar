"""Gemeinsame Typen und Interface für Benachrichtigungskanäle.

Ein Kanal (Browser, Desktop, später Telegram/E-Mail) implementiert
:class:`NotificationChannel`. So lässt sich ein weiterer Kanal ergänzen, ohne die
Dispatch-Logik anzufassen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import Listing


@dataclass(frozen=True)
class Notification:
    """Eine versandfertige Benachrichtigung zu genau einem Angebot."""

    listing_id: int
    title: str
    body: str
    url: str

    @classmethod
    def from_listing(cls, listing: Listing) -> Notification:
        """Aus einem Angebot eine kompakte Meldung bauen.

        Titel = Kurzfassung fürs Auge; Body = die harten Fakten (Miete, Größe,
        Stadtteil, ÖPNV-Zeit zur TUM), soweit vorhanden.
        """
        parts: list[str] = [listing.rent_display()]
        if listing.size_sqm:
            parts.append(f"{listing.size_sqm:.0f} m²")
        if listing.district:
            parts.append(listing.district)
        if listing.transit_minutes:
            parts.append(f"{listing.transit_minutes:.0f} min zur TUM")
        if listing.score is not None:
            parts.append(f"Score {listing.score:.0f}")

        return cls(
            listing_id=listing.id or 0,
            title=listing.title,
            body=" · ".join(parts),
            url=listing.url,
        )


class NotificationChannel(ABC):
    """Basisklasse für einen Ausgabekanal."""

    #: Kennung für Logs/Config.
    name: str = "base"

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Eine Benachrichtigung ausliefern.

        Returns:
            ``True`` bei Erfolg. Fehler dürfen **nicht** durchschlagen — ein
            defekter Kanal darf die App nicht stören —, daher fangen die
            Implementierungen intern ab und geben im Zweifel ``False`` zurück.
        """
        raise NotImplementedError
