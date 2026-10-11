from datetime import UTC, datetime

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from budget.app import build_main_window, open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.platform.resources import assets_dir
from budget.services.facade import AppServices
from budget.storage.transaction import transaction
from budget.ui.formatting import format_money
from budget.ui.main_window import FOOTER_ROUTES, MAIN_ROUTES, MainWindow
from budget.ui.theme.tokens import CONTRAST_REQUIREMENTS, ROLES, contrast_ratio
from budget.ui.theme.typography import font_for, register_fonts

CLOCK = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def connection(tmp_path):
    connection = open_application_database(DataPaths(tmp_path / "data"), CLOCK)
    yield connection
    connection.close()


def test_clean_database_starts_in_setup_mode(qtbot, connection):
    window = build_main_window(load_product_identity(), connection, CLOCK)
    qtbot.addWidget(window)
    assert window.sidebar is None
    assert window.routes() == ["setup"]
    assert window.wizard is not None
    assert window.windowTitle() == load_product_identity().name


def test_completed_setup_shows_all_routes(qtbot, connection):
    with transaction(connection):
        connection.execute(
            "UPDATE setup_state SET status = 'completed', completed_month = '2026-10' WHERE id = 1"
        )
    window = build_main_window(load_product_identity(), connection, CLOCK)
    qtbot.addWidget(window)
    expected = [route for route, _ in MAIN_ROUTES + FOOTER_ROUTES]
    assert window.routes() == expected
    assert window.current_route() == "overview"
    for route in expected:
        window.navigate(route)
        assert window.current_route() == route
        assert window.sidebar.active_route() == route


def test_sidebar_click_navigates(qtbot, connection):
    with transaction(connection):
        connection.execute(
            "UPDATE setup_state SET status = 'completed', completed_month = '2026-10' WHERE id = 1"
        )
    window = MainWindow("Test", AppServices.create(connection, CLOCK))
    qtbot.addWidget(window)
    window.sidebar._buttons["debts"].click()
    assert window.current_route() == "debts"


def test_stylesheet_applies(qtbot, connection):
    build_main_window(load_product_identity(), connection, CLOCK)
    sheet = QApplication.instance().styleSheet()
    assert ROLES["bg"] in sheet and ROLES["primary"] in sheet


def test_display_font_is_bundled(qapp):
    families = register_fonts(assets_dir() / "fonts")
    assert families.display == "Source Serif 4"
    amount = font_for("display-amount", families)
    assert amount.family() == "Source Serif 4"
    assert amount.featureValue(QFont.Tag("tnum")) == 1


def test_format_money(qapp):
    assert format_money(Money(500_000)).replace(" ", " ").replace(" ", " ") == "5 000"
    assert format_money(Money(500_050)).endswith(",50")
    assert format_money(Money(7)) == "0,07"
    assert format_money(Money(-100)) == "−1"


@pytest.mark.parametrize(("foreground", "background", "minimum"), CONTRAST_REQUIREMENTS)
def test_token_contrast(foreground, background, minimum):
    assert contrast_ratio(ROLES[foreground], ROLES[background]) >= minimum


def test_rendered_colors_match_tokens(qtbot, connection):
    with transaction(connection):
        connection.execute(
            "UPDATE setup_state SET status = 'completed', completed_month = '2026-10' WHERE id = 1"
        )
    window = build_main_window(load_product_identity(), connection, CLOCK)
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.waitExposed(window)
    image = window.grab().toImage()
    sidebar_width = window.sidebar.width()
    assert image.pixelColor(sidebar_width // 2, 400).name().upper() == ROLES["surface-sunken"]
    assert image.pixelColor(sidebar_width - 1, 400).name().upper() == ROLES["divider"]
    assert image.pixelColor(sidebar_width + 400, 700).name().upper() == ROLES["bg"]
