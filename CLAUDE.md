# Projekt: Wohnungs- & WG-Radar München

> **Auftrag an Claude Code:** Baue die unten spezifizierte Anwendung. Lies zuerst
> den Abschnitt [Offene Entscheidungen](#0-offene-entscheidungen-zuerst-klären) und
> stelle mir die dort genannten Fragen, **bevor** du mit Code beginnst. Arbeite
> danach in den [Meilensteinen](#8-umsetzung-in-meilensteinen) und committe nach
> jedem Meilenstein.

---

## 1. Ziel

Eine lokal laufende **Web-App**, die kontinuierlich Wohnungs- und WG-Angebote in
München sammelt, jedes Angebot mit Standort-Informationen (v. a. Entfernung zur
TUM-Informatik) anreichert und mich **automatisch bei neuen passenden Angeboten
benachrichtigt**. Die App läuft im Hintergrund, solange der Laptop online ist, und
zeigt die Ergebnisse in einem **Google-Chrome-Fenster** an.

## 2. Kontext & Rahmenbedingungen

- **Nutzer:** Student/Mitarbeiter an der **Informatik-Fakultät der TUM**.
  → Relevanter Referenzpunkt ist **TUM Garching, Boltzmannstraße 3, 85748 Garching**
  (nicht der Innenstadt-Campus). Nächste ÖPNV-Anbindung: U6 *Garching-Forschungszentrum*.
  Der Referenzpunkt muss **konfigurierbar** sein (zweiter Standort Arcisstraße optional).
- **Betriebssystem:** Windows-Laptop mit **WSL2** (Ubuntu). Backend darf in WSL2
  laufen, das Chrome-Fenster läuft auf dem Windows-Host → Netzwerkbrücke beachten
  (`localhost`-Forwarding von WSL2 zu Windows funktioniert i. d. R. automatisch).
- **Dauerbetrieb:** kein Cloud-Server, alles lokal. Auto-Start beim Login.

## 3. Tech-Stack (Vorschlag — bei besserer Alternative kurz begründen)

| Bereich          | Wahl                                   | Grund |
|------------------|----------------------------------------|-------|
| Backend/API      | **Python 3.11 + FastAPI**              | async, schnell, sauberes API-Schema |
| Scheduler        | **APScheduler** (in-process)           | periodisches Scraping ohne Extra-Dienst |
| Datenhaltung     | **SQLite** (via SQLModel/SQLAlchemy)   | dedup, Verlauf, „schon gesehen"-Status |
| Scraping         | **httpx + selectolax**, bei JS-Seiten **Playwright (Chromium)** | robust, headless möglich |
| Geo/Routing      | siehe [Abschnitt 6](#6-entfernungs--umgebungs-daten) | |
| Frontend         | **HTMX + Jinja2** *oder* schlankes React (SPA) | Vorschlag begründen; HTMX reicht wahrscheinlich |
| Notifications    | Web Notifications API + Desktop-Fallback | siehe [Abschnitt 7](#7-benachrichtigungen) |

Bevorzuge wenige, wartbare Dependencies. Kein schwergewichtiges Framework, wenn HTMX genügt.

## 4. Funktionsumfang

### 4.1 Angebote sammeln
- Periodischer Abruf (Intervall konfigurierbar, Default 15 Min) mehrerer Portale.
- **Deduplizierung** über stabile IDs/URL-Hash; jedes Angebot bekommt Status
  `neu | gesehen | favorisiert | ausgeblendet`.
- Nur Angebote im Suchgebiet München + konfigurierbarer Umkreis.

### 4.2 Anzeige pro Angebot
- **Basis:** Titel, Miete (warm/kalt), Größe/Zimmerzahl, Stadtteil, Portal, Link, Datum.
- **Entfernung/Fahrzeit zur TUM Garching:** zu Fuß, Rad, **ÖPNV** (mit Umstiegen/Dauer).
- **Umgebung (in Gehminuten/Metern):** nächster Supermarkt, Apotheke, ÖPNV-Haltestelle.
- **Score:** gewichtete Gesamtbewertung nach meinen Prioritäten (Uni-Nähe, Miete,
  Größe, Umgebung) — Gewichte in der Config einstellbar.

### 4.3 Interaktion (Frontend)
- Sortierbare/filterbare Tabelle **und** Kartenansicht (Angebote + TUM-Marker).
- Filter: max. Miete, min. Größe, Stadtteile, max. Fahrzeit zur Uni.
- Buttons: *Favorit*, *Ausblenden*, *Öffnen im Portal*.
- Badge/Zähler für neue Angebote seit letztem Besuch.

## 5. Datenquellen & Recht (wichtig)

Ziel-Portale: **WG-Gesucht**, **ImmoScout24**, **Kleinanzeigen** (Priorisierung siehe
Offene Entscheidungen).

**Claude Code muss dabei:**
- Für jede Quelle prüfen und dokumentieren, ob eine **offizielle API** existiert
  (bevorzugt) oder nur Scraping möglich ist.
- **`robots.txt` und AGB respektieren**; bei Verboten die Quelle nicht scrapen,
  sondern mich warnen und legale Alternativen (RSS, offizielle API, manueller
  Import) vorschlagen.
- **Rate-Limits & höfliches Crawling** umsetzen (Delays, Backoff, realistischer
  User-Agent, Caching, nur Änderungen abrufen).
- Scraping-Adapter als **austauschbare Module** (ein Interface pro Portal) bauen,
  damit einzelne Quellen leicht deaktivierbar sind.

Schreibe die rechtlichen Hinweise pro Portal in eine `DATA_SOURCES.md`.

## 6. Entfernungs- & Umgebungs-Daten

- **Geocoding:** OpenStreetMap **Nominatim** (Adresse → Koordinaten), mit lokalem Cache.
- **Routing zu Fuß/Rad:** **OSRM** oder **OpenRouteService** (API-Key).
- **ÖPNV:** MVV/Bayern-GTFS über **OpenTripPlanner** *oder* eine Transit-API;
  falls zu aufwändig, als **Meilenstein 2** kennzeichnen und zunächst Luftlinie +
  Rad-Fahrzeit liefern.
- **POIs (Supermarkt/Apotheke/Haltestelle):** OpenStreetMap **Overpass API**.
- Bevorzugt **kostenlose/Open-Data-Dienste**; wenn Google Maps API vorgeschlagen
  wird, Kosten und Key-Bedarf klar benennen. Alle Geo-Ergebnisse **cachen**
  (Angebote ändern die Adresse nicht).

## 7. Benachrichtigungen

- **Trigger:** neues Angebot, das die aktiven Filter erfüllt.
- **Kanäle:**
  1. **Browser-Notification** (Web Notifications API) im laufenden Chrome-Fenster.
  2. **Desktop-Fallback** (Windows-Toast), falls das Fenster nicht im Vordergrund ist.
  3. Optionaler zusätzlicher Kanal (z. B. Telegram-Bot / E-Mail) als spätere Ausbaustufe.
- Klick auf die Benachrichtigung öffnet das Angebot direkt.
- **Keine Doppel-Benachrichtigungen** (über den `gesehen`-Status).

## 8. Dauerbetrieb & Chrome-Fenster (Windows/WSL2)

- **Ein Startskript** (`start.sh` / `start.ps1`), das:
  1. Backend + Scheduler startet (in WSL2),
  2. wartet, bis der Server erreichbar ist,
  3. **Chrome im App-Modus** öffnet:
     `chrome.exe --app=http://localhost:PORT --new-window`
     (App-Modus = eigenes Fenster ohne Adressleiste, wirkt wie native App).
- **Auto-Start beim Login:** Windows **Task Scheduler** (Trigger „Bei Anmeldung")
  oder Verknüpfung im Autostart-Ordner. Anleitung in die README.
- Backend soll bei fehlender Internetverbindung **sauber pausieren** und beim
  Wiederverbinden weiterlaufen (der Nutzer sagt: „läuft, wenn der Laptop online ist").
- Health-Check-Endpoint (`/health`) + einfaches Logging in Datei.

## 9. Konfiguration

Eine gut kommentierte **`config.yaml`** (oder `.env` für Secrets), u. a.:

```yaml
tum_campus:
  primary: "Boltzmannstraße 3, 85748 Garching"   # Informatik
  secondary: "Arcisstraße 21, 80333 München"      # optional
search:
  max_warm_rent: 900
  min_size_sqm: 15
  districts: ["Garching", "Schwabing", "Maxvorstadt", "Freimann"]
  max_transit_minutes_to_tum: 40
scoring_weights:      # Summe frei, wird normalisiert
  distance_to_tum: 0.4
  rent: 0.3
  size: 0.2
  surroundings: 0.1
sources:
  wg_gesucht: true
  immoscout: true
  kleinanzeigen: true
poll_interval_minutes: 15
```

API-Keys ausschließlich über `.env`, niemals committen (`.gitignore`).

## 10. Projektstruktur (Richtwert)

```
wohnungsradar/
├── app/
│   ├── main.py            # FastAPI, Routes, App-Start
│   ├── scheduler.py       # APScheduler-Jobs
│   ├── sources/           # ein Adapter pro Portal (gemeinsames Interface)
│   ├── enrich/            # Geocoding, Routing, POIs, Scoring
│   ├── notify/            # Browser- & Desktop-Notifications
│   ├── models.py          # SQLModel-Tabellen
│   └── web/               # Templates (HTMX/Jinja) oder Frontend
├── config.yaml
├── .env.example
├── requirements.txt
├── start.sh / start.ps1
├── README.md
└── DATA_SOURCES.md        # rechtliche Hinweise pro Quelle
```

## 11. Qualität

- **Type Hints** durchgehend, `ruff` + `black`, Docstrings an öffentlichen Funktionen.
- **Tests** für Enrichment (Scoring, Distanzberechnung) und den Dedup-Mechanismus.
- Fehler beim Scraping einer Quelle dürfen die App **nicht** crashen (isolierte Adapter).

## 12. Deliverables

1. Kurze **Architektur-Übersicht** mit Begründung der Technologie-Entscheidungen.
2. Vollständiger, **kommentierter, modularer Code**.
3. **`requirements.txt`** und **README** mit Setup-Anleitung (WSL2, Keys, Auto-Start,
   Chrome-App-Modus).
4. **`DATA_SOURCES.md`** mit Rate-Limits & rechtlichen Hinweisen pro Portal.

---

## 8. Umsetzung in Meilensteinen

Committe nach jedem Meilenstein.

- **M0 — Gerüst:** FastAPI-App, SQLite-Modelle, Config-Loader, ein Dummy-Portal-Adapter,
  Frontend mit leerer Tabelle. Startskript + Chrome-App-Modus funktionieren.
- **M1 — Eine echte Quelle:** ein Portal-Adapter (Vorschlag: WG-Gesucht), Scheduler,
  Dedup, Anzeige echter Angebote.
- **M2 — Enrichment:** Geocoding + Fuß/Rad-Distanz zur TUM + POIs + Scoring.
- **M3 — Benachrichtigungen:** Browser- + Desktop-Notification bei neuen Treffern.
- **M4 — Weitere Quellen & Filter/Kartenansicht.**
- **M5 — ÖPNV-Routing** (falls in M2 zurückgestellt) & Auto-Start-Feinschliff.

---

## 0. Offene Entscheidungen (zuerst klären)

Bitte stelle mir diese Fragen, bevor du loslegst:

1. **Portal-Priorität:** Mit welchem Portal soll M1 starten (WG-Gesucht / ImmoScout24 /
   Kleinanzeigen)? Suche ich eher **WG-Zimmer**, **eigene Wohnung** oder beides?
2. **Frontend:** Reicht **HTMX + Server-Templates**, oder willst du ein **React-SPA**?
3. **ÖPNV-Fahrzeit:** Ist die genaue MVV-Fahrzeit von Anfang an wichtig (mehr Aufwand),
   oder reichen zunächst Luftlinie + Rad/Fuß?
4. **Zusätzlicher Melde-Kanal:** Nur im Browser/Desktop, oder zusätzlich **Telegram/E-Mail**?
5. **Budget/Filter-Defaults:** Sind die Beispielwerte in `config.yaml` (≤ 900 € warm,
   ≥ 15 m², Stadtteile) für dich passend oder soll ich sie anpassen?
