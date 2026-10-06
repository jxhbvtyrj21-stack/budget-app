"""Розташування файлів, що постачаються разом із застосунком."""

import sys
from pathlib import Path


def resource_root() -> Path:
    """Корінь ресурсів: тека збірки PyInstaller або корінь репозиторію під час розробки."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is not None:
        return Path(bundle_dir)
    # src/budget/platform/resources.py → корінь репозиторію.
    return Path(__file__).resolve().parents[3]


def product_toml_path() -> Path:
    return resource_root() / "product.toml"


def assets_dir() -> Path:
    return resource_root() / "assets"
