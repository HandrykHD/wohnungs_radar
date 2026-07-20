#!/usr/bin/env bash
#
# Wohnungs-Radar starten (WSL2).
#
#   1. Backend + Scheduler in WSL2 hochfahren
#   2. warten, bis /health antwortet
#   3. Chrome auf dem Windows-Host im App-Modus öffnen
#
# Aufruf:  ./start.sh            (startet Backend und Chrome)
#          ./start.sh --no-browser   (nur Backend, z.B. für Entwicklung)
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

OPEN_BROWSER=1
[[ "${1:-}" == "--no-browser" ]] && OPEN_BROWSER=0

# --- Port aus config.yaml lesen (Fallback 8765) ------------------------------
PORT="$(grep -E '^\s+port:' config.yaml | head -1 | grep -oE '[0-9]+' || true)"
PORT="${PORT:-8765}"
URL="http://localhost:${PORT}"

# --- Virtualenv sicherstellen ------------------------------------------------
if [[ ! -d .venv ]]; then
  echo "→ Lege virtuelle Umgebung an …"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [[ ! -f .env ]]; then
  echo "⚠  Keine .env gefunden. Kopiere .env.example nach .env und trage deine"
  echo "   Kontaktadresse ein (OSM_CONTACT_EMAIL) — Nominatim verlangt das."
fi

# --- Backend starten ---------------------------------------------------------
# Bewusst 0.0.0.0 statt 127.0.0.1: Chrome läuft auf dem Windows-Host. Das
# localhost-Forwarding von WSL2 greift zwar meist, versagt aber je nach
# Windows-Version und Netzwerkprofil. 0.0.0.0 macht den Start verlässlich —
# die WSL2-VM ist von außen ohnehin nicht erreichbar.
echo "→ Starte Backend auf ${URL} …"
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" &
BACKEND_PID=$!

# Backend beenden, wenn dieses Skript endet (Strg+C, Fehler, regulärer Ausstieg).
cleanup() {
  echo ""
  echo "→ Beende Backend (PID ${BACKEND_PID}) …"
  kill "${BACKEND_PID}" 2>/dev/null || true
  wait "${BACKEND_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- Auf Erreichbarkeit warten ----------------------------------------------
echo -n "→ Warte auf /health "
for _ in $(seq 1 40); do
  if curl -sf "${URL}/health" >/dev/null 2>&1; then
    echo " bereit."
    break
  fi
  # Wenn das Backend schon gestorben ist, hat Warten keinen Zweck.
  if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
    echo ""
    echo "✗ Backend ist beim Start abgestürzt. Siehe Ausgabe oben und logs/wohnungsradar.log"
    exit 1
  fi
  echo -n "."
  sleep 0.5
done

if ! curl -sf "${URL}/health" >/dev/null 2>&1; then
  echo ""
  echo "✗ Backend antwortet nach 20 s nicht. Abbruch."
  exit 1
fi

# --- Chrome im App-Modus öffnen ---------------------------------------------
if [[ "${OPEN_BROWSER}" -eq 1 ]]; then
  CHROME="/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
  [[ -f "${CHROME}" ]] || CHROME="/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"

  if [[ -f "${CHROME}" ]]; then
    echo "→ Öffne Chrome im App-Modus …"
    # Eigenes Nutzerprofil: so bleibt die Notification-Berechtigung dauerhaft
    # erteilt und das Fenster mischt sich nicht unter die normalen Tabs.
    # wslpath übersetzt den Linux- in einen Windows-Pfad — chrome.exe läuft auf
    # dem Host und kann mit /mnt/c/... nichts anfangen.
    mkdir -p .chrome-profile
    PROFILE_DIR="$(wslpath -w "$(pwd)/.chrome-profile")"
    "${CHROME}" \
      --app="${URL}" \
      --new-window \
      --user-data-dir="${PROFILE_DIR}" \
      >/dev/null 2>&1 &
  else
    echo "⚠  chrome.exe nicht gefunden. Öffne manuell: ${URL}"
  fi
fi

echo ""
echo "Wohnungs-Radar läuft auf ${URL} — Strg+C beendet ihn."
wait "${BACKEND_PID}"
