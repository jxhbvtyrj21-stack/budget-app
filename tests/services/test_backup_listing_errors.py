"""Перелік копій не прочитано (F9): невідомий стан теки — помилка, а не «копій немає».

Теки немає чи вона порожня — ``[]``, як і раніше. ``OSError`` читання теки чи перевірки
запису доходить до викликача: ``find_backups`` її передає, ``restore_candidates`` дає
``StorageError`` із причиною, ``run_automatic`` пише в журнал і нічого не створює й не
видаляє. Відмову в доступі імітує підміна ``os.scandir``: тести можуть іти від root.
"""

import errno
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.services.backup import (
    BACKUPS_UNREADABLE_MESSAGE,
    BackupService,
    find_backups,
    restore_candidates,
)
from budget.services.startup import prepare_database, startup_recovery_indicators

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def setup(tmp_path, clock):
    """База з трьома автоматичними копіями (щоденна, тижнева, місячна)."""
    backups_dir = tmp_path / "data" / "backups"
    connection = prepare_database(tmp_path / "data" / "budget.db", backups_dir, clock)
    service = BackupService(connection, backups_dir, clock)
    service.run_automatic()
    yield service, backups_dir
    connection.close()


def names(folder) -> set[str]:
    return {p.name for p in folder.iterdir()}


class BrokenListing:
    """Читання теки, що обривається ``EIO`` після першого запису; закриття фіксується."""

    def __init__(self, listing) -> None:
        self._listing = listing
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._listing.close()
        self.closed = True

    def __iter__(self):
        yield next(iter(self._listing))
        raise OSError(errno.EIO, "помилка введення-виведення")


class Entry:
    """Запис теки, перевірка якого (``is_file``) дає відмову в доступі."""

    def __init__(self, entry) -> None:
        self.name, self.path = entry.name, entry.path

    def is_file(self) -> bool:
        raise PermissionError(errno.EACCES, "доступ заборонено", self.path)


def unreadable(monkeypatch, target, mode: str = "open", *, skip: int = 0):
    """Підміняє ``os.scandir`` для ``target``: ``open`` — відмова відкрити теку, ``broken`` —
    обрив під час читання, ``entry`` — відмова перевірити записи. ``skip`` перших викликів
    для ``target`` проходять звичайно (напр., попередня перевірка під час запуску)."""
    original = os.scandir
    calls = {"count": 0, "broken": []}

    def scandir(path="."):
        if os.fspath(path) != os.fspath(target):
            return original(path)
        calls["count"] += 1
        if calls["count"] <= skip:
            return original(path)
        if mode == "open":
            raise PermissionError(errno.EACCES, "доступ заборонено", os.fspath(path))
        if mode == "broken":
            listing = BrokenListing(original(path))
            calls["broken"].append(listing)
            return listing
        with original(path) as entries:
            items = [Entry(e) for e in entries]

        class Listing(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

        return Listing(items)

    monkeypatch.setattr(os, "scandir", scandir)
    return calls


# find_backups ------------------------------------------------------------------------------


def test_missing_directory_means_no_backups(tmp_path):
    assert find_backups(tmp_path / "немає") == []


def test_empty_directory_means_no_backups(tmp_path):
    (tmp_path / "backups").mkdir()
    assert find_backups(tmp_path / "backups") == []


def test_unreadable_directory_is_an_error_not_an_empty_list(setup, monkeypatch):
    _, backups_dir = setup
    assert len(find_backups(backups_dir)) == 3
    unreadable(monkeypatch, backups_dir)
    with pytest.raises(PermissionError):
        find_backups(backups_dir)


def test_error_while_reading_gives_no_partial_list_and_closes_the_listing(setup, monkeypatch):
    _, backups_dir = setup
    calls = unreadable(monkeypatch, backups_dir, "broken")
    with pytest.raises(OSError) as raised:
        find_backups(backups_dir)
    assert raised.value.errno == errno.EIO
    assert [listing.closed for listing in calls["broken"]] == [True]


def test_entry_check_error_is_not_hidden(setup, monkeypatch):
    _, backups_dir = setup
    unreadable(monkeypatch, backups_dir, "entry")
    with pytest.raises(PermissionError):
        find_backups(backups_dir)


# run_automatic -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["open", "broken", "entry"])
def test_automatic_backups_skip_unknown_listing_without_creating_or_deleting(
    setup, clock, monkeypatch, caplog, mode
):
    service, backups_dir = setup
    clock.set(START + timedelta(days=40))  # потрібні нові щоденна, тижнева й місячна
    before = names(backups_dir)
    unreadable(monkeypatch, backups_dir, mode)
    with caplog.at_level(logging.ERROR):
        assert service.run_automatic() == []  # запуск не зупиняється
    monkeypatch.undo()
    assert names(backups_dir) == before  # нічого не створено й не видалено
    records = [r for r in caplog.records if "Перелік копій не прочитано" in r.getMessage()]
    assert len(records) == 3 and all(r.exc_info for r in records)


# restore_candidates ------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["open", "broken", "entry"])
def test_restore_candidates_report_an_unreadable_listing(setup, monkeypatch, mode):
    _, backups_dir = setup
    unreadable(monkeypatch, backups_dir, mode)
    with pytest.raises(StorageError) as raised:
        restore_candidates(backups_dir)
    error = raised.value
    assert type(error) is StorageError and not isinstance(error, DatabaseCorruptedError)
    assert error.user_message == BACKUPS_UNREADABLE_MESSAGE
    assert BACKUPS_UNREADABLE_MESSAGE == "Не вдалося прочитати теку резервних копій."
    assert isinstance(error.__cause__, OSError)


def test_service_candidates_use_the_same_rule(setup, monkeypatch):
    service, backups_dir = setup
    unreadable(monkeypatch, backups_dir)
    with pytest.raises(StorageError) as raised:
        service.candidates()
    assert raised.value.user_message == BACKUPS_UNREADABLE_MESSAGE


# Запуск без бази ---------------------------------------------------------------------------


def test_startup_indicators_do_not_turn_a_failed_listing_into_no_backups(tmp_path, monkeypatch):
    """Попередня перевірка ``backups/`` пройшла, а читання переліку — ні (мікровікно R1.1):
    ``OSError`` для контрольованого виходу, а не «ознак немає»."""
    root = tmp_path / "data"
    backups_dir = root / "backups"
    backups_dir.mkdir(parents=True)  # робочої бази немає, ознака — лише копія
    (backups_dir / "budget-20261005-120000-daily.db").write_bytes(b"copy")
    assert startup_recovery_indicators(root, root / "budget.db", backups_dir)
    calls = unreadable(monkeypatch, backups_dir, skip=1)
    with pytest.raises(PermissionError):
        startup_recovery_indicators(root, root / "budget.db", backups_dir)
    assert calls["count"] == 2  # попередня перевірка й сам перелік
