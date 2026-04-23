#!/usr/bin/env python3
"""
core/user_manager.py
Zarządzanie stanem użytkowników (znani, na liście śmierci, etc.)
"""

import time
from typing import Optional
from functools import lru_cache

from drive_utils import check_user_in_sheet
from core.logging_reporter import get_logger


class UserManager:
    """Zarządza sprawdzaniem statusu użytkowników."""

    def __init__(
        self, history_sheet_id: str, death_sheet_id: str, cache_ttl: int = 300
    ):
        self.history_sheet_id = history_sheet_id
        self.death_sheet_id = death_sheet_id
        self.cache_ttl = cache_ttl
        self.logger = get_logger()
        self._cache = {}

    @lru_cache(maxsize=1000)
    def _check_sheet_cached(self, sheet_id: str, email: str, timestamp: int) -> bool:
        """Cache'owana wersja sprawdzenia w arkuszu."""
        return check_user_in_sheet(sheet_id, email)

    def is_known_user(self, email: str) -> bool:
        """Sprawdza czy użytkownik jest znany (w historii)."""
        if not self.history_sheet_id:
            return False

        cache_key = f"history_{email}"
        now = int(time.time())

        if cache_key in self._cache:
            cached_time, result = self._cache[cache_key]
            if now - cached_time < self.cache_ttl:
                return result

        try:
            result = self._check_sheet_cached(self.history_sheet_id, email, now)
            self._cache[cache_key] = (now, result)
            return result
        except Exception as e:
            self.logger.error(f"Błąd sprawdzania historii dla {email}: {e}")
            return False

    def is_on_death_list(self, email: str) -> bool:
        """Sprawdza czy użytkownik jest na liście śmierci."""
        if not self.death_sheet_id:
            return False

        cache_key = f"death_{email}"
        now = int(time.time())

        if cache_key in self._cache:
            cached_time, result = self._cache[cache_key]
            if now - cached_time < self.cache_ttl:
                return result

        try:
            result = self._check_sheet_cached(self.death_sheet_id, email, now)
            self._cache[cache_key] = (now, result)
            return result
        except Exception as e:
            self.logger.error(f"Błąd sprawdzania listy śmierci dla {email}: {e}")
            return False

    def clear_cache(self):
        """Czyści cache."""
        self._cache.clear()
        self._check_sheet_cached.cache_clear()
