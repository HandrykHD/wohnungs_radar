"""Logging-Konfiguration: Konsole + rotierende Logdatei."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"


def setup_logging(log_path: Path, level: str = "INFO") -> None:
    """Root-Logger einrichten.

    Die Datei rotiert bei 2 MB (5 Sicherungen), damit der Dauerbetrieb die
    Festplatte nicht über Wochen vollschreibt.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Bei Reload (uvicorn --reload) sonst doppelte Ausgaben.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # APScheduler protokolliert jeden Job-Start auf INFO — bei 15-Minuten-Takt
    # ist das nur Rauschen.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
