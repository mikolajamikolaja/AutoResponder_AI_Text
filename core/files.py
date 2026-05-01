"""
core/files.py
Pomocnicze operacje na plikach: odczyt base64, wczytywanie promptów.
"""

import os
import base64
from flask import current_app

# Katalog główny projektu (tam gdzie app.py)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")


def read_file_base64(path: str):
    """Odczytuje plik i zwraca jego zawartość jako base64 string, lub None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            current_app.logger.warning("Plik pusty: %s", path)
            return None
        return base64.b64encode(data).decode("ascii")
    except Exception as e:
        current_app.logger.warning("read_file_base64 failed: %s — %s", path, e)
        return None


def load_prompt(filename: str, fallback: str = "") -> str:
    """
    Wczytuje prompt z katalogu prompts/.
    Jeśli plik nie istnieje, zwraca fallback.
    """
    path = os.path.join(PROMPTS_DIR, filename)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        current_app.logger.warning("load_prompt failed: %s — %s", path, e)
    return fallback
