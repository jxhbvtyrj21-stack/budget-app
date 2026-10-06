"""Резервні копії й відновлення (DS-5, DS-6).

Повна ротація (7 щоденних + 4 щотижневі + 12 щомісячних) і сценарій відновлення в
інтерфейсі реалізуються на наступних етапах.
"""

import sqlite3
from pathlib import Path

from budget.domain.calendar import Clock
from budget.storage.backup import backup_database
from budget.storage.recovery import quarantine_database, restore_from_backup

BACKUP_SUFFIX = ".db"


def _timestamp(clock: Clock) -> str:
    return clock.now().strftime("%Y%m%d-%H%M%S")


class BackupService:
    def __init__(self, connection: sqlite3.Connection, backups_dir: Path, clock: Clock) -> None:
        self._connection = connection
        self._backups_dir = backups_dir
        self._clock = clock

    def create_backup(self, reason: str) -> Path:
        """Створює перевірену копію; ``reason`` — вид копії (daily, migration, manual…)."""
        name = f"budget-{_timestamp(self._clock)}-{reason}{BACKUP_SUFFIX}"
        return backup_database(self._connection, self._backups_dir / name)

    def list_backups(self) -> list[Path]:
        if not self._backups_dir.is_dir():
            return []
        return sorted(self._backups_dir.glob(f"budget-*{BACKUP_SUFFIX}"), reverse=True)


class RecoveryService:
    def __init__(self, database_path: Path, clock: Clock) -> None:
        self._database_path = database_path
        self._clock = clock

    def quarantine_corrupted(self) -> Path:
        """Зберігає пошкоджену базу під новою назвою, нічого в неї не записуючи."""
        return quarantine_database(self._database_path, _timestamp(self._clock))

    def restore(self, backup_path: Path) -> None:
        restore_from_backup(backup_path, self._database_path)
