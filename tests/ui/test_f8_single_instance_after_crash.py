"""F8-E3: блокування одного екземпляра після аварійного завершення власника.

Справжній ``acquire_single_instance`` (справжній ``QLockFile``) у дочірніх процесах на шляху
в тимчасовій теці тесту. ``Popen.pid`` не обов'язково PID процесу Python: на Windows
``sys.executable`` у venv (uv) — запускач, що стартує інтерпретатор окремим процесом. Тому
дочірній процес сам повідомляє ``locked <os.getpid()>`` — лише після того, як блокування
взято, — і саме з цим PID звіряється файл блокування (перший рядок ``QLockFile`` — PID).

Аварійне завершення — примусове завершення саме цього процесу без ``unlock`` і деструкторів
Qt: SIGKILL на POSIX, на Windows ``os.kill`` викликає ``TerminateProcess``. Запускач
завершується слідом за своїм процесом; на нього чекаємо ``wait`` з тайм-аутом. Потім
блокування бере другий, незалежний процес.
"""

import os
import signal
import subprocess
import sys

from budget.platform.single_instance import acquire_single_instance

CHILD = """
import os
import sys
from pathlib import Path
from budget.platform.single_instance import acquire_single_instance

lock = acquire_single_instance(Path(sys.argv[1]))
print(f"locked {os.getpid()}" if lock is not None else "busy", flush=True)
sys.stdin.readline()  # тримає блокування, доки батьківський процес не дасть знак
if lock is not None:
    lock.unlock()
"""
# Без обробника й без очищення: SIGKILL на POSIX; на Windows будь-який сигнал, крім
# CTRL_*_EVENT, — TerminateProcess.
CRASH = getattr(signal, "SIGKILL", signal.SIGTERM)
TIMEOUT = 30


def start_owner(lock_path) -> tuple[subprocess.Popen, int]:
    """Процес, що взяв блокування, і PID процесу Python, який його тримає."""
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    reply = process.stdout.readline().split()
    if len(reply) != 2 or reply[0] != "locked":
        stop(process, None)
        raise AssertionError(f"Дочірній процес не взяв блокування: {reply}")
    return process, int(reply[1])


def stop(process: subprocess.Popen, owner_pid: int | None) -> None:
    """Прибирає дочірній процес, навіть якщо тест упав: спершу власника блокування (поки
    живий запускач, PID ще належить саме йому), потім запускач."""
    if process.poll() is None:
        if owner_pid is not None and owner_pid != process.pid:
            try:
                os.kill(owner_pid, CRASH)
            except OSError:
                pass  # уже завершився
        process.kill()
        process.wait(timeout=TIMEOUT)
    process.stdin.close()
    process.stdout.close()


def lock_owner(lock_path) -> str:
    return lock_path.read_text(encoding="utf-8").splitlines()[0]


def test_lock_is_acquired_again_after_the_owner_is_killed(tmp_path):
    lock_path = tmp_path / "isolated" / "budget.lock"
    first, first_pid = start_owner(lock_path)
    try:
        # Власник живий: блокування зайняте, файл не видаляється як застарілий, а в ньому —
        # PID саме того процесу Python, що тримає блокування.
        assert acquire_single_instance(lock_path) is None
        assert lock_owner(lock_path) == str(first_pid)
        # Аварійне завершення саме власника; запускач (якщо він є) завершується слідом.
        os.kill(first_pid, CRASH)
        returncode = first.wait(timeout=TIMEOUT)
        assert returncode != 0
        if first.pid == first_pid and os.name == "posix":
            assert returncode == -CRASH  # убито саме сигналом, без штатного завершення
    finally:
        stop(first, first_pid)

    # Аварійне завершення не прибрало файл: штатного unlock не було.
    assert lock_owner(lock_path) == str(first_pid)

    # Другий, незалежний процес бере те саме блокування.
    second, second_pid = start_owner(lock_path)
    try:
        assert second_pid != first_pid
        assert lock_owner(lock_path) == str(second_pid)
        assert acquire_single_instance(lock_path) is None  # тепер зайняте другим
        second.stdin.write("\n")  # штатне завершення: unlock прибирає файл
        second.stdin.flush()
        assert second.wait(timeout=TIMEOUT) == 0
    finally:
        stop(second, second_pid)
    assert not lock_path.exists()
