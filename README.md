# Wohnungs- & WG-Radar München

Lokal laufende Web-App, die Wohnungs- und WG-Angebote in München sammelt, sie mit
Entfernungs- und Umgebungsdaten (v. a. zur **TUM Garching**) anreichert und bei
neuen passenden Treffern benachrichtigt. Alles läuft auf dem eigenen Laptop
(WSL2), die Oberfläche öffnet sich in einem Chrome-Fenster im App-Modus.

> ⚠️ **Rechtlicher Hinweis vorab:** Die AGB der Zielportale untersagen
> automatisierten Abruf. Dieses Werkzeug ist für **privaten Gebrauch in geringem
> Umfang** gedacht. Lies unbedingt [`DATA_SOURCES.md`](DATA_SOURCES.md), bevor du
> eine echte Quelle aktivierst. Die Verantwortung liegt bei dir.

---

## Aktueller Stand

| Meilenstein | Inhalt | Status |
|-------------|--------|--------|
| **M0** | Gerüst: FastAPI, SQLite, Config, Dummy-Adapter, HTMX-Tabelle, Startskripte | ✅ fertig |
| **M1** | WG-Gesucht-Adapter (WG-Zimmer + Wohnungen), Scheduler, Dedup | ✅ fertig |
| **M2** | Geocoding, Fuß-/Rad-Schätzung, ÖPNV zur TUM, POIs, Scoring | ✅ fertig |
| M3 | Browser- & Desktop-Benachrichtigungen | geplant |
| M4 | Weitere Quellen, Filter, Kartenansicht | geplant |
| M5 | ÖPNV-Feinschliff & Auto-Start | geplant |

Aktiv ist der **WG-Gesucht-Adapter**; jedes neue Angebot wird im Hintergrund
angereichert (Koordinaten, Fuß-/Rad-/ÖPNV-Zeit zur TUM Garching, Umgebungs-POIs,
Score). Der Dummy-Adapter bleibt für Entwicklung/Tests verfügbar (in
`config.yaml` umschaltbar).

**Ablauf eines Laufs:** Der Scheduler sammelt alle 15 min neue Angebote und
reichert danach einen Schwung (`enrichment.max_per_run`, Default 20) an. Die
Anreicherung ist bewusst langsam getaktet (Nominatim erlaubt nur 1 Anfrage/s),
läuft daher im Hintergrund weiter — der „Jetzt suchen"-Button kehrt sofort mit
der Trefferzahl zurück, die Scores/Zeiten erscheinen nach und nach.

---

## Architektur — Kurzüberblick

```
Chrome (Windows-Host, App-Modus)
        │  http://localhost:8765
        ▼
FastAPI  ── HTMX-Templates (Server-Rendering, kein Build-Step)
   │
   ├── APScheduler ──► Sammel-Lauf alle 15 min
   │                     └── Portal-Adapter (austauschbar, fehlerisoliert)
   │
   ├── Enrichment (ab M2): Nominatim · OSRM · Overpass · HAFAS  ──► GeoCache
   │
   └── SQLite (SQLModel) ── Angebote · Dedup · Status · Cache
```

**Warum diese Entscheidungen?**

- **FastAPI + HTMX statt React:** Die Oberfläche ist eine gefilterte Tabelle mit
  ein paar Buttons. HTMX rendert das serverseitig ohne npm, Build-Schritt oder
  Client-State — weniger Code, weniger Wartung, funktioniert offline. Ein
  React-SPA würde hier nur Komplexität ohne Mehrwert bringen.
- **SQLite:** Einzelnutzer, lokal, kein Server nötig. WAL-Modus erlaubt
  gleichzeitiges Lesen (Web) und Schreiben (Scheduler).
- **APScheduler in-process:** Periodisches Scraping ohne zusätzlichen Dienst
  (kein cron, kein Celery).
- **Adapter-Interface pro Portal:** Jede Quelle ist ein Modul, einzeln in der
  Config abschaltbar; ein Fehler in einer Quelle kann die App nicht abstürzen
  lassen (`run_adapter_safely`).
- **Aggressives Geo-Caching:** Adressen ändern sich nicht — jede teure externe
  Abfrage wird genau einmal gemacht.

---

## Voraussetzungen

- Windows mit **WSL2 (Ubuntu)**
- **Python 3.11+** in WSL2 (`python3 --version`)
- **Google Chrome** auf dem Windows-Host (Standardpfad wird automatisch gefunden)

---

## Einrichtung

Alles in der **WSL2-Shell**, im Projektverzeichnis:

```bash
# 1. Secrets-Vorlage kopieren und ausfüllen
cp .env.example .env
nano .env          # OSM_CONTACT_EMAIL eintragen — Nominatim verlangt eine echte Adresse

# 2. Starten (legt beim ersten Mal automatisch die virtuelle Umgebung an)
./start.sh
```

`start.sh` fährt das Backend hoch, wartet auf `/health` und öffnet Chrome im
App-Modus auf `http://localhost:8765`. **Strg+C** beendet alles wieder.

Nur Backend, ohne Browser (z. B. für Entwicklung):

```bash
./start.sh --no-browser
```

### Manuell (ohne Startskript)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

---

## Bedienung

- **Tabelle:** sortier- und filterbar (Miete, Größe, Stadtteile, ÖPNV-Zeit, Typ).
  Filter greifen sofort, ohne Seiten-Neuladen (HTMX).
- **☆ / ★** — Favorit an/aus.
- **✕** — Angebot ausblenden (über „Ausgeblendete zeigen" wieder sichtbar).
- **↗** — Angebot im Portal öffnen.
- **„Jetzt suchen"** — Sammel-Lauf sofort auslösen, statt aufs Intervall zu warten.
- **Badge „N neu"** — so viele Angebote sind seit deinem letzten Besuch dazugekommen.

---

## Konfiguration

Alle nicht-geheimen Einstellungen stehen kommentiert in
[`config.yaml`](config.yaml): Referenzpunkte (TUM Garching/Arcisstraße),
getrennte Budget-/Größenlimits für WG-Zimmer und Wohnungen, Stadtteile,
Scoring-Gewichte, aktive Quellen, Poll-Intervall und höfliche Crawling-Parameter.

**Secrets** (`OSM_CONTACT_EMAIL`, optionale API-Keys) ausschließlich in `.env` —
diese Datei ist in `.gitignore` und wird nie committet.

Quelle aktivieren: in `config.yaml` unter `sources:` den Schalter auf `true`
setzen. In M0 ist nur `dummy: true` sinnvoll; die echten Adapter kommen ab M1.

---

## Geo-Dienste

Ausschließlich kostenlose Open-Data-Dienste ohne Pflicht-Key; **alle** Ergebnisse
werden dauerhaft in der DB gecacht (Adressen ändern sich nicht):

- **Nominatim** (OpenStreetMap) — Adresse → Koordinaten + Stadtteil. Max. 1
  Anfrage/Sekunde, echter User-Agent mit Kontakt Pflicht (daher
  `OSM_CONTACT_EMAIL` in `.env`).
- **Fuß-/Rad-Zeit** — geschätzt aus der Luftlinie (Umwegfaktor + realistische
  Geschwindigkeiten, konfigurierbar unter `enrichment`). Grund: Der öffentliche
  OSRM-Demo-Server kann nur das Auto-Profil und liefert für Fuß/Rad heimlich
  Autozeiten. Für **exakte** Werte einen kostenlosen `ORS_API_KEY`
  (OpenRouteService) in `.env` hinterlegen — dann werden echte Routen abgefragt.
- **MVG-API** — echte ÖPNV-Verbindung (Tür-zu-Tür, Umstiege, Fahrzeit) zur TUM
  Garching. Offizieller Münchner Anbieter, kein Key. (Ursprünglich war eine
  HAFAS-API geplant; sie war dauerhaft nicht erreichbar — Details in
  `DATA_SOURCES.md`.)
- **Overpass** (OpenStreetMap) — nächster Supermarkt, Apotheke, ÖPNV-Haltestelle.
  Stark ausgelasteter Gratis-Dienst; die App pausiert vor jeder Anfrage und
  weicht bei Überlast auf einen Spiegel aus.

Google Maps wird bewusst **nicht** genutzt (kostenpflichtig, API-Key nötig).

Alle Geo-Parameter (Dienst-URLs, Suchradius, Cache-Dauer, Geschwindigkeiten)
stehen kommentiert in `config.yaml` unter `geo` und `enrichment`.

---

## Auto-Start beim Login (Windows Task Scheduler)

Wird in **M5** finalisiert. Grundprinzip:

1. Task Scheduler öffnen → *Aufgabe erstellen*.
2. Trigger: *Bei Anmeldung*.
3. Aktion: *Programm starten*
   - Programm: `powershell.exe`
   - Argumente: `-WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Pfad\zu\wohnungs_radar\start.ps1"`
4. Häkchen *„Nur ausführen, wenn Benutzer angemeldet ist"*.

Alternativ eine Verknüpfung auf `start.ps1` in den Autostart-Ordner
(`shell:startup`) legen.

---

## Betrieb & Robustheit

- **Offline:** Ohne Internet überspringt der Scheduler den Lauf sauber (kein
  Absturz, kein Log-Spam) und macht beim nächsten Intervall weiter.
- **Health-Check:** `GET /health` liefert Status, Angebotszahl und den Zeitpunkt
  des letzten Laufs.
- **Logs:** `logs/wohnungsradar.log` (rotierend, 5 × 2 MB).
- **Adapter-Fehler** (Layout-Änderung, Bot-Wall) werden isoliert und geloggt,
  ohne die App oder andere Quellen zu beeinträchtigen.

---

## Entwicklung

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest          # Tests (Dedup, Scoring, Distanz)
.venv/bin/ruff check app tests
.venv/bin/black app tests
```

## Projektstruktur

```
app/
├── main.py            FastAPI-Routen, App-Start (lifespan)
├── config.py          config.yaml + .env laden und validieren
├── models.py          SQLModel-Tabellen, Fingerprint, Zeit-Helfer
├── db.py              Engine, Sessions, Upsert/Dedup, App-Zustand
├── queries.py         Filter- und Sortierlogik (geteilt: Web/API/Notify)
├── scheduler.py       APScheduler-Job, Online-Check, Sammel-Lauf
├── logging_setup.py   Konsole + rotierende Logdatei
├── sources/           Portal-Adapter (base, dummy, wg_gesucht …)
├── enrich/            cache, geocode, routing, transit (MVG), pois, scoring, pipeline
├── notify/            Benachrichtigungen (ab M3)
└── web/               HTMX-Templates + statische Dateien
config.yaml · .env.example · requirements*.txt · start.sh · start.ps1
DATA_SOURCES.md · tests/
```
