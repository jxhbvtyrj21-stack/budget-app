"""Інтеграція з Windows: ідентифікатор застосунку для панелі завдань (A-3, Додаток A.3)."""

import sys


def set_app_user_model_id(app_user_model_id: str) -> None:
    """Однаковий AppUserModelID у ярлику й процесі — іконка застосунку на панелі завдань."""
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_user_model_id)
