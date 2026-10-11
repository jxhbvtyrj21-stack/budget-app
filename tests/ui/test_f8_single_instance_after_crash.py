"""F8-E3: блокування одного екземпляра після аварійного завершення власника.

Справжній ``acquire_single_instance`` (справжній ``QLockFile``) у дочірньому процесі на
шляху в тимчасовій теці тесту. Дочірній процес повідомляє, що блокування взято, і чекає;
батьківський процес перевіряє, що блокування зайняте, примусово завершує дочірній
(``Popen.kill``: SIGKILL на Linux, ``TerminateProcess`` на Windows) — без ``unlock`` і без
деструкторів Qt — і чекає його завершення через ``wait``. Потім те саме блокування має
братися знову.
"""

import subprocess
import sys

from budget.platform.single_instance import acquire_single_instance

CHILD = """
import sys
from pathlib import Path
from budget.platform.single_instance import acquire_single_instance

lock = acquire_single_instance(Path(sys.argv[1]))
print("locked" if lock is not None else "busy", flush=True)
sys.stdin.readline()  # тримає блокування, доки батьківський процес його не завершить
"""


def test_lock_is_acquired_again_after_the_owner_is_killed(tmp_path):
    lock_path = tmp_path / "isolated" / "budget.lock"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert lock_path.exists()
        # Власник живий: блокування зайняте, а файл не видаляється як застарілий.
        assert acquire_single_instance(lock_path) is None
        assert lock_path.exists()
        owner_pid = lock_path.read_text(encoding="utf-8").splitlines()[0]
        assert owner_pid == str(child.pid)
        child.kill()
        assert child.wait(timeout=30) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
        child.stdin.close()
        child.stdout.close()

    # Аварійне завершення не прибрало файл: штатного unlock не було.
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").splitlines()[0] == str(child.pid)

    lock = acquire_single_instance(lock_path)
    assert lock is not None
    try:
        assert lock_path.read_text(encoding="utf-8").splitlines()[0] != str(child.pid)
    finally:
        lock.unlock()
    assert not lock_path.exists()
