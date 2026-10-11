"""Шаблон QSS, що отримує значення лише з токенів."""

from budget.ui.theme.tokens import CONTROL_HEIGHT, RADII, ROLES, SPACING, TYPE_SCALE
from budget.ui.theme.typography import FontFamilies


def _text_rule(selector: str, role: str, families: FontFamilies) -> str:
    style = TYPE_SCALE[role]
    family = families.display if style.display else families.ui
    return (
        f'{selector} {{ font-family: "{family}"; font-size: {style.size_px}px;'
        f" font-weight: {style.weight}; }}"
    )


def build_stylesheet(families: FontFamilies) -> str:
    r = ROLES
    rules = [
        f"""
QWidget {{ color: {r["text"]}; font-family: "{families.ui}"; font-size: 14px; }}
QMainWindow, #ContentArea, #PageViewport {{ background: {r["bg"]}; }}
QScrollArea {{ background: {r["bg"]}; border: none; }}
#Sidebar {{ background: {r["surface-sunken"]}; border-right: 1px solid {r["divider"]}; }}
#Sidebar QPushButton {{
    text-align: left; border: none; border-left: 3px solid transparent;
    padding: {SPACING[3]}px {SPACING[4]}px; background: transparent; color: {r["text"]};
}}
#Sidebar QPushButton:hover {{ background: {r["surface"]}; }}
#Sidebar QPushButton:checked {{
    background: {r["surface"]}; color: {r["primary"]}; font-weight: 600;
    border-left: 3px solid {r["primary"]};
}}
#Sidebar QPushButton:focus {{ outline: none; background: {r["primary-soft"]}; }}
#Panel {{
    background: {r["surface"]}; border: 1px solid {r["divider"]};
    border-radius: {RADII["lg"]}px;
}}
QPushButton[variant="primary"] {{
    background: {r["primary"]}; color: {r["on-primary"]}; border: 1px solid {r["primary"]};
    border-radius: {RADII["md"]}px; min-height: {CONTROL_HEIGHT}px; padding: 0 {SPACING[4]}px;
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {r["primary-hover"]}; }}
QPushButton[variant="primary"]:pressed {{ background: {r["primary-pressed"]}; }}
QPushButton[variant="secondary"] {{
    background: {r["surface"]}; color: {r["text"]}; border: 1px solid {r["border-control"]};
    border-radius: {RADII["md"]}px; min-height: {CONTROL_HEIGHT}px; padding: 0 {SPACING[4]}px;
    font-weight: 600;
}}
QPushButton[variant="secondary"]:hover {{ background: {r["surface-sunken"]}; }}
QPushButton:disabled {{
    background: {r["surface-sunken"]}; color: {r["text-disabled"]}; border-color: {r["divider"]};
}}
QPushButton[variant]:focus {{ border: 2px solid {r["focus-ring"]}; }}
QLineEdit {{
    background: {r["surface"]}; border: 1px solid {r["border-control"]};
    border-radius: {RADII["md"]}px; min-height: {CONTROL_HEIGHT}px; padding: 0 10px;
}}
QLineEdit:focus {{ border: 2px solid {r["focus-ring"]}; }}
QLineEdit:disabled {{ background: {r["surface-sunken"]}; color: {r["text-disabled"]}; }}
QLabel[tone="muted"] {{ color: {r["text-muted"]}; }}
QLabel[tone="error"] {{ color: {r["danger-text"]}; font-size: 12px; }}
QLabel[tone="primary"] {{ color: {r["primary"]}; }}
QPushButton[variant="text"] {{
    background: transparent; color: {r["primary"]}; border: none; padding: 0; font-weight: 600;
}}
QPushButton[variant="text"]::menu-indicator {{ image: none; width: 0; }}
QPushButton[variant="danger-text"] {{
    background: transparent; color: {r["danger-text"]}; border: none; padding: 0;
}}
#Notice {{
    background: {r["notice-soft"]}; border-left: 3px solid {r["notice-border"]};
    border-radius: {RADII["sm"]}px;
}}
#ErrorNotice {{
    background: {r["danger-soft"]}; border-left: 3px solid {r["danger"]};
    border-radius: {RADII["sm"]}px;
}}
#ListRow {{ border-bottom: 1px solid {r["divider"]}; }}
#DividedSection {{ border-top: 1px solid {r["divider"]}; }}
QPushButton[segment="true"]:checked {{
    background: {r["primary-soft"]}; color: {r["primary"]}; border-color: {r["primary"]};
}}
#StatusBadge {{
    border-radius: {RADII["sm"]}px; padding: 0 {SPACING[2]}px; font-weight: 600;
}}
#StatusBadge[badge="active"] {{ border: 1px solid {r["border-control"]}; color: {r["text"]}; }}
#StatusBadge[badge="reached"] {{ background: {r["primary-soft"]}; color: {r["primary"]}; }}
#StatusBadge[badge="closed"], #StatusBadge[badge="paid"] {{
    background: {r["neutral-fill"]}; color: {r["on-neutral-fill"]};
}}
#ArchiveMarker {{ color: {r["text-muted"]}; font-weight: 600; }}
#TargetProgress {{
    background: {r["surface-sunken"]}; border: none; border-radius: {RADII["sm"]}px;
}}
#TargetProgress::chunk {{ background: {r["primary"]}; border-radius: {RADII["sm"]}px; }}
#InfoBanner {{ background: {r["surface-sunken"]}; border-radius: {RADII["sm"]}px; }}
QDialog {{ background: {r["surface"]}; }}
""",
    ]
    for role in TYPE_SCALE:
        rules.append(_text_rule(f'QLabel[textRole="{role}"]', role, families))
    return "\n".join(rules)
