# Datenquellen — rechtliche Hinweise, APIs & Rate-Limits

> **Kurzfassung / Haftung:** Dieses Projekt ist ein privates Hilfsmittel, das
> öffentlich einsehbare Inserate in geringem Umfang abruft — Angebote, die du
> ohnehin von Hand durchsehen würdest. Das ist **rechtlich nicht eindeutig
> erlaubt**: Die AGB aller drei Zielportale untersagen automatisierten Abruf.
> `robots.txt` und AGB sind **zwei verschiedene Ebenen** — die `robots.txt`
> erlaubt zwar das Crawlen der Suchergebnisseiten, die AGB verbieten es
> trotzdem. Nutze die Adapter ausschließlich privat, mit langsamer Taktung, und
> aktiviere im Zweifel nur eine Quelle. Die Verantwortung liegt bei dir.

Stand der Recherche: **20.07.2026**. `robots.txt` und AGB ändern sich — prüfe sie
gelegentlich neu.

---

## Übersicht

| Portal          | Offizielle API                         | `robots.txt` (Suchseiten) | AGB: Automatisierung | Bot-Schutz            | Empfehlung |
|-----------------|----------------------------------------|---------------------------|----------------------|-----------------------|------------|
| WG-Gesucht      | nein (nur interne, undokumentierte)    | erlaubt                   | untersagt            | moderat               | M1-Start   |
| ImmoScout24     | ja (Partner-API, Zulassung nötig)      | erlaubt                   | untersagt            | hart (Akamai)         | API/Playwright |
| Kleinanzeigen   | nein                                   | erlaubt (RSS gesperrt)    | untersagt            | hart (Cloudflare)     | vorsichtig |

---

## 1. WG-Gesucht (`wg_gesucht`)

- **URL:** https://www.wg-gesucht.de
- **Offizielle API:** Keine öffentliche/dokumentierte API. Die App nutzt intern
  eine JSON-Schnittstelle unter `/api/`, die per `robots.txt` **ausdrücklich
  gesperrt** ist (`Disallow: /api/`) — deshalb wird sie **nicht** verwendet.
  Der Adapter liest ausschließlich die regulären, öffentlichen Suchergebnis-HTML-Seiten.
- **`robots.txt` (geprüft 20.07.2026):** `User-agent: *` erlaubt die Suchseiten.
  Gesperrt sind u.a. `/api/`, `/angebot-bearbeiten.html`, `/nachricht-senden.html`,
  `/agb.html`. Der Adapter meidet all diese Pfade.
- **AGB:** § Nutzungsbedingungen untersagen „automatisiertes Auslesen" und das
  Anlegen von Kopien des Datenbestands. → **Nur privater, kleiner Umfang**, keine
  Weitergabe, keine Vollindexierung.
- **Bot-Schutz:** Moderat. Kein aggressives JS-Challenge auf den Suchseiten,
  daher mit `httpx + selectolax` (ohne Browser) abrufbar.
- **Rate-Limits (selbst gesetzt, konservativ):**
  - ≥ 3 s Pause zwischen Requests (`scraping.request_delay_seconds`),
  - max. 3 Ergebnisseiten pro Lauf (`scraping.max_pages_per_run`),
  - Lauf alle 15 min → ~12 Requests/Stunde. Bewusst weit unter jeder Schwelle.
- **Bei Sperrung (HTTP 403):** Der Adapter bricht sofort ab und wiederholt
  **nicht** (siehe `PoliteClient`). Dann: Intervall erhöhen, Quelle pausieren.

## 2. ImmoScout24 (`immoscout`)

- **URL:** https://www.immobilienscout24.de
- **Offizielle API:** **Ja** — die *Import/Export*- bzw. *Partner-API* (OAuth 1.0a).
  Das ist der **rechtlich saubere Weg**. Sie richtet sich an gewerbliche Partner;
  eine Zulassung für ein privates Hobbyprojekt ist unwahrscheinlich, aber die
  Anfrage ist kostenlos. Doku: https://api.immobilienscout24.de
- **`robots.txt` (geprüft 20.07.2026):** Suchseiten sind erlaubt; gesperrt sind
  u.a. `/Suche/`-Parameter mit `geocodes=`, `/immobilienpreise/api/`, `/adresse/`.
  Bemerkenswert: `ClaudeBot`, `GPTBot` etc. sind **ausdrücklich erlaubt** — das
  betrifft aber nur diese benannten Bots, nicht einen eigenen Scraper.
- **AGB:** Untersagen automatisiertes Auslesen ausdrücklich und mit Nachdruck.
- **Bot-Schutz:** **Hart.** Akamai Bot Manager mit JS-/TLS-Fingerprinting. Ein
  reiner `httpx`-Abruf wird zuverlässig geblockt. Realistisch nur mit
  **Playwright (Chromium)** und selbst dann fragil.
- **Empfehlung:** Zuerst Partner-API anfragen. Andernfalls Adapter in M4 mit
  Playwright, sehr langsam, und mit der Bereitschaft, ihn wieder abzuschalten.

## 3. Kleinanzeigen (`kleinanzeigen`)

- **URL:** https://www.kleinanzeigen.de (früher eBay Kleinanzeigen)
- **Offizielle API:** Keine öffentliche API für die Suche.
- **`robots.txt` (geprüft 20.07.2026):** `User-agent: *` erlaubt die
  Kategorie-/Suchseiten. Gesperrt sind viele `/m-*`- und `/p-*`-Pfade sowie
  **ausdrücklich der RSS-Feed** `Disallow: /s-feed.rss` — ein bequemer legaler
  Feed-Weg entfällt damit.
- **AGB:** Untersagen automatisierten Zugriff und das systematische Auslesen.
- **Bot-Schutz:** **Hart.** Cloudflare mit Managed Challenge. Wie bei IS24
  praktisch nur über einen echten Browser (Playwright) erreichbar, und auch das
  wird regelmäßig unterbrochen.
- **Empfehlung:** In M4 mit Playwright, niedrige Frequenz. Viele private
  Angebote ohne Makler, aber stark schwankende Datenqualität.

---

## Gemeinsame Schutzmaßnahmen im Code

Alle Adapter erben von `app/sources/base.py::SourceAdapter` und nutzen den
`PoliteClient`. Der setzt die folgenden Regeln **zentral** um:

- **Ehrlicher User-Agent** mit Projektname und Kontaktadresse (`OSM_CONTACT_EMAIL`
  aus `.env`) — Betreiber können uns zuordnen statt zu raten.
- **Feste Pause** vor jedem Request (`request_delay_seconds`).
- **Exponentielles Backoff mit Jitter** bei `429`/`5xx`.
- **Sofortiger, wiederholungsfreier Abbruch bei `403`** — eine Ablehnung wird
  respektiert, nicht umgangen.
- **Aggressives Caching** aller Geo-Ergebnisse (`GeoCache`), damit Nominatim,
  OSRM, Overpass und die ÖPNV-API nur einmal pro Adresse belastet werden.
- **Fehlerisolation:** `run_adapter_safely` fängt jeden Adapter-Fehler ab; eine
  kaputte Quelle legt die App nicht lahm.

## Geo-Dienste (Nutzungsbedingungen)

| Dienst      | Zweck                    | Limit / Policy | Hinweis |
|-------------|--------------------------|----------------|---------|
| Nominatim   | Adresse → Koordinaten    | max. **1 req/s**, echter UA mit Kontakt Pflicht | Ergebnisse werden dauerhaft gecacht |
| OSRM (Demo) | Fuß-/Rad-Route zur TUM   | „reasonable use", kein SLA | Alternative: OpenRouteService (Key) oder eigener OSRM |
| Overpass    | POIs (Supermarkt etc.)   | fair use, teilen sich alle Nutzer | wenige Queries dank Cache |
| db.transport.rest | ÖPNV-Verbindung (HAFAS/DB, deckt MVV) | fair use, kein Key | pro Angebot 1× berechnet und gecacht |

Details und Alternativen (eigener OSRM-Server, OpenTripPlanner) stehen in der
`README.md` unter „Geo-Dienste".
