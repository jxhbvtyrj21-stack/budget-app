"""Позначки стану й прогрес до цільової суми (design-system.md, 6.7, 8).

Статус життєвого циклу й архівність — позначки різного вигляду: архівоване
накопичення має обидві («Закрите» і «В архіві»). Суми не забарвлюються.
"""

from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from budget.domain.models import AccumulationStatus, TargetProgress
from budget.services.accumulation import status_label
from budget.ui.components.basic import text_label
from budget.ui.formatting import format_money

BADGE_HEIGHT = 22  # design-system.md, 6.7


def status_badge(status: AccumulationStatus) -> QLabel:
    text = status_label(status)
    if status is AccumulationStatus.REACHED:
        text = f"✓ {text}"
    badge = text_label(text, "caption")
    badge.setObjectName("StatusBadge")
    badge.setProperty("badge", status.value)
    badge.setWordWrap(False)
    badge.setFixedHeight(BADGE_HEIGHT)
    return badge


def archive_marker() -> QLabel:
    marker = text_label("В архіві", "caption", muted=True)
    marker.setObjectName("ArchiveMarker")
    marker.setWordWrap(False)
    return marker


def progress_text(progress: TargetProgress) -> str:
    if progress.excess.is_positive:
        return f"понад цільову суму на {format_money(progress.excess)}"
    return (
        f"{format_money(progress.balance)} з {format_money(progress.target)} · {progress.percent} %"
    )


class ProgressView(QWidget):
    """Тонка смуга й підпис; без цільової суми не показується (design-system.md, 8)."""

    def __init__(self, progress: TargetProgress) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.bar = QProgressBar()
        self.bar.setObjectName("TargetProgress")
        self.bar.setRange(0, 100)
        self.bar.setValue(min(progress.percent, 100))
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.label = text_label(progress_text(progress), "secondary", muted=True)
        layout.addWidget(self.bar)
        layout.addWidget(self.label)
