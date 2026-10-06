"""Журнал застосунку: технічні подробиці помилок пишуться сюди, а не в інтерфейс."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "budget.log"


def configure_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / LOG_FILE_NAME, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
