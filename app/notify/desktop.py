"""Desktop-Kanal: Windows-Toast aus WSL2 heraus über ``powershell.exe``.

Genutzt wird ``System.Windows.Forms.NotifyIcon`` — das funktioniert auf Windows
10/11 ohne zusätzliche PowerShell-Module (anders als z.B. BurntToast). Der Toast
ist bewusst rein informativ; das Klick-zum-Öffnen übernimmt der Browser-Kanal.

Der PowerShell-Prozess lebt für die Anzeigedauer (~8 s) und wird **entkoppelt**
gestartet, damit :meth:`DesktopChannel.send` sofort zurückkehrt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.notify.base import Notification, NotificationChannel

logger = logging.getLogger(__name__)

# Vom WSL2-PATH auflösbar (Windows-Interop); Fallback auf den Standardpfad.
_POWERSHELL_CANDIDATES = (
    "powershell.exe",
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
)

_DISPLAY_SECONDS = 8


def _ps_literal(text: str) -> str:
    """String für ein einfach-gequotetes PowerShell-Literal absichern.

    In PowerShell wird ein einfaches Anführungszeichen durch Verdoppeln escaped.
    Steuerzeichen werden entfernt, damit das Skript nicht zerbricht.
    """
    cleaned = "".join(ch for ch in text if ch >= " ").replace("'", "''")
    # Länge begrenzen — Balloon-Text wird sonst ohnehin abgeschnitten.
    return cleaned[:200]


def _build_script(notification: Notification) -> str:
    """PowerShell-Skript für einen Balloon-Toast zusammenbauen."""
    title = _ps_literal(notification.title)
    body = _ps_literal(notification.body)
    return (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle = '{title}';"
        f"$n.BalloonTipText = '{body}';"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip({_DISPLAY_SECONDS * 1000});"
        f"Start-Sleep -Seconds {_DISPLAY_SECONDS};"
        "$n.Dispose()"
    )


class DesktopChannel(NotificationChannel):
    """Zeigt einen Windows-Balloon/Toast über PowerShell an."""

    name = "desktop"

    def __init__(self) -> None:
        # Sobald ein Kandidat funktioniert, wird er gemerkt; schlägt alles fehl,
        # wird der Kanal stillgelegt, um nicht bei jeder Meldung neu zu scheitern.
        self._executable: str | None = None
        self._disabled = False

    async def send(self, notification: Notification) -> bool:
        if self._disabled:
            return False

        script = _build_script(notification)
        candidates = [self._executable] if self._executable else list(_POWERSHELL_CANDIDATES)

        for candidate in candidates:
            if candidate is None:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    candidate,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except (FileNotFoundError, OSError):
                continue

            self._executable = candidate
            # Prozess läuft ~8 s weiter; nicht darauf warten, aber später ernten,
            # damit kein Zombie zurückbleibt.
            asyncio.create_task(_reap(proc))  # noqa: RUF006
            return True

        logger.warning("powershell.exe nicht gefunden — Desktop-Benachrichtigungen deaktiviert.")
        self._disabled = True
        return False


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Auf das Prozessende warten, Fehler ignorieren (Aufräumen ohne Blockieren)."""
    with contextlib.suppress(Exception):
        await proc.wait()
