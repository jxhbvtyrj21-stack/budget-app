"""Розташування даних користувача (розділ 5.3, DS-9).

Windows: ``%LOCALAPPDATA%\\<DataDirectoryName>``. Назва теки береться з
``product.toml``. Змінна ``BUDGET_DATA_DIR`` перевизначає корінь для тестів і
розробки; на інших ОС (лише розробка й CI) використовується ``XDG_DATA_HOME``.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from budget.errors import StartupError
from budget.platform.identity import ProductIdentity

DATA_DIR_OVERRIDE_ENV = "BUDGET_DATA_DIR"


@dataclass(frozen=True, slots=True)
class DataPaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "budget.db"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def lock(self) -> Path:
        return self.root / "budget.lock"


def data_paths(identity: ProductIdentity) -> DataPaths:
    override = os.environ.get(DATA_DIR_OVERRIDE_ENV)
    if override:
        return DataPaths(Path(override))
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise StartupError(detail="Змінна LOCALAPPDATA не визначена")
        base = Path(local_app_data)
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return DataPaths(base / identity.data_directory_name)
