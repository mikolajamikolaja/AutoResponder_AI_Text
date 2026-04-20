"""
core/hf_token_manager.py
════════════════════════════════════════════════════════════════════════════════
Centralny menedżer tokenów Hugging Face — współdzielony przez WSZYSTKIE
respondery (zwykly, smierc, dociekliwy, …).

ZASADY:
  1. Stan tokenów (aktywne / martwe) jest JEDEN dla całego procesu serwera.
  2. Walidacja tokenu odbywa się przez lekki HEAD request (bez generowania
     obrazka) — endpoint whoami HF API lub sprawdzenie modelu.
  3. Warm-up jednorazowy przy pierwszym użyciu (lazy) — wynik cache'owany
     w pamięci przez całą sesję.
  4. Tokeny mogą być też oznaczone jako martwe w trakcie sesji (402/401/403)
     — każdy responder wywołuje mark_dead(name) zamiast pisać do własnego seta.
  5. Resety ręczne przez /admin/hf-status (opcjonalny endpoint Flask).

UŻYCIE:
    from core.hf_token_manager import hf_tokens, mark_dead, get_active_tokens

    # Pobierz listę aktywnych tokenów — [(name, value), ...]
    tokens = get_active_tokens()

    # Po błędzie 402/401/403 w responderze:
    mark_dead("HF_TOKEN3")

MIGRACJA z kodu respondentów:
  - Usuń lokalne  _HF_DEAD_TOKENS  i  _get_hf_tokens()
  - Zamień wywołania  _get_hf_tokens()      →  get_active_tokens()
  - Zamień  _HF_DEAD_TOKENS.add(name)       →  mark_dead(name)
  - Zamień  name not in _HF_DEAD_TOKENS     →  not is_dead(name)
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─── Stałe ────────────────────────────────────────────────────────────────────

# Endpoint do lekkiej weryfikacji tokenu (nie generuje obrazka, nie zużywa kredytów)
_HF_WHOAMI_URL = "https://huggingface.co/api/whoami"

# Alternatywny endpoint — sprawdza dostęp do modelu (tylko GET, brak kosztów)
_HF_MODEL_URL  = (
    "https://api-inference.huggingface.co/models/"
    "black-forest-labs/FLUX.1-schnell"
)

# Timeout lekkiego sprawdzenia (sekund)
_CHECK_TIMEOUT = 8

# Liczba tokenów do skanowania (HF_TOKEN, HF_TOKEN1 … HF_TOKEN{MAX-1})
_TOKEN_RANGE = 100

# Minimalny interwał ponownego sprawdzania MARTWEGO tokenu (sekundy).
# Ustaw na 0 jeśli nie chcesz automatycznego ponownego sprawdzania.
_RECHECK_INTERVAL = 0  # domyślnie bez automatycznego przywracania


# ─── Stan globalny (singleton w ramach procesu) ────────────────────────────────

class _TokenState:
    """Wewnętrzna struktura stanu jednego tokenu."""
    __slots__ = ("name", "value", "alive", "dead_reason", "dead_at", "remaining")

    def __init__(self, name: str, value: str):
        self.name        = name
        self.value       = value
        self.alive       = True          # domyślnie zakładamy aktywny
        self.dead_reason = ""
        self.dead_at     = 0.0
        self.remaining: Optional[int] = None  # z nagłówka X-Remaining-Requests


class HFTokenManager:
    """
    Singleton — jeden obiekt na cały proces Flask.
    Thread-safe (RLock).
    """

    def __init__(self):
        self._lock         = threading.RLock()
        self._tokens: dict[str, _TokenState] = {}
        self._warmed_up    = False
        self._warmup_lock  = threading.Lock()

    # ── Wczytywanie tokenów ze środowiska ─────────────────────────────────────

    def _load_from_env(self) -> list[_TokenState]:
        """Wczytuje tokeny ze zmiennych środowiskowych."""
        states = []
        names = ["HF_TOKEN"] + [f"HF_TOKEN{i}" for i in range(1, _TOKEN_RANGE)]
        for name in names:
            val = os.getenv(name, "").strip()
            if val:
                states.append(_TokenState(name, val))
        return states

    # ── Lekka weryfikacja jednego tokenu (bez generowania obrazka) ─────────────

    @staticmethod
    def _check_token_alive(name: str, value: str) -> tuple[bool, str]:
        """
        Sprawdza czy token jest aktywny przez lekki GET na whoami.

        Nie generuje obrazka, nie zużywa kredytów generatywnych.

        Zwraca: (alive: bool, reason: str)
        """
        headers = {"Authorization": f"Bearer {value}"}
        try:
            resp = requests.get(_HF_WHOAMI_URL, headers=headers, timeout=_CHECK_TIMEOUT)
            if resp.status_code == 200:
                user = resp.json().get("name", "?")
                logger.debug("[hf-manager] Token %s OK → użytkownik: %s", name, user)
                return True, ""
            elif resp.status_code in (401, 403):
                reason = f"Nieważny token (HTTP {resp.status_code})"
                logger.warning("[hf-manager] Token %s MARTWY: %s", name, reason)
                return False, reason
            elif resp.status_code == 429:
                # Rate limit na whoami — token prawdopodobnie żywy
                logger.warning("[hf-manager] Token %s: rate limit na whoami — zakładam aktywny", name)
                return True, ""
            else:
                reason = f"HTTP {resp.status_code}"
                logger.warning("[hf-manager] Token %s nieznany status: %s", name, reason)
                # Niepewny — zakładamy aktywny, żeby nie blokować niepotrzebnie
                return True, ""

        except requests.exceptions.Timeout:
            logger.warning("[hf-manager] Token %s: timeout sprawdzenia — zakładam aktywny", name)
            return True, ""
        except requests.exceptions.ConnectionError as e:
            logger.warning("[hf-manager] Token %s: brak połączenia (%s) — zakładam aktywny", name, e)
            return True, ""
        except Exception as e:
            logger.warning("[hf-manager] Token %s: błąd sprawdzenia: %s — zakładam aktywny", name, e)
            return True, ""

    # ── Warm-up (jednorazowy przy pierwszym użyciu) ────────────────────────────

    def warmup(self, force: bool = False) -> None:
        """
        Sprawdza wszystkie tokeny JEDEN raz na starcie sesji.
        Wywoływane leniwie (lazy) przy pierwszym get_active_tokens().
        Można wymusić przez force=True (np. po restarcie lub z endpointu /admin).

        Blokuje wątek wywołujący — przy starcie to OK, bo warm-up jest szybki
        (tylko GET whoami, równoległy dla każdego tokenu).
        """
        with self._warmup_lock:
            if self._warmed_up and not force:
                return

            states = self._load_from_env()
            if not states:
                logger.error("[hf-manager] Brak tokenów HF w zmiennych środowiskowych!")
                with self._lock:
                    self._tokens = {}
                    self._warmed_up = True
                return

            logger.info(
                "[hf-manager] Warm-up: sprawdzam %d tokenów HF (lekki HEAD, bez generowania)…",
                len(states)
            )

            # Równoległe sprawdzanie tokenów (threadpool)
            results: dict[str, tuple[bool, str]] = {}
            threads = []

            def _check(state: _TokenState):
                alive, reason = self._check_token_alive(state.name, state.value)
                results[state.name] = (alive, reason)

            for s in states:
                t = threading.Thread(target=_check, args=(s,), daemon=True)
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=_CHECK_TIMEOUT + 2)

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
            logger.info(
                "[hf-manager] Warm-up zakończony: %d aktywnych, %d martwych (z %d łącznie)",
                active, dead, len(self._tokens)
            )
            self._warmed_up = True

    # ── Publiczne API ──────────────────────────────────────────────────────────

    def get_active_tokens(self) -> list[tuple[str, str]]:
        """
        Zwraca listę aktywnych tokenów: [(name, value), …]

        Przy pierwszym wywołaniu wykonuje warm-up (jednorazowo).
        W trakcie sesji zwraca cached wynik — bez dodatkowych requestów.
        """
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
        Oznacza token jako martwy na czas sesji.
        Wywoływane przez respondery po otrzymaniu błędu 402/401/403.
        """
        with self._lock:
            if name in self._tokens:
                s = self._tokens[name]
                if s.alive:
                    s.alive       = False
                    s.dead_reason = reason
                    s.dead_at     = time.monotonic()
                    active = sum(1 for x in self._tokens.values() if x.alive)
                    logger.warning(
                        "[hf-manager] Token %s → MARTWY (%s). Pozostało aktywnych: %d",
                        name, reason, active
                    )
            else:
                # Token nie był w warm-upie (np. dodany po starcie) — dodaj jako martwy
                logger.warning("[hf-manager] mark_dead(%s) — token nieznany, ignoruję", name)

    def mark_remaining(self, name: str, remaining: int) -> None:
        """Aktualizuje licznik pozostałych requestów dla tokenu (z nagłówka X-Remaining-Requests)."""
        with self._lock:
            if name in self._tokens:
                self._tokens[name].remaining = remaining

    def is_dead(self, name: str) -> bool:
        """Zwraca True jeśli token jest na liście martwych."""
        with self._lock:
            s = self._tokens.get(name)
            return s is not None and not s.alive

    def status_report(self) -> list[dict]:
        """
        Zwraca pełny raport stanu tokenów (do logów / endpointu /admin/hf-status).
        """
        if not self._warmed_up:
            self.warmup()
        with self._lock:
            report = []
            for s in self._tokens.values():
                report.append({
                    "name":      s.name,
                    "alive":     s.alive,
                    "reason":    s.dead_reason or "OK",
                    "remaining": s.remaining,
                    "dead_ago":  (
                        f"{int(time.monotonic() - s.dead_at)}s temu"
                        if not s.alive and s.dead_at else None
                    ),
                })
            return report

    def reset(self) -> None:
        """
        Resetuje stan — wymusza ponowny warm-up przy następnym użyciu.
        Przydatne np. po ręcznym odnowieniu tokenów.
        """
        with self._warmup_lock:
            with self._lock:
                self._warmed_up = False
                self._tokens    = {}
        logger.info("[hf-manager] Stan tokenów zresetowany — warm-up przy następnym użyciu")

    def all_dead(self) -> bool:
        """Zwraca True gdy wszystkie znane tokeny są martwe."""
        if not self._warmed_up:
            return False  # Nie wiemy jeszcze
        with self._lock:
            if not self._tokens:
                return True
            return all(not s.alive for s in self._tokens.values())


# ── Singleton ──────────────────────────────────────────────────────────────────
hf_tokens = HFTokenManager()


# ── Skróty (dla wygody migracji) ──────────────────────────────────────────────

def get_active_tokens() -> list[tuple[str, str]]:
    """Skrót: zwraca aktywne tokeny [(name, value), …]"""
    return hf_tokens.get_active_tokens()


def mark_dead(name: str, reason: str = "błąd 402/401/403") -> None:
    """Skrót: oznacza token jako martwy."""
    hf_tokens.mark_dead(name, reason)


def mark_remaining(name: str, remaining: int) -> None:
    """Skrót: aktualizuje licznik pozostałych requestów."""
    hf_tokens.mark_remaining(name, remaining)


def is_dead(name: str) -> bool:
    """Skrót: sprawdza czy token jest martwy."""
    return hf_tokens.is_dead(name)
