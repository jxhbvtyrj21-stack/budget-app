"""Блокування від запуску другої копії (DS-8)."""

from pathlib import Path

from PySide6.QtCore import QLockFile


def acquire_single_instance(lock_path: Path) -> QLockFile | None:
    """Повертає утримуване блокування або ``None``, якщо застосунок уже запущено."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return None
    return lock
