"""Ієрархія помилок застосунку.

Кожна помилка має ``user_message`` — текст для користувача українською. Технічні
подробиці (виняток SQLite, трасування) потрапляють лише в журнал, а не в основне
повідомлення інтерфейсу.
"""


class BudgetError(Exception):
    """Базова помилка застосунку."""

    default_message = "Сталася помилка."

    def __init__(self, user_message: str | None = None, *, detail: str | None = None) -> None:
        self.user_message = user_message or self.default_message
        self.detail = detail
        super().__init__(detail or self.user_message)


class ValidationError(BudgetError):
    """Некоректне введення користувача: формат суми, порожня обов'язкова назва тощо."""

    default_message = "Перевірте введені дані."


class DomainRuleError(BudgetError):
    """Дію заблоковано бізнес-правилом: недостатній залишок, архів, минулий місяць тощо."""

    default_message = "Цю дію не можна виконати."


class StorageError(BudgetError):
    """Помилка роботи зі сховищем даних."""

    default_message = "Не вдалося прочитати або зберегти дані."


class DataReadError(StorageError):
    """Звичайна (не пошкодження) помилка читання для екрана (IA 12). Створює її лише
    ``services.read_errors.read_failure``; пошкодження бази нею ніколи не стає."""

    default_message = "Не вдалося прочитати дані."


class DatabaseCorruptedError(StorageError):
    """База даних пошкоджена; застосунок не повинен у неї писати (DS-6)."""

    default_message = "Дані пошкоджено. Застосунок нічого не записав у пошкоджений файл."


class StartupError(BudgetError):
    """Помилка, після якої застосунок не може продовжити запуск."""

    default_message = "Не вдалося запустити застосунок."


class ProductIdentityError(StartupError):
    """Файл ідентичності продукту відсутній або некоректний (A-7, ADR 0015)."""

    default_message = "Пошкоджено файли встановлення застосунку. Перевстановіть застосунок."
