import subprocess
import sys

from budget.platform.identity import load_product_identity
from budget.platform.paths import DATA_DIR_OVERRIDE_ENV, data_paths


def test_self_check_succeeds():
    result = subprocess.run(
        [sys.executable, "-m", "budget", "--self-check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_data_paths_use_identity(monkeypatch, tmp_path):
    identity = load_product_identity()
    monkeypatch.delenv(DATA_DIR_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = data_paths(identity)
    assert paths.root == tmp_path / identity.data_directory_name
    assert paths.database.name == "budget.db"
