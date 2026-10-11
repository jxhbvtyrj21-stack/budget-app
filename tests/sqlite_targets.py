"""Файл бази, який відкриває ``sqlite3.connect``, — для шпигунів у тестах.

Базу, що вже має існувати, застосунок відкриває через URI ``file:…?mode=rw``
(``storage.database.open_database(create=False)``), інші — за звичайним шляхом. Шпигун
записує ``connect_target(...)`` і так однаково впізнає обидва способи відкриття того самого
файлу: URI розбирається (``urlsplit`` + ``url2pathname``: відсоткове кодування, Unicode, диск
Windows), а не порівнюється з шляхом як рядок.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def connect_target(target, uri: bool = False) -> str:
    """Шлях до файлу, який відкриває ``sqlite3.connect(target, uri=uri)``, як ``str``."""
    if uri:
        parts = urlsplit(os.fspath(target))
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            raise AssertionError(f"Неочікуваний URI бази: {target}")
        return str(Path(url2pathname(parts.path)))
    return str(Path(os.fspath(target)))
