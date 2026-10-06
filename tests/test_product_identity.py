from pathlib import Path

import pytest

from budget.errors import ProductIdentityError
from budget.platform.identity import load_product_identity
from budget.platform.resources import product_toml_path

VALID = """
[product]
name = "Budget"
publisher = "Budget"
app_id = "5534B0BB-9852-4C19-8043-7486FC92928A"
app_user_model_id = "Budget.Budget"
executable_name = "Budget"
installer_base_name = "Budget-Setup"
data_directory_name = "Budget"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "product.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_product_toml_loads():
    identity = load_product_identity()
    assert product_toml_path().is_file()
    assert identity.app_user_model_id == f"{identity.publisher}.{identity.name}"


def test_missing_file(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(tmp_path / "absent.toml")


def test_malformed_toml(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, "[product\nname = "))


def test_duplicate_key_is_rejected(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, VALID + 'name = "Other"\n'))


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "publisher",
        "app_id",
        "app_user_model_id",
        "executable_name",
        "installer_base_name",
        "data_directory_name",
    ],
)
def test_missing_required_field(tmp_path, field):
    text = "\n".join(line for line in VALID.splitlines() if not line.startswith(f"{field} ="))
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, text))


def test_invalid_app_id(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, VALID.replace("5534B0BB-", "XYZ-")))


def test_inconsistent_app_user_model_id(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, VALID.replace('"Budget.Budget"', '"Other.App"')))


def test_unknown_field(tmp_path):
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, VALID + 'extra = "x"\n'))


def test_invalid_file_name_characters(tmp_path):
    text = VALID.replace('data_directory_name = "Budget"', 'data_directory_name = "Bud:get"')
    with pytest.raises(ProductIdentityError):
        load_product_identity(write(tmp_path, text))
