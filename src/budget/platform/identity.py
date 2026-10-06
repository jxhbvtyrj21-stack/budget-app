"""Єдиний механізм читання ідентичності продукту з ``product.toml`` (A-7, ADR 0015).

Значення ідентичності не прописуються в коді: застосунок, пакування й CI читають їх
лише звідси.
"""

import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from budget.errors import ProductIdentityError
from budget.platform.resources import product_toml_path

_REQUIRED_FIELDS = (
    "name",
    "publisher",
    "app_id",
    "app_user_model_id",
    "executable_name",
    "installer_base_name",
    "data_directory_name",
)
# Символи, заборонені в іменах файлів і тек Windows.
_INVALID_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    name: str
    publisher: str
    app_id: str
    app_user_model_id: str
    executable_name: str
    installer_base_name: str
    data_directory_name: str


def load_product_identity(path: Path | None = None) -> ProductIdentity:
    """Читає й перевіряє ``product.toml``. Помилка — ``ProductIdentityError``."""
    path = path or product_toml_path()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProductIdentityError(detail=f"product.toml недоступний: {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProductIdentityError(detail=f"product.toml некоректний: {exc}") from exc

    section = data.get("product")
    if not isinstance(section, dict):
        raise ProductIdentityError(detail="product.toml: відсутня секція [product]")
    unknown = sorted(set(section) - set(_REQUIRED_FIELDS))
    if unknown:
        raise ProductIdentityError(detail=f"product.toml: невідомі поля {unknown}")
    values: dict[str, str] = {}
    for field in _REQUIRED_FIELDS:
        value = section.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProductIdentityError(detail=f"product.toml: відсутнє поле {field}")
        values[field] = value
    identity = ProductIdentity(**values)
    _check_consistency(identity)
    return identity


def _check_consistency(identity: ProductIdentity) -> None:
    try:
        parsed = uuid.UUID(identity.app_id)
    except ValueError as exc:
        raise ProductIdentityError(detail="product.toml: app_id не є GUID") from exc
    if str(parsed).upper() != identity.app_id:
        raise ProductIdentityError(detail="product.toml: app_id має бути GUID у верхньому регістрі")
    expected_aumid = f"{identity.publisher}.{identity.name}"
    if identity.app_user_model_id != expected_aumid:
        raise ProductIdentityError(
            detail=f"product.toml: app_user_model_id має бути {expected_aumid!r} (Видавець.Продукт)"
        )
    for field in ("executable_name", "installer_base_name", "data_directory_name"):
        if _INVALID_FILE_NAME.search(getattr(identity, field)):
            raise ProductIdentityError(detail=f"product.toml: {field} містить заборонені символи")
