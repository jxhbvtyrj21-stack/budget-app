import os

# Тести інтерфейсу працюють без екрана (розділ 5.6).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
