"""Точка входу: ``python -m budget`` або ``Budget.exe``."""

import sys

from budget.app import main


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
