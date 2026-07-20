// Kartenansicht: Angebote als farbige Marker + TUM-Campus.
//
// Die Angebote kommen als JSON von /api/listings (gleiche Filterung wie die
// Tabelle). Nur bereits geocodierte Angebote (mit lat/lon) erscheinen als Marker;
// die Farbe folgt dem Score (rot = niedrig … grün = hoch), analog zur Tabelle.

(() => {
  "use strict";

  const MUNICH_CENTER = [48.1372, 11.5756];

  // Farbe aus Score (0..100) — gleiche Logik wie das Score-Badge im CSS.
  function scoreColor(score) {
    if (score === null || score === undefined) return "#9aa3ad";
    return `hsl(${score * 1.2} 65% 60%)`;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text ?? "";
    return div.innerHTML;
  }

  function popupHtml(listing) {
    const rent = listing.rent_warm ? `${Math.round(listing.rent_warm)} € warm` : "Miete k. A.";
    const size = listing.size_sqm ? ` · ${Math.round(listing.size_sqm)} m²` : "";
    const transit =
      listing.transit_minutes != null
        ? `<br>🚇 ${Math.round(listing.transit_minutes)} min zur TUM` +
          (listing.transit_summary ? ` (${escapeHtml(listing.transit_summary)})` : "")
        : "";
    const score = listing.score != null ? `<br>Score ${Math.round(listing.score)}` : "";
    return (
      `<b>${escapeHtml(listing.title)}</b><br>` +
      `${rent}${size}<br>` +
      `${escapeHtml(listing.district || "")}${transit}${score}<br>` +
      `<a href="${escapeHtml(listing.url)}" target="_blank" rel="noopener">Im Portal öffnen ↗</a>`
    );
  }

  async function loadListings(map) {
    let listings = [];
    try {
      const response = await fetch("/api/listings");
      listings = await response.json();
    } catch (error) {
      console.error("Angebote konnten nicht geladen werden:", error);
      return;
    }

    const bounds = [];
    let shown = 0;
    for (const listing of listings) {
      if (listing.lat == null || listing.lon == null) continue;
      shown += 1;
      const marker = L.circleMarker([listing.lat, listing.lon], {
        radius: 8,
        color: "#333",
        weight: 1,
        fillColor: scoreColor(listing.score),
        fillOpacity: 0.85,
      });
      marker.bindPopup(popupHtml(listing));
      marker.addTo(map);
      bounds.push([listing.lat, listing.lon]);
    }

    const counter = document.getElementById("map-count");
    if (counter) counter.textContent = `· ${shown} Angebote auf der Karte`;

    return bounds;
  }

  function addCampusMarkers(map, bounds) {
    let campus = [];
    try {
      campus = JSON.parse(document.getElementById("campus-data").textContent);
    } catch (error) {
      return;
    }
    for (const point of campus) {
      if (point.lat == null || point.lon == null) continue;
      // Auffälliger Diamant-Marker für den Campus, klar abgesetzt von den Angeboten.
      L.marker([point.lat, point.lon], {
        icon: L.divIcon({
          className: "campus-marker",
          html: "◆",
          iconSize: [24, 24],
          iconAnchor: [12, 12],
        }),
        zIndexOffset: 1000,
      })
        .bindPopup(`<b>${point.label}</b>`)
        .addTo(map);
      bounds.push([point.lat, point.lon]);
    }
  }

  async function init() {
    const map = L.map("map").setView(MUNICH_CENTER, 11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap-Mitwirkende",
    }).addTo(map);

    const bounds = (await loadListings(map)) || [];
    addCampusMarkers(map, bounds);

    // Kartenausschnitt an alle Marker anpassen, falls vorhanden.
    if (bounds.length > 0) {
      map.fitBounds(bounds, { padding: [40, 40] });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
