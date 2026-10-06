"""Архітектурні обмеження (розділ 5.2, ADR 0008, ADR 0009, ADR 0015)."""

import ast
import tomllib
from pathlib import Path

import pytest

from budget.platform.resources import product_toml_path

SRC = Path(__file__).resolve().parents[1] / "src" / "budget"


def modules(layer: str | None = None) -> list[Path]:
    root = SRC / layer if layer else SRC
    return sorted(root.rglob("*.py"))


def imported_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


FORBIDDEN_IMPORTS = {
    "domain": (
        "PySide6",
        "sqlite3",
        "budget.storage",
        "budget.services",
        "budget.ui",
        "budget.platform",
        "budget.app",
    ),
    "storage": ("PySide6", "budget.services", "budget.ui", "budget.platform", "budget.app"),
    "services": ("PySide6", "budget.ui", "budget.platform", "budget.app"),
    "ui": ("sqlite3", "budget.storage"),
}


@pytest.mark.parametrize("layer", sorted(FORBIDDEN_IMPORTS))
def test_layer_boundaries(layer):
    violations = []
    for path in modules(layer):
        for name in imported_names(path):
            if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS[layer]):
                violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert not violations, violations


@pytest.mark.parametrize("layer", ["domain", "storage", "services"])
def test_no_float_for_money(layer):
    """``float`` заборонений у логіці й сховищі (A-15, DS-1)."""
    violations = []
    for path in modules(layer):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and type(node.value) is float:
                violations.append(f"{path.relative_to(SRC)}:{node.lineno} float literal")
            if isinstance(node, ast.Name) and node.id == "float":
                violations.append(f"{path.relative_to(SRC)}:{node.lineno} float")
        if "REAL" in path.read_text(encoding="utf-8"):
            violations.append(f"{path.relative_to(SRC)} uses REAL columns")
    assert not violations, violations


def test_financial_timezone_and_clock_are_central():
    """Europe/Kyiv, ZoneInfo і системний час — лише в domain/calendar.py (ADR 0009)."""
    allowed = SRC / "domain" / "calendar.py"
    found = []
    for path in modules():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and node.value == "Europe/Kyiv":
                found.append(path)
            if isinstance(node, ast.Name) and node.id == "ZoneInfo" and path != allowed:
                found.append(path)
        if path != allowed:
            for call in ("datetime.now(", "date.today(", "time.time(", "datetime.utcnow("):
                assert call not in text, f"{path}: {call}"
    assert found == [allowed], found


def test_product_identity_values_are_not_hardcoded():
    """Значення ідентичності є лише в product.toml (A-7, ADR 0015)."""
    identity = tomllib.loads(product_toml_path().read_text(encoding="utf-8"))["product"]
    values = set(identity.values())
    violations = []
    for path in modules():
        text = path.read_text(encoding="utf-8")
        if identity["app_id"] in text:
            violations.append(f"{path.relative_to(SRC)} contains AppId")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in values:
                    violations.append(f"{path.relative_to(SRC)}:{node.lineno} {node.value!r}")
    assert not violations, violations


def test_product_toml_read_only_by_identity_loader():
    readers = {
        p.relative_to(SRC).as_posix() for p in modules() if "tomllib" in p.read_text("utf-8")
    }
    assert readers == {"platform/identity.py"}


def test_ui_formats_money_in_one_place():
    """Суми форматує лише budget.ui.formatting (design-system.md, 3.3)."""
    users = {
        p.relative_to(SRC).as_posix()
        for p in modules("ui")
        if "QLocale" in p.read_text(encoding="utf-8")
    }
    assert users == {"ui/formatting.py"}


def test_colors_only_in_theme():
    """Hex-кольори лише в ui/theme (A-14, розділ 5.4)."""
    import re

    pattern = re.compile(r"#[0-9A-Fa-f]{6}\b")
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in modules()
        if pattern.search(p.read_text(encoding="utf-8")) and "ui/theme" not in p.as_posix()
    ]
    assert not offenders, offenders
