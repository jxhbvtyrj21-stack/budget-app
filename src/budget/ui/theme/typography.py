"""Шрифти: Segoe UI для інтерфейсу, вбудована Source Serif 4 для заголовків і головних сум."""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from budget.ui.theme.tokens import (
    DISPLAY_FONT_FALLBACK,
    DISPLAY_FONT_FILES,
    TYPE_SCALE,
    UI_FONT_FAMILY,
)


@dataclass(frozen=True, slots=True)
class FontFamilies:
    ui: str
    display: str


def register_fonts(fonts_dir: Path) -> FontFamilies:
    """Реєструє вбудовані шрифти; без файлів — запасний засічковий шрифт (Georgia)."""
    display = None
    for file_name in DISPLAY_FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(str(fonts_dir / file_name))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families and display is None:
            display = families[0]
    return FontFamilies(ui=UI_FONT_FAMILY, display=display or DISPLAY_FONT_FALLBACK)


def font_for(role: str, families: FontFamilies) -> QFont:
    """QFont для ролі шкали типографіки; суми — з цифрами однакової ширини."""
    style = TYPE_SCALE[role]
    font = QFont(families.display if style.display else families.ui)
    font.setPixelSize(style.size_px)
    font.setWeight(QFont.Weight(style.weight))
    if style.tabular_figures:
        font.setFeature(QFont.Tag("tnum"), 1)
    return font
