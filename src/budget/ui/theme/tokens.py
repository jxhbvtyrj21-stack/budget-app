"""Дизайн-токени першого релізу — значення з ``docs/design/design-system.md`` (ADR 0021).

Компоненти звертаються лише до семантичних ролей (``ROLES``), а не до базової палітри.
Кольори поза цим модулем не прописуються.
"""

from dataclasses import dataclass

PALETTE = {
    "paper-100": "#FBF8F1",
    "paper-200": "#F5F0E6",
    "paper-300": "#EFE8DA",
    "paper-400": "#EAE3D5",
    "stone-300": "#E2DACB",
    "stone-500": "#8F8471",
    "stone-600": "#A39C90",
    "stone-700": "#6B655C",
    "stone-800": "#4A463F",
    "graphite-900": "#1F2A24",
    "sage-100": "#DDE5D8",
    "sage-700": "#2F4A3A",
    "sage-800": "#263D30",
    "sage-900": "#1E3226",
    "clay-100": "#F3E1DA",
    "clay-600": "#A8553F",
    "clay-700": "#93472F",
    "ochre-100": "#F4E9D3",
    "ochre-500": "#B88A3E",
    "ochre-800": "#7D5A1F",
}

_ROLE_TO_PALETTE = {
    "bg": "paper-200",
    "surface": "paper-100",
    "surface-sunken": "paper-300",
    "divider": "stone-300",
    "border-control": "stone-500",
    "text": "graphite-900",
    "text-muted": "stone-700",
    "text-disabled": "stone-600",
    "primary": "sage-700",
    "primary-hover": "sage-800",
    "primary-pressed": "sage-900",
    "on-primary": "paper-100",
    "primary-soft": "sage-100",
    "neutral-fill": "paper-400",
    "on-neutral-fill": "stone-800",
    "danger": "clay-600",
    "danger-text": "clay-700",
    "danger-soft": "clay-100",
    "notice-border": "ochre-500",
    "notice-text": "ochre-800",
    "notice-soft": "ochre-100",
    "focus-ring": "sage-700",
}

ROLES: dict[str, str] = {role: PALETTE[name] for role, name in _ROLE_TO_PALETTE.items()}

# Пари «текст / фон» із мінімальним контрастом WCAG AA (design-system.md, 2.3).
CONTRAST_REQUIREMENTS: tuple[tuple[str, str, float], ...] = (
    ("text", "bg", 4.5),
    ("text", "surface", 4.5),
    ("text", "surface-sunken", 4.5),
    ("text-muted", "bg", 4.5),
    ("text-muted", "surface", 4.5),
    ("text-muted", "surface-sunken", 4.5),
    ("on-primary", "primary", 4.5),
    ("primary", "surface", 4.5),
    ("primary", "primary-soft", 4.5),
    ("on-neutral-fill", "neutral-fill", 4.5),
    ("danger-text", "surface", 4.5),
    ("danger-text", "danger-soft", 4.5),
    ("on-primary", "danger", 4.5),
    ("notice-text", "notice-soft", 4.5),
    ("border-control", "surface", 3.0),
    ("border-control", "bg", 3.0),
)

SPACING = {1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 7: 48, 8: 64}
RADII = {"sm": 4, "md": 6, "lg": 10}

UI_FONT_FAMILY = "Segoe UI"
DISPLAY_FONT_FILES = ("SourceSerif4-Regular.ttf", "SourceSerif4-Semibold.ttf")
DISPLAY_FONT_FALLBACK = "Georgia"


@dataclass(frozen=True, slots=True)
class TextStyle:
    display: bool  # True — Source Serif 4, False — Segoe UI
    size_px: int
    line_height_px: int
    weight: int
    tabular_figures: bool = False


TYPE_SCALE: dict[str, TextStyle] = {
    "display-amount": TextStyle(True, 40, 48, 600, tabular_figures=True),
    "title": TextStyle(True, 28, 36, 400),
    "heading": TextStyle(False, 18, 24, 600),
    "subheading": TextStyle(False, 15, 20, 600),
    "amount-lg": TextStyle(False, 20, 28, 600, tabular_figures=True),
    "body": TextStyle(False, 14, 20, 400),
    "body-strong": TextStyle(False, 14, 20, 600),
    "amount": TextStyle(False, 14, 20, 600, tabular_figures=True),
    "secondary": TextStyle(False, 13, 18, 400),
    "caption": TextStyle(False, 12, 16, 400),
}

SIDEBAR_WIDTH = 224
CONTENT_MAX_WIDTH = 1040
WINDOW_MIN_SIZE = (1024, 680)
WINDOW_DEFAULT_SIZE = (1280, 800)
CONTROL_HEIGHT = 32


def contrast_ratio(foreground: str, background: str) -> float:
    """Контраст WCAG 2.x між двома кольорами ``#RRGGBB``."""

    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
