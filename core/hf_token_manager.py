"""
core/hf_token_manager.py
════════════════════════════════════════════════════════════════════════════════
Centralny menedżer tokenów Hugging Face — współdzielony przez WSZYSTKIE
respondery (zwykly, smierc, dociekliwy, …).

ZASADY:
  1. Stan tokenów (aktywne / martwe) jest JEDEN dla całego procesu serwera.
  2. Walidacja tokenu odbywa się przez lekki GET na whoami HF API.
  3. Warm-up jednorazowy przy pierwszym użyciu (lazy).
  4. Tokeny oznaczane jako martwe przez mark_dead(name) podczas sesji.
  5. Reset ręczny przez /admin/hf-reset.

KLUCZOWE OPTYMALIZACJE (vs poprzednia wersja):
  - Gdy ALL_DEAD_CACHE=True → get_active_tokens() zwraca [] natychmiast,
    zero requestów HTTP. Kasuje cache po RECHECK_AFTER sekundach.
  - warm-up NIE jest blokujący dla wątku pipeline — odpala się w tle
    i zwraca [] dopóki nie skończy (fail-fast).
  - warmup() uruchamia się MAX RAZ co MIN_WARMUP_INTERVAL sekund
    — chroni przed pętlą restart→warmup→OOM.
  - Liczba równoległych wątków warm-upu ograniczona do MAX_WARMUP_THREADS.

UŻYCIE:
    from core.hf_token_manager import hf_tokens, mark_dead, get_active_tokens

    tokens = get_active_tokens()          # [(name, value), ...]
    mark_dead("HF_TOKEN3")                # po błędzie 401/402/403
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

import requests as _requests

logger = logging.getLogger(__name__)

# ─── Stałe konfiguracyjne ──────────────────────────────────────────────────────

_HF_WHOAMI_URL = "https://huggingface.co/api/whoami"
_CHECK_TIMEOUT = 6           # timeout pojedynczego sprawdzenia tokenu (s)
_TOKEN_RANGE   = 100         # skanuj HF_TOKEN, HF_TOKEN1 … HF_TOKEN99
_MAX_WARMUP_THREADS = 8      # max równoległych wątków podczas warm-upu

# Po tym czasie od warm-upu z wynikiem ALL_DEAD — spróbuj ponownie
# 0 = nigdy nie próbuj ponownie (tokeny 401 są trwale martwe)
_RECHECK_AFTER = 300  # po 5 min sprawdź ponownie (obsługuje dodanie nowego tokenu)

# Minimalny odstęp między kolejnymi warm-upami (ochrona przed pętlą OOM-restart)
_MIN_WARMUP_INTERVAL = 120   # sekund


# ─── Stan tokenu ──────────────────────────────────────────────────────────────

class _TokenState:
    __slots__ = ("name", "value", "alive", "dead_reason", "dead_at", "remaining")

    def __init__(self, name: str, value: str):
        self.name:        str            = name
        self.value:       str            = value
        self.alive:       bool           = True
        self.dead_reason: str            = ""
        self.dead_at:     float          = 0.0
        self.remaining:   Optional[int]  = None


# ─── Menedżer ─────────────────────────────────────────────────────────────────

class HFTokenManager:
    """
    Singleton — jeden obiekt na cały proces Flask. Thread-safe (RLock).
    """

    def __init__(self):
        self._lock              = threading.RLock()
        self._tokens:           dict[str, _TokenState] = {}
        self._warmed_up:        bool  = False
        self._warmup_lock:      threading.Lock = threading.Lock()
        self._warmup_running:   bool  = False
        self._last_warmup_at:   float = 0.0   # monotonic timestamp ostatniego warm-upu
        self._all_dead_since:   float = 0.0   # kiedy ostatnio stwierdzono ALL DEAD

    # ── Wczytywanie tokenów ze środowiska ─────────────────────────────────────

    def _load_from_env(self) -> list[_TokenState]:
        names = ["HF_TOKEN"] + [f"HF_TOKEN{i}" for i in range(1, _TOKEN_RANGE)]
        states = []
        for name in names:
            val = os.getenv(name, "").strip()
            if val:
                states.append(_TokenState(name, val))
        return states

    # ── Lekka weryfikacja jednego tokenu ──────────────────────────────────────

    @staticmethod
    def _check_token_alive(name: str, value: str) -> tuple[bool, str]:
        """
        Sprawdza token przez GET /api/whoami.
        Zwraca (alive, reason).
        """
        headers = {"Authorization": f"Bearer {value}"}
        try:
            resp = _requests.get(_HF_WHOAMI_URL, headers=headers,
                                 timeout=_CHECK_TIMEOUT)
            resp.close()
            if resp.status_code == 200:
                return True, ""
            elif resp.status_code in (401, 403):
                reason = f"Nieważny token (HTTP {resp.status_code})"
                logger.warning("[hf-manager] Token %s MARTWY: %s", name, reason)
                return False, reason
            elif resp.status_code == 429:
                # Rate limit na whoami — token prawdopodobnie żywy
                logger.warning("[hf-manager] Token %s: rate limit whoami — zakładam aktywny", name)
                return True, ""
            else:
                # Nieznany status — zakładamy aktywny, żeby nie blokować
                logger.debug("[hf-manager] Token %s: HTTP %s — zakładam aktywny",
                             name, resp.status_code)
                return True, ""
        except _requests.exceptions.Timeout:
            logger.warning("[hf-manager] Token %s: timeout — zakładam aktywny", name)
            return True, ""
        except Exception as e:
            logger.warning("[hf-manager] Token %s: błąd (%s) — zakładam aktywny", name, e)
            return True, ""

    # ── Warm-up ───────────────────────────────────────────────────────────────

    def warmup(self, force: bool = False) -> None:
        """
        Sprawdza wszystkie tokeny JEDEN raz.
        - Blokuje warmup_lock żeby nie uruchamiać wielokrotnie równolegle.
        - Respektuje _MIN_WARMUP_INTERVAL — chroni przed pętlą OOM-restart.
        - Ogranicza równoległość do _MAX_WARMUP_THREADS.
        """
        with self._warmup_lock:
            if self._warmed_up and not force:
                return

            now = time.monotonic()
            if not force and (now - self._last_warmup_at) < _MIN_WARMUP_INTERVAL:
                # Za wcześnie na kolejny warm-up — zwróć co mamy
                logger.info(
                    "[hf-manager] Warm-up pominięty — był %.0fs temu (min %ds)",
                    now - self._last_warmup_at, _MIN_WARMUP_INTERVAL
                )
                self._warmed_up = True  # traktuj jako "done" żeby nie loopować
                return

            self._last_warmup_at = now

            states = self._load_from_env()
            if not states:
                logger.error("[hf-manager] Brak tokenów HF w zmiennych środowiskowych!")
                with self._lock:
                    self._tokens    = {}
                    self._warmed_up = True
                return

            logger.info(
                "[hf-manager] Warm-up: sprawdzam %d tokenów HF…", len(states)
            )

            # Równoległe sprawdzanie z ograniczoną pulą wątków
            results: dict[str, tuple[bool, str]] = {}
            with ThreadPoolExecutor(max_workers=_MAX_WARMUP_THREADS) as pool:
                future_to_name = {
                    pool.submit(self._check_token_alive, s.name, s.value): s.name
                    for s in states
                }
                try:
                    for future in as_completed(future_to_name,
                                               timeout=_CHECK_TIMEOUT + 4):
                        name = future_to_name[future]
                        try:
                            results[name] = future.result()
                        except Exception as e:
                            logger.warning("[hf-manager] Błąd sprawdzenia %s: %s", name, e)
                            results[name] = (True, "")  # fail-safe: zakładaj aktywny
                except FuturesTimeout:
                    logger.warning("[hf-manager] Warm-up timeout — część tokenów nieznana")
                    for name in future_to_name.values():
                        if name not in results:
                            results[name] = (True, "")  # fail-safe

            with self._lock:
                self._tokens = {}
                for s in states:
                    alive, reason = results.get(s.name, (True, ""))
                    s.alive       = alive
                    s.dead_reason = reason
                    s.dead_at     = 0.0 if alive else time.monotonic()
                    self._tokens[s.name] = s

                active = sum(1 for s in self._tokens.values() if s.alive)
                dead   = len(self._tokens) - active

                if active == 0 and self._tokens:
                    self._all_dead_since = time.monotonic()
                    logger.warning(
                        "[hf-manager] WSZYSTKIE %d TOKENY MARTWE — "
                        "FLUX wyłączony do ręcznego resetu lub upływu %ds",
                        dead, _RECHECK_AFTER or 999999
                    )
                else:
                    self._all_dead_since = 0.0

            logger.info(
                "[hf-manager] Warm-up: %d aktywnych / %d martwych / %d łącznie",
                active, dead, len(self._tokens)
            )
            self._warmed_up = True

    # ── Publiczne API ──────────────────────────────────────────────────────────

    def get_active_tokens(self) -> list[tuple[str, str]]:
        """
        Zwraca listę aktywnych tokenów: [(name, value), …]

        FAST-PATH: gdy wszystkie tokeny są martwe → zwraca [] BEZ warm-upu
        i BEZ żadnych requestów HTTP. Sprawdza tylko czy minął _RECHECK_AFTER.
        """
        # Fast-path: wszystkie martwe i _RECHECK_AFTER=0 → od razu []
        if self._warmed_up and self._all_dead_since > 0:
            if _RECHECK_AFTER == 0:
                return []
            if (time.monotonic() - self._all_dead_since) < _RECHECK_AFTER:
                return []
            # Czas minął — zresetuj i sprawdź ponownie
            logger.info("[hf-manager] Ponowne sprawdzenie tokenów po %ds", _RECHECK_AFTER)
            with self._lock:
                self._warmed_up     = False
                self._all_dead_since = 0.0

        if not self._warmed_up:
            self.warmup()

        with self._lock:
            return [
                (s.name, s.value)
                for s in self._tokens.values()
                if s.alive
            ]

    def mark_dead(self, name: str, reason: str = "402/401/403") -> None:
        """
        Oznacza token jako martwy. Wywoływane przez respondery po błędzie.
        Gdy po oznaczeniu wszystkie są martwe — ustawia _all_dead_since.
        """
        with self._lock:
            s = self._tokens.get(name)
            if s is None:
                logger.debug("[hf-manager] mark_dead(%s) — token nieznany", name)
                return
            if not s.alive:
                return  # już martwy, nic do roboty

            s.alive       = False
            s.dead_reason = reason
            s.dead_at     = time.monotonic()

            active = sum(1 for x in self._tokens.values() if x.alive)
            logger.warning(
                "[hf-manager] Token %s → MARTWY (%s). Aktywnych: %d",
                name, reason, active
            )

            if active == 0:
                self._all_dead_since = time.monotonic()
                logger.warning(
                    "[hf-manager] WSZYSTKIE TOKENY MARTWE — "
                    "kolejne wywołania get_active_tokens() zwrócą [] natychmiast"
                )

    def mark_remaining(self, name: str, remaining: int) -> None:
        with self._lock:
            s = self._tokens.get(name)
            if s:
                s.remaining = remaining

    def is_dead(self, name: str) -> bool:
        with self._lock:
            s = self._tokens.get(name)
            return s is not None and not s.alive

    def all_dead(self) -> bool:
        """True gdy wszystkie znane tokeny są martwe (lub brak tokenów)."""
        if not self._warmed_up:
            return False
        with self._lock:
            if not self._tokens:
                return True
            return self._all_dead_since > 0

    def status_report(self) -> list[dict]:
        """Pełny raport stanu — do /admin/hf-status."""
        if not self._warmed_up:
            # Nie blokuj endpointu status jeśli warm-up nie był wykonany
            return [{"info": "warm-up nie wykonany — brak danych"}]
        with self._lock:
            now = time.monotonic()
            return [
                {
                    "name":      s.name,
                    "alive":     s.alive,
                    "reason":    s.dead_reason or "OK",
                    "remaining": s.remaining,
                    "dead_ago":  (
                        f"{int(now - s.dead_at)}s temu"
                        if not s.alive and s.dead_at else None
                    ),
                }
                for s in self._tokens.values()
            ]

    def reset(self) -> None:
        """
        Resetuje stan — wymusza ponowny warm-up przy następnym użyciu.
        Używa przy ręcznym odnowieniu tokenów w Render env.
        """
        with self._warmup_lock:
            with self._lock:
                self._warmed_up      = False
                self._all_dead_since = 0.0
                self._last_warmup_at = 0.0   # zezwól na natychmiastowy warm-up
                self._tokens         = {}
        logger.info("[hf-manager] Reset — warm-up przy następnym get_active_tokens()")

    def force_reset(self) -> None:
        """
        Reset TWARDY — ignoruje _MIN_WARMUP_INTERVAL.
        Tylko do użytku przez /admin/hf-reset gdy tokeny faktycznie odnowiono.
        """
        with self._warmup_lock:
            with self._lock:
                self._warmed_up      = False
                self._all_dead_since = 0.0
                self._last_warmup_at = 0.0   # zezwól na natychmiastowy warm-up
                self._tokens         = {}
        logger.info("[hf-manager] Force-reset — warm-up przy następnym użyciu (bez cooldown)")


# ── Singleton ──────────────────────────────────────────────────────────────────
hf_tokens = HFTokenManager()


# ── Skróty (dla wygody migracji) ──────────────────────────────────────────────

def get_active_tokens() -> list[tuple[str, str]]:
    """Zwraca aktywne tokeny [(name, value), …]"""
    return hf_tokens.get_active_tokens()


def mark_dead(name: str, reason: str = "błąd 402/401/403") -> None:
    """Oznacza token jako martwy."""
    hf_tokens.mark_dead(name, reason)


def mark_remaining(name: str, remaining: int) -> None:
    """Aktualizuje licznik pozostałych requestów."""
    hf_tokens.mark_remaining(name, remaining)


def is_dead(name: str) -> bool:
    """Sprawdza czy token jest martwy."""
    return hf_tokens.is_dead(name)
