"""Архітектурні обмеження (розділ 5.2, ADR 0008, ADR 0009, ADR 0015)."""

import ast
import re
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


# Межа читання (IA 12, E2) ---------------------------------------------------------------------

READ_BOUNDARY = SRC / "ui" / "components" / "read_error.py"
READ_FAILURE = SRC / "services" / "read_errors.py"
BROAD = {"Exception", "BaseException"}


def broad_handlers(path: Path) -> list[ast.ExceptHandler]:
    """``except:``, ``except Exception`` чи ``except BaseException`` (також у кортежі)."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ExceptHandler):
            continue
        types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        names = {getattr(t, "id", getattr(t, "attr", None)) for t in types if t is not None}
        if node.type is None or names & BROAD:
            found.append(node)
    return found


def test_broad_except_in_ui_only_in_the_read_boundary():
    found = {
        p.relative_to(SRC).as_posix(): len(broad_handlers(p))
        for p in modules("ui")
        if broad_handlers(p)
    }
    assert found == {"ui/components/read_error.py": 1}


def test_read_boundary_reraises_unchanged_and_keeps_no_exception():
    """Класифікація лише через ``read_failure``; усе інше — bare ``raise`` (той самий
    об'єкт), без ``raise X from``; виняток не записується в атрибути."""
    (handler,) = broad_handlers(READ_BOUNDARY)
    calls = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "read_failure" in calls
    raises = [node for node in ast.walk(handler) if isinstance(node, ast.Raise)]
    assert raises and all(node.exc is None and node.cause is None for node in raises)
    stores = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    ]
    assert stores == []


def test_read_failure_uses_the_guard_classification_and_operational_error_only():
    source = READ_FAILURE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("budget.storage.integrity", "corruption_code") in imports  # та сама, що в guard
    # Надто широкий критерій (DatabaseError і його підкласи) заборонений.
    assert "DatabaseError" not in source
    checked = [
        ast.unparse(node.args[1])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "isinstance"
    ]
    assert checked == ["sqlite3.OperationalError"]


def test_ui_does_not_classify_sqlite_errors_itself():
    for path in modules("ui"):
        text = path.read_text(encoding="utf-8")
        assert "corruption_code" not in text and "sqlite_errorcode" not in text, path


def test_read_error_path_is_not_used_by_storage_recovery_or_app():
    pattern = re.compile(r"\b(read_failure|DataReadError|ReadBoundary|ReadErrorState)\b")
    users = {
        p.relative_to(SRC).as_posix()
        for p in modules()
        if pattern.search(p.read_text(encoding="utf-8"))
    }
    assert users == {
        "errors.py",
        "services/read_errors.py",
        "ui/components/read_error.py",
        "ui/screens/overview.py",
        "ui/screens/month.py",
        "ui/screens/accumulations.py",
        "ui/screens/debts.py",
    }
