// Browser-Benachrichtigungen + Heartbeat für den Wohnungs-Radar.
//
// Aufgaben:
//   1. Beim ersten Klick die Notification-Berechtigung erfragen (Chrome verlangt
//      eine Nutzergeste; ein Auto-Request beim Laden würde ignoriert).
//   2. Regelmäßig einen Heartbeat mit Sichtbarkeitsstatus senden — daran erkennt
//      das Backend, ob es in den Browser oder als Desktop-Toast melden soll.
//   3. Wartende Meldungen abholen und als Web-Notification anzeigen; ein Klick
//      öffnet das Angebot im Portal.

(() => {
  "use strict";

  const HEARTBEAT_MS = 20000; // Fenster-Lebenszeichen (< heartbeat_stale_seconds)
  const POLL_MS = 15000; // Abholintervall für wartende Browser-Meldungen

  async function sendHeartbeat() {
    const visible = document.visibilityState === "visible";
    try {
      await fetch(`/api/heartbeat?visible=${visible}`, { method: "POST" });
    } catch (error) {
      // Backend evtl. kurz nicht erreichbar — beim nächsten Tick erneut.
    }
  }

  function showNotification(item) {
    if (Notification.permission !== "granted") return;
    const notification = new Notification(item.title, {
      body: item.body,
      tag: `listing-${item.listing_id}`, // dieselbe Anzeige nie doppelt stapeln
      renotify: false,
    });
    notification.onclick = () => {
      window.open(item.url, "_blank", "noopener");
      notification.close();
    };
  }

  async function pollPending() {
    // Ohne Erlaubnis gar nicht erst abholen — sonst gingen Meldungen verloren,
    // bevor der Nutzer die Berechtigung erteilt.
    if (Notification.permission !== "granted") return;
    try {
      const response = await fetch("/api/notifications/pending");
      const items = await response.json();
      for (const item of items) showNotification(item);
    } catch (error) {
      // still ignorieren, nächster Poll folgt
    }
  }

  function requestPermissionOnce() {
    if (Notification.permission === "default") {
      Notification.requestPermission();
    }
  }

  function init() {
    if (!("Notification" in window)) return; // sehr alter Browser

    // Berechtigung bei der ersten Nutzergeste erfragen (Klick reicht).
    document.addEventListener("click", requestPermissionOnce, { once: true });

    sendHeartbeat();
    setInterval(sendHeartbeat, HEARTBEAT_MS);
    // Bei Sichtbarkeitswechsel sofort melden, damit die Kanalwahl aktuell ist.
    document.addEventListener("visibilitychange", sendHeartbeat);

    setInterval(pollPending, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
