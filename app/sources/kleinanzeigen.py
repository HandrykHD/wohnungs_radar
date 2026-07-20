"""Adapter für Kleinanzeigen (https://www.kleinanzeigen.de).

Liest die öffentlichen HTML-Suchergebnisseiten der Mietwohnungs-Kategorie in
München. Zum Umsetzungszeitpunkt (20.07.2026) war die Suche mit dem ehrlichen
Projekt-User-Agent ohne Bot-Challenge erreichbar — es wird **kein** gefälschter
Browser-User-Agent und keine Umgehung von Schutzmechanismen verwendet.

Datenqualität-Hinweis: Anders als WG-Gesucht weist Kleinanzeigen in der
Listenansicht **keine** strukturierte Wohnfläche/Zimmerzahl und keine Straße aus.
Diese Felder werden — soweit möglich — aus dem Titel bzw. der PLZ/Stadtteil-Angabe
gewonnen; fehlt etwas, bleibt es leer (das Scoring gewichtet dann die vorhandenen
Komponenten). Siehe DATA_SOURCES.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from app.models import Listing, ListingType, make_fingerprint, normalize_district, utcnow
from app.sources.base import PoliteClient, SourceAdapter, SourceUnavailable

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.kleinanzeigen.de"
# Mietwohnungen (c203) in München (l6411), nur Angebote (keine Gesuche).
_CATEGORY_PATH = "s-wohnung-mieten/anzeige:angebote"
_CATEGORY_SUFFIX = "muenchen/c203l6411"

# Titel, die klar ein Gesuch statt ein Angebot sind (der anzeige:angebote-Filter
# lässt vereinzelt welche durch).
_WANTED_RE = re.compile(r"\b(such(e|t|en)|gesucht)\b", re.IGNORECASE)
_WG_RE = re.compile(r"\bWG\b|Wohngemeinschaft|WG[- ]?Zimmer", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[- ]?Zimmer|\b(\d)[- ]?Zi\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"(\d[\d.]*)")
_PLZ_RE = re.compile(r"\b(\d{5})\b")


@dataclass(frozen=True)
class _Category:
    path: str
    suffix: str


def _clean(text: str | None) -> str | None:
    """Whitespace inkl. Zero-Width-Space (​) normalisieren; leer → None."""
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text.replace("​", "")).strip()
    return collapsed or None


def _parse_price(raw: str | None) -> float | None:
    """Erste Zahl aus Angaben wie ``"1.450 € VB 1.500 €"`` als Warmmiete lesen.

    Kleinanzeigen hängt an „VB"-Preise mitunter einen durchgestrichenen
    Ursprungspreis an; maßgeblich ist die **erste** Zahl. Der Punkt ist
    Tausendertrennzeichen.
    """
    if not raw:
        return None
    match = _PRICE_RE.search(raw)
    if not match:
        return None
    digits = match.group(1).replace(".", "")
    return float(digits) if digits else None


def _parse_size(title: str) -> float | None:
    """Wohnfläche aus dem Titel ziehen (``"45 m²"`` → 45.0), sofern vorhanden."""
    match = _SIZE_RE.search(title)
    return float(match.group(1).replace(",", ".")) if match else None


def _parse_rooms(title: str) -> float | None:
    """Zimmerzahl aus dem Titel ziehen (``"2-Zimmer"`` / ``"3 Zi"``), sofern vorhanden."""
    match = _ROOMS_RE.search(title)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return float(value.replace(",", ".")) if value else None


def _parse_location(raw: str | None) -> tuple[str | None, str | None]:
    """Standortzeile ``"80637 Neuhausen"`` in (PLZ, Stadtteil) zerlegen."""
    cleaned = _clean(raw)
    if not cleaned:
        return None, None
    plz_match = _PLZ_RE.search(cleaned)
    plz = plz_match.group(1) if plz_match else None
    district = normalize_district(cleaned)
    return plz, district


class KleinanzeigenSource(SourceAdapter):
    """Sammelt Münchner Mietangebote von Kleinanzeigen."""

    name = "kleinanzeigen"
    label = "Kleinanzeigen"

    def _search_url(self, page: int) -> str:
        """Such-URL für eine 1-basierte Seite bauen."""
        if page <= 1:
            return f"{_BASE_URL}/{_CATEGORY_PATH}/{_CATEGORY_SUFFIX}"
        return f"{_BASE_URL}/{_CATEGORY_PATH}/seite:{page}/{_CATEGORY_SUFFIX}"

    async def fetch(self) -> list[Listing]:
        """Mehrere Ergebnisseiten abrufen und parsen (mit laufinterner Dedup)."""
        max_pages = self.config.scraping.max_pages_per_run
        seen: set[str] = set()
        listings: list[Listing] = []

        async with PoliteClient(self.config) as client:
            for page in range(1, max_pages + 1):
                url = self._search_url(page)
                try:
                    response = await client.get(url)
                except SourceUnavailable:
                    raise

                page_listings = self._parse_page(response.text)
                if not page_listings:
                    break
                for listing in page_listings:
                    key = listing.external_id or listing.fingerprint
                    if key in seen:
                        continue
                    seen.add(key)
                    listings.append(listing)

        return listings

    def _parse_page(self, html: str) -> list[Listing]:
        """Alle Anzeigenkarten einer Seite parsen (jede isoliert)."""
        tree = HTMLParser(html)
        results: list[Listing] = []
        for card in tree.css("article.aditem"):
            try:
                listing = self._parse_card(card)
            except Exception:
                logger.exception("Konnte eine Kleinanzeigen-Karte nicht parsen — übersprungen")
                continue
            if listing is not None:
                results.append(listing)
        return results

    def _parse_card(self, card: Node) -> Listing | None:
        """Eine Anzeigenkarte in ein :class:`Listing` überführen (oder überspringen)."""
        external_id = card.attributes.get("data-adid")
        href = card.attributes.get("data-href")
        if not external_id or not href:
            return None

        link = card.css_first("h2.text-module-begin a") or card.css_first("a.ellipsis")
        title = _clean(link.text()) if link else None
        if not title:
            return None

        # Gesuche aussortieren (der URL-Filter lässt vereinzelt welche durch).
        if _WANTED_RE.search(title):
            return None

        price_node = card.css_first(".aditem-main--middle--price-shipping--price")
        rent = _parse_price(price_node.text() if price_node else None)

        location_node = card.css_first(".aditem-main--top--left")
        plz, district = _parse_location(location_node.text() if location_node else None)

        listing_type = ListingType.WG_ROOM if _WG_RE.search(title) else ListingType.APARTMENT

        # Adresse für das Geocoding: Straße gibt es nicht, daher PLZ + Stadtteil.
        # Das liefert eine ungefähre (Stadtteil-genaue) Position — bewusst grob.
        address_parts = [part for part in (plz, district) if part]
        address = f"{' '.join(address_parts)}, München" if address_parts else None
        url = urljoin(_BASE_URL, href)

        return Listing(
            fingerprint=make_fingerprint(self.name, url, external_id),
            source=self.name,
            external_id=external_id,
            url=url,
            title=title,
            listing_type=listing_type,
            rent_warm=rent,
            size_sqm=_parse_size(title),
            rooms=_parse_rooms(title),
            district=district,
            address=address,
            posted_at=None,
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
