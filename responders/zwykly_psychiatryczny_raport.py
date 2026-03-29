"""
responders/zwykly_psychiatryczny_raport.py

Moduł obsługujący CAŁY pipeline raportu psychiatrycznego.

ARCHITEKTURA FALLBACK (v4 — ATOMOWA):
  Dla każdego wywołania AI:
    1. Groq, klucz API_KEY_GROQ
    2. Groq, klucz API_KEY_GROQ ... API_KEY_GROQ_01 aż do  API_KEY_GROQ_09
    3. DeepSeek awaryjny (ten sam prompt)
    4. Pole = [BRAK DANYCH]   ← JEDYNA dopuszczalna wartość zastępcza

  Brak hardkodowanych fallbacków tekstowych. Brak statycznych opisów.
  Każde pole musi nawiązywać do emaila lub zawierać [BRAK DANYCH].

RÓWNOLEGŁOŚĆ:
  Runda 1 (niezależne): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
  Runda 2 (czekają na #2): #3 tydzień1 | #4 tydzień2 | #5 wypis
  Runda 3 (czeka na #3+#4): #7 zalecenia
  Potem: Groq #9 leczenie specjalne
  Potem: DeepSeek tone + completeness (sekwencyjnie, zawsze)
  Potem: FLUX oba zdjęcia równolegle
  Potem: DOCX + LOG

WYJŚCIE:
  {raport_pdf, psych_photo_1, psych_photo_2, log_psych}
"""

import os
import io
import re
import json
import base64
import random
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from flask import current_app  # używane tylko w _generate_photos_parallel

# Bezpieczny logger modułu — działa w wątkach bez kontekstu Flask
logger = logging.getLogger(__name__)

from core.ai_client import call_deepseek, MODEL_TYLER
from core.config import (
    GROQ_API_URL,
    GROQ_MODEL,
    HF_API_URL,
    HF_STEPS,
    HF_GUIDANCE,
    HF_TIMEOUT,
    MAX_DLUGOSC_EMAIL,
)

# ─────────────────────────────────────────────────────────────────────────────
# STAŁE
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
RAPORT_JSON = os.path.join(PROMPTS_DIR, "zwykly_raport.json")

# Marker atomowy — jedyna dopuszczalna wartość gdy brak danych z emaila
BRAK        = "[BRAK DANYCH]"
# Marker wewnętrzny zwracany przez Groq — zamieniany na BRAK przed DOCX
_GROQ_BRAK  = "__BRAK__"
# Marker dla nieunoszalnych przedmiotów
_NIEUNOSZALNE = "__NIEUNOSZALNE__"


# ─────────────────────────────────────────────────────────────────────────────
# LOG BUILDER
# ─────────────────────────────────────────────────────────────────────────────

class PsychLog:
    """
    Buduje log_psych.txt — kompletny zapis pipeline.
    Wysyłany użytkownikowi jako załącznik obok DOCX.
    """
    def __init__(self, sender_name: str, body: str):
        self._lines = []
        self._ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self._braki = []  # sekcje które dostały [BRAK DANYCH]
        self._header(sender_name, body)

    def _header(self, sender_name: str, body: str):
        self._lines += [
            "═" * 70,
            "LOG RAPORTU PSYCHIATRYCZNEGO",
            f"Data:     {self._ts}",
            f"Nadawca:  {sender_name or '(nieznany)'}",
            "═" * 70,
            "",
            "[1] TREŚĆ EMAILA WEJŚCIOWEGO",
            "─" * 70,
            body,
            "",
        ]

    def nouns_before(self, nouns_dict: dict):
        self._lines += [
            "[2] RZECZOWNIKI WEJŚCIOWE (przed filtrem fizyczności)",
            "─" * 70,
            json.dumps(nouns_dict, ensure_ascii=False, indent=2),
            "",
        ]

    def nouns_after(self, nouns_dict: dict):
        self._lines += [
            "[3] RZECZOWNIKI PO FILTRZE FIZYCZNOŚCI",
            "─" * 70,
            json.dumps(nouns_dict, ensure_ascii=False, indent=2),
            "",
        ]

    def sekcja(self, numer: int, nazwa: str, prompt: str,
               model: str, klucz: str, raw: str, wynik: str):
        self._lines += [
            f"[{numer}] SEKCJA: {nazwa}",
            "─" * 70,
            f"→ MODEL:  {model}",
            f"→ KLUCZ:  {klucz}",
            "→ PROMPT WYSŁANY:",
            prompt[:2000] + ("..." if len(prompt) > 2000 else ""),
            "→ ODPOWIEDŹ SUROWA:",
            (raw or "(brak)")[:3000] + ("..." if raw and len(raw) > 3000 else ""),
            f"→ WYNIK PARSOWANIA: {wynik}",
            "",
        ]

    def deepseek(self, rola: str, zmiany: list):
        self._lines += [
            f"[DS] DEEPSEEK {rola.upper()}",
            "─" * 70,
            f"→ Zmiany wprowadzone: {len(zmiany)}",
        ] + [f"   • {z}" for z in zmiany[:20]] + [""]

    def flux(self, photo1_ok: bool, photo2_ok: bool):
        self._lines += [
            "[FL] FLUX ZDJĘCIA",
            "─" * 70,
            f"→ photo_pacjent:    {'OK' if photo1_ok else 'BRAK'}",
            f"→ photo_przedmioty: {'OK' if photo2_ok else 'BRAK'}",
            "",
        ]

    def docx_info(self, rozmiar_kb: int, braki: list):
        self._braki = braki
        self._lines += [
            "[DC] DOCX",
            "─" * 70,
            f"→ Rozmiar: {rozmiar_kb} KB",
            f"→ Pola [BRAK DANYCH]: {len(braki)}",
        ] + [f"   • {b}" for b in braki] + [
            "",
            "═" * 70,
            "KONIEC LOGU",
            "═" * 70,
        ]

    def build(self) -> dict:
        """Zwraca dict z base64 pliku txt."""
        tekst = "\n".join(self._lines)
        b64 = base64.b64encode(tekst.encode("utf-8")).decode("ascii")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return {
            "base64":       b64,
            "content_type": "text/plain; charset=utf-8",
            "filename":     f"log_psych_{ts}.txt",
        }


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — konwersja markerów wewnętrznych
# ─────────────────────────────────────────────────────────────────────────────

def _zamien_braki(obj):
    """
    Rekurencyjnie zamienia '__BRAK__' i '__NIEUNOSZALNE__' na BRAK = '[BRAK DANYCH]'.
    Działa na dict, list, str.
    """
    if isinstance(obj, dict):
        return {k: _zamien_braki(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_zamien_braki(i) for i in obj]
    if isinstance(obj, str):
        if obj in (_GROQ_BRAK, _NIEUNOSZALNE):
            return BRAK
        return obj
    return obj


def _czy_brak(wartosc) -> bool:
    """Zwraca True jeśli wartość to marker braku."""
    if wartosc is None:
        return True
    if wartosc == BRAK:
        return True
    if isinstance(wartosc, str) and wartosc.strip() == "":
        return True
    if isinstance(wartosc, list) and all(
        isinstance(i, str) and i in (BRAK, _GROQ_BRAK, "") for i in wartosc
    ):
        return True
    return False


def _get_groq_keys() -> list:
    keys = []
    k = os.getenv("API_KEY_GROQ", "").strip()
    if k:
        keys.append(("API_KEY_GROQ", k))
    for i in range(1, 10):
        name = f"API_KEY_GROQ_{i:02d}"
        k = os.getenv(name, "").strip()
        if k:
            keys.append((name, k))
    return keys

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — Groq rotacja kluczy + DeepSeek awaryjny
# ─────────────────────────────────────────────────────────────────────────────

def _get_groq_keys() -> list:
    keys = []
    for i in range(40):
        name = f"GROQ_API_KEY{i}" if i else "GROQ_API_KEY"
        val  = os.getenv(name, "").strip()
        if val:
            keys.append((name, val))
    return keys


def _call_groq_single(key: str, system: str, user: str, max_tokens: int = 4096) -> str | None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "system", "content": system},
                        {"role": "user",   "content": user}],
        "max_tokens":  max_tokens,
        "temperature": 0.85,
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=45)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        if resp.status_code == 429:
            return "RATE_LIMIT"
        logger.warning("[psych-raport] Groq HTTP %d", resp.status_code)
    except Exception as e:
        logger.warning("[psych-raport] Groq wyjątek: %s", e)
    return None


def _call_ai_with_fallback(system: str, user: str, max_tokens: int = 4096,
                            section_name: str = "?",
                            log: PsychLog = None,
                            log_numer: int = 0) -> str | None:
    """
    Fallback chain:
      1. Wszystkie klucze Groq (rotacja, rate limit → następny klucz)
      2. DeepSeek awaryjny (ten sam prompt)
      3. None → pole dostanie [BRAK DANYCH]

    Loguje każdą próbę do PsychLog.
    """
    keys = _get_groq_keys()

    # ── Próba przez wszystkie klucze Groq ────────────────────────────────────
    for attempt in range(3):
        for key_name, key_val in keys:
            result = _call_groq_single(key_val, system, user, max_tokens)
            if result and result != "RATE_LIMIT":
                logger.info(
                    "[psych-raport] %s OK (groq/%s attempt=%d)", section_name, key_name, attempt + 1
                )
                if log:
                    log.sekcja(log_numer, section_name, user, f"groq/{GROQ_MODEL}",
                               key_name, result, "OK")
                return result
            if result == "RATE_LIMIT":
                logger.warning(
                    "[psych-raport] %s RATE_LIMIT klucz=%s → następny", section_name, key_name
                )
                continue
        logger.warning(
            "[psych-raport] %s — wszystkie Groq zawiodły attempt=%d/%d",
            section_name, attempt + 1, 3
        )

    # ── DeepSeek awaryjny ────────────────────────────────────────────────────
    logger.warning(
        "[psych-raport] %s → DeepSeek awaryjny", section_name
    )
    try:
        ds_result = call_deepseek(system, user, MODEL_TYLER)
        if ds_result:
            logger.info("[psych-raport] %s → DeepSeek OK", section_name)
            if log:
                log.sekcja(log_numer, section_name, user, "deepseek/awaryjny",
                           "API_KEY_DEEPSEEK", ds_result, "OK (DeepSeek awaryjny)")
            return ds_result
    except Exception as e:
        logger.error("[psych-raport] %s → DeepSeek wyjątek: %s", section_name, e)

    # ── Ostateczny fallback — brak danych ────────────────────────────────────
    logger.error(
        "[psych-raport] %s → BRAK DANYCH (wszystkie AI zawiodły)", section_name
    )
    if log:
        log.sekcja(log_numer, section_name, user, "BRAK", "BRAK", "",
                   "BRAK DANYCH — wszystkie AI zawiodły")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — parsowanie JSON
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_safe(raw: str, section: str) -> dict | list | None:
    """
    Wyciąga JSON z odpowiedzi AI. Obsługuje:
    - czysty JSON
    - JSON owinięty w ```json...```
    - JSON poprzedzony tekstem
    - ucięty JSON (próba naprawy)
    """
    if not raw:
        return None
    try:
        clean = re.sub(r'^```[a-z]*\s*', '', raw.strip(), flags=re.M)
        clean = re.sub(r'\s*```\s*$', '', clean, flags=re.M).strip()

        start_idx = next((i for i, ch in enumerate(clean) if ch in '{['), None)
        if start_idx is not None:
            clean = clean[start_idx:]

        end_idx = len(clean)
        for i in range(len(clean) - 1, -1, -1):
            if clean[i] in '}]':
                end_idx = i + 1
                break
        clean = clean[:end_idx]

        result = json.loads(clean.strip())
        logger.info("[psych-raport] JSON OK sekcja=%s", section)
        return result
    except Exception as e:
        logger.warning(
            "[psych-raport] JSON błąd sekcja=%s: %s | próba naprawy...", section, e
        )
        try:
            partial = re.sub(r'^```[a-z]*\s*', '', raw.strip(), flags=re.M)
            partial = re.sub(r'\s*```\s*$', '', partial, flags=re.M).strip()
            start = next((i for i, c in enumerate(partial) if c in '{['), None)
            if start is not None:
                partial = partial[start:]
                stack = []
                pairs = {'}': '{', ']': '['}
                closers = {'{': '}', '[': ']'}
                in_str = False
                escape = False
                last_valid = 0
                for i, ch in enumerate(partial):
                    if escape:
                        escape = False
                        continue
                    if ch == '\\' and in_str:
                        escape = True
                        continue
                    if ch == '"' and not escape:
                        in_str = not in_str
                        continue
                    if in_str:
                        continue
                    if ch in '{[':
                        stack.append(ch)
                    elif ch in '}]':
                        if stack and stack[-1] == pairs[ch]:
                            stack.pop()
                            if not stack:
                                last_valid = i + 1
                suffix = ''.join(closers[c] for c in reversed(stack))
                if suffix:
                    repaired = partial[:last_valid if last_valid else len(partial)] + suffix
                    result = json.loads(repaired)
                    logger.warning(
                        "[psych-raport] JSON naprawiony sekcja=%s (doklejono '%s')", section, suffix
                    )
                    return result
        except Exception as e2:
            logger.warning(
                "[psych-raport] JSON naprawa nieudana sekcja=%s: %s | raw=%.300s",
                section, e2, raw
            )
        return None


def _extract_list_from_result(result, min_len: int = 5) -> list | None:
    if isinstance(result, list) and len(result) >= min_len:
        return result
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list) and len(v) >= min_len:
                return v
        for v in result.values():
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and len(vv) >= min_len:
                        return vv
        for v in result.values():
            if isinstance(v, list) and len(v) > 0:
                return v
    return None


def _merge_dicts(base: dict, override: dict) -> dict:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override if override else base
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge_dicts(result[k], v)
        elif v is not None and v != "" and v != [] and v != {}:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ŁADOWANIE CFG
# ─────────────────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    try:
        with open(RAPORT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("[psych-raport] Brak zwykly_raport.json: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# KONTEKST EMAILA — wstrzykiwany do każdego promptu
# ─────────────────────────────────────────────────────────────────────────────

def _email_kontekst(body: str, sender_name: str = "", nouns_dict: dict = None,
                    previous_body: str = "", extra: str = "") -> str:
    lines = []

    if sender_name:
        first = sender_name.split()[0]
        lines.append(
            f"PACJENT — NADAWCA EMAILA: {sender_name} (imię: {first})\n"
            f"UWAGA: Jeśli email zaczyna się od 'Drogi/a X' — X jest ADRESATEM, NIE pacjentem. "
            f"Pacjentem jest zawsze '{sender_name}'.\n"
        )

    lines.append(f"EMAIL PACJENTA (PEŁNA TREŚĆ):\n{body[:MAX_DLUGOSC_EMAIL]}\n")

    akapity = [p.strip() for p in body.split('\n\n') if p.strip() and len(p.strip()) > 30]
    if akapity:
        cytaty = []
        for ap in akapity[:8]:
            pierwsze = ap.split('.')[0].strip()
            if len(pierwsze) > 20:
                cytaty.append(f'  • "{pierwsze}"')
        if cytaty:
            lines.append(
                "KLUCZOWE ZDANIA Z EMAILA (MUSZĄ być podstawą każdego opisu):\n"
                + "\n".join(cytaty) + "\n"
            )

    if nouns_dict:
        nouns_filtered = {k: v for k, v in nouns_dict.items()
                          if v not in (BRAK, _GROQ_BRAK, _NIEUNOSZALNE)}
        if nouns_filtered:
            nouns_str = ", ".join(nouns_filtered.values())
            lines.append(f"PRZEDMIOTY Z EMAILA (fizyczne reprezentacje): {nouns_str}\n")

    if previous_body and previous_body.strip() != body.strip():
        lines.append(
            f"POPRZEDNIA WIADOMOŚĆ OD PACJENTA:\n{previous_body[:800]}\n"
        )

    if extra:
        lines.append(extra)

    lines.append(
        "BEZWZGLĘDNY WYMÓG: Każde pole MUSI zawierać nawiązanie do konkretnego słowa z emaila. "
        "Jeśli nie możesz nawiązać — zwróć '__BRAK__' dla tego pola. ZAKAZ ogólników.\n"
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #0 — filtr rzeczowników fizycznych
# ─────────────────────────────────────────────────────────────────────────────

def _filtruj_rzeczowniki_fizyczne(cfg: dict, body: str, nouns_dict: dict,
                                   log: PsychLog) -> dict:
    if not nouns_dict:
        return nouns_dict
    sec = cfg.get("groq_0_filtr_rzeczownikow", {})
    system = sec.get("system", "")
    if not system:
        return nouns_dict

    nouns_str = json.dumps(nouns_dict, ensure_ascii=False)
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    user = (
        f"EMAIL PACJENTA (kontekst):\n{body[:800]}\n\n"
        f"LISTA RZECZOWNIKÓW DO PRZEFILTROWANIA:\n{nouns_str}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Przekształć nieunoszalne w fizyczne reprezentacje. Nieprzeliczalne → '__NIEUNOSZALNE__'. "
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 1000, "filtr_rzeczownikow", log, 4)
    if not raw:
        return nouns_dict

    result = _parse_json_safe(raw, "filtr_rzeczownikow")
    if not result or not isinstance(result, dict):
        return nouns_dict

    if "fizyczne_przedmioty" in result and isinstance(result["fizyczne_przedmioty"], dict):
        fizyczne = result["fizyczne_przedmioty"]
    else:
        fizyczne = {k: v for k, v in result.items() if re.match(r'^rzecz\d+$', k)}

    if not fizyczne:
        return nouns_dict

    # Usuń nieunoszalne z dict
    oczyszczone = {k: v for k, v in fizyczne.items()
                   if v not in (_NIEUNOSZALNE, BRAK)}

    logger.info(
        "[psych-raport] filtr_rzeczownikow OK — %d fizycznych (usunięto %d nieunoszalnych)",
        len(oczyszczone), len(fizyczne) - len(oczyszczone)
    )
    return oczyszczone if oczyszczone else nouns_dict


# ─────────────────────────────────────────────────────────────────────────────
# GROQ skierowanie
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_skierowanie(cfg: dict, body: str, sender_name: str,
                        nouns_dict: dict, log: PsychLog) -> dict:
    sec = cfg.get("groq_skierowanie", {})
    system = sec.get("system", "")
    if not system:
        return {"skierowanie": {k: BRAK for k in
                ["instytucja_kierujaca", "lekarz_kierujacy", "pacjent_imie_nazwisko",
                 "data_skierowania", "rozpzowanie_wstepne", "powody_skierowania",
                 "obserwacje_wstepne", "podpis_lekarza"]}}
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    kontekst = _email_kontekst(body, sender_name, nouns_dict)
    user = f"{kontekst}\nSCHEMAT JSON:\n{schema}\n\nZwróć TYLKO czysty JSON."
    raw = _call_ai_with_fallback(system, user, 1500, "skierowanie", log, 5)
    result = _parse_json_safe(raw, "skierowanie") if raw else None
    if not result:
        return {"skierowanie": {
            "instytucja_kierujaca": BRAK, "lekarz_kierujacy": BRAK,
            "pacjent_imie_nazwisko": sender_name or BRAK,
            "data_skierowania": datetime.now().strftime("%d.%m.%Y"),
            "rozpzowanie_wstepne": BRAK, "powody_skierowania": [BRAK],
            "obserwacje_wstepne": BRAK, "podpis_lekarza": BRAK
        }}
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #1 — dane pacjenta + powód + cytaty
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_pacjent(cfg: dict, body: str, sender_name: str,
                    nouns_dict: dict, log: PsychLog) -> dict:
    sec = cfg.get("groq_1_pacjent", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    kontekst = _email_kontekst(body, sender_name, nouns_dict)
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"PAMIĘTAJ:\n"
        f"  - imie_nazwisko = '{sender_name}' + wymyślone nazwisko z emaila\n"
        f"  - ZAKAZ 'Jan Emailowy'\n"
        f"  - cytaty = TYLKO parafrazy REALNYCH zdań z emaila\n"
        f"  - ZAKAZ cytatu 'Nie pamiętam co napisałem'\n"
        f"  - Brak danych z emaila → '__BRAK__'\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 2500, "pacjent", log, 6)
    result = _parse_json_safe(raw, "pacjent") if raw else None

    if not result:
        # Atomowy fallback — tylko sender_name, reszta BRAK
        return {
            "numer_historii_choroby": f"NY-2026-{random.randint(10000, 99999)}",
            "data_przyjecia": datetime.now().strftime("%d.%m.%Y"),
            "dane_pacjenta": {
                "imie_nazwisko": sender_name or BRAK,
                "wiek": BRAK,
                "adres": BRAK,
                "zawod": BRAK,
                "stan_cywilny": BRAK,
                "numer_ubezpieczenia": f"PL-PSY-{random.randint(10000, 99999)}",
            },
            "powod_przyjecia": BRAK,
            "cytaty_z_przyjecia": [BRAK],
        }

    result = _zamien_braki(result)

    # Zawsze podmień imię na sender_name
    if sender_name and isinstance(result.get("dane_pacjenta"), dict):
        dp = result["dane_pacjenta"]
        if not dp.get("imie_nazwisko") or "jan emailowy" in str(dp.get("imie_nazwisko", "")).lower():
            dp["imie_nazwisko"] = sender_name
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #2 — depozyt + leki
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_depozyt_leki(cfg: dict, body: str, nouns_dict: dict,
                          sender_name: str, log: PsychLog) -> dict:
    sec = cfg.get("groq_2_depozyt_leki", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    kontekst = _email_kontekst(body, sender_name, nouns_dict)
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"JEDEN lek per przedmiot. Dawkowanie żartobliwe z emaila. "
        f"Nieunoszalne przedmioty → pomiń. Brak danych → '__BRAK__'. "
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 3000, "depozyt_leki", log, 7)
    result = _parse_json_safe(raw, "depozyt_leki") if raw else None
    if not result:
        return {
            "depozyt": {"lista_przedmiotow": [BRAK], "protokol_depozytu": BRAK},
            "farmakologia": {
                "leki": [{"nazwa": BRAK, "rzeczownik_zrodlowy": BRAK,
                           "wskazanie": BRAK, "dawkowanie": BRAK}],
                "nota_farmaceutyczna": BRAK,
            }
        }
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #3/#4 — hospitalizacja tydzień 1 i 2
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_tydzien(cfg: dict, body: str, leki: list, tydzien: int,
                    data_przyjecia: str, sender_name: str,
                    nouns_dict: dict, log: PsychLog) -> list:
    sec_key = f"groq_{2 + tydzien}_tydzien{tydzien}"
    sec = cfg.get(sec_key, {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)

    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
    except Exception:
        base_date = datetime.now()
    start_day = (tydzien - 1) * 7 + 1
    daty = [(base_date + timedelta(days=start_day - 1 + i)).strftime("%d.%m.%Y")
            for i in range(7)]
    daty_str = "\n".join(f"Dzień {start_day + i}: {d}" for i, d in enumerate(daty))

    leki_str = json.dumps(leki, ensure_ascii=False, indent=2) if leki else "[]"
    kontekst = _email_kontekst(
        body, sender_name, nouns_dict,
        extra=(
            f"LISTA LEKÓW (rotuj dla dni):\n{leki_str}\n\n"
            f"DATY HOSPITALIZACJI:\n{daty_str}\n"
        )
    )
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON (tablica 7 obiektów dla dni {start_day}-{start_day+6}):\n{schema}\n\n"
        f"Każdy dzień INNE absurdalne zdarzenie nawiązujące do emaila. "
        f"ZAKAZ nudnych zdarzeń. Brak materiału z emaila → '__BRAK__'. "
        f"Zwróć TYLKO czysty JSON z tablicą 7 obiektów."
    )
    section_name = f"tydzien{tydzien}"
    raw = _call_ai_with_fallback(system, user, 4096, section_name, log, 8 + tydzien - 1)

    if not raw:
        return _fallback_dni_brak(start_day, daty)

    result = _parse_json_safe(raw, section_name)
    if result is None:
        logger.error("[psych-raport] %s parse None", section_name)
        return _fallback_dni_brak(start_day, daty)

    lista = _extract_list_from_result(result, min_len=5)
    if lista is not None:
        return [_zamien_braki(d) for d in lista]

    return _fallback_dni_brak(start_day, daty)


def _fallback_dni_brak(start_day: int, daty: list) -> list:
    """Atomowy fallback — wszystkie pola [BRAK DANYCH]."""
    return [
        {
            "dzien": start_day + i,
            "data": daty[i],
            "zdarzenie": BRAK,
            "lek": BRAK,
            "stan_pacjenta": BRAK,
            "nota_lekarska": BRAK,
        }
        for i in range(7)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #5 — wypis
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_wypis(cfg: dict, body: str, data_przyjecia: str,
                  sender_name: str, nouns_dict: dict, log: PsychLog) -> dict:
    sec = cfg.get("groq_5_wypis", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
        data_wypisu = (base_date + timedelta(days=14)).strftime("%d.%m.%Y")
    except Exception:
        data_wypisu = datetime.now().strftime("%d.%m.%Y")
    kontekst = _email_kontekst(
        body, sender_name, nouns_dict,
        extra=f"DATA WYPISU (dzień 15): {data_wypisu}\n"
    )
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Każde zalecenie MUSI nawiązywać do emaila. Brak danych → '__BRAK__'. "
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 2000, "wypis", log, 10)
    result = _parse_json_safe(raw, "wypis") if raw else None
    if not result:
        return {"wypis": {
            "dzien_wypisu": f"Dzień 15, {data_wypisu}",
            "stan_przy_wypisie": BRAK,
            "powod_wypisu": BRAK,
            "zalecenia_po_wypisie": [BRAK],
            "opis_pozegnania": BRAK,
        }}
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #6 — diagnozy łacińskie
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_diagnozy(cfg: dict, body: str, previous_body: str,
                     sender_name: str, nouns_dict: dict, log: PsychLog) -> dict:
    sec = cfg.get("groq_6_diagnozy_lacina", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    kontekst = _email_kontekst(body, sender_name, nouns_dict, previous_body)
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Nazwy łacińskie MUSZĄ zawierać rdzeń słowa z emaila. "
        f"Opis kliniczny — każde zdanie nawiązuje do emaila. Brak → '__BRAK__'. "
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 2000, "diagnozy", log, 11)
    result = _parse_json_safe(raw, "diagnozy") if raw else None
    if not result:
        return {
            "diagnoza_wstepna": {
                "nazwa_lacinska": BRAK, "nazwa_polska": BRAK,
                "kod_dsm": BRAK, "opis_kliniczny": BRAK,
            },
            "diagnoza_dodatkowa": {
                "nazwa_lacinska": BRAK, "nazwa_polska": BRAK,
                "kod_dsm": BRAK, "opis_kliniczny": BRAK,
            },
            "objawy": [BRAK],
        }
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #7 — zalecenia + notatki + rokowanie + incydenty
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_zalecenia(cfg: dict, body: str, dni_1_7: list, dni_8_14: list,
                      sender_name: str, nouns_dict: dict, log: PsychLog) -> dict:
    sec = cfg.get("groq_7_zalecenia_notatki", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)

    zdarzenia = []
    for d in (dni_1_7 or []) + (dni_8_14 or []):
        if isinstance(d, dict) and d.get("zdarzenie") and d["zdarzenie"] != BRAK:
            zdarzenia.append(f"Dzień {d.get('dzien', '?')}: {str(d['zdarzenie'])[:200]}")
    zdarzenia_str = "\n".join(zdarzenia[:12]) if zdarzenia else "(brak)"

    kontekst = _email_kontekst(
        body, sender_name, nouns_dict,
        extra=f"ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n"
    )
    user = (
        f"{kontekst}\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zadania Tylera nawiązują do konkretnych przedmiotów z emaila. "
        f"Notatki pielęgniarek nawiązują do zdarzeń. Min. 10 notatek pielęgniarek. "
        f"Min. 10 notatek sprzątaczki. Min. 10 incydentów. Brak → '__BRAK__'. "
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 3000, "zalecenia", log, 12)
    result = _parse_json_safe(raw, "zalecenia") if raw else None
    if not result:
        return {
            "zalecenia_tylera": {
                "naglowek": "RACHUNEK ZA WYZWOLENIE",
                "zadanie_1": BRAK, "zadanie_2": BRAK, "zadanie_3": BRAK,
                "podpis": "Tyler Durden",
            },
            "rokowanie": BRAK,
            "notatki_pielegniarek": [{"imie_pielegniarki": BRAK, "data": "", "tresc": BRAK}],
            "notatki_sprzataczki": [{"data": "", "tresc": BRAK}],
            "incydenty_specjalne": [BRAK],
        }
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #8 — prompty FLUX
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_flux_prompty(cfg: dict, body: str, nouns_dict: dict,
                          sender_name: str, gender: str, log: PsychLog) -> dict:
    sec = cfg.get("groq_8_flux_prompty", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    nouns_filtered = {k: v for k, v in (nouns_dict or {}).items()
                      if v not in (BRAK, _GROQ_BRAK, _NIEUNOSZALNE)}
    nouns_str = ", ".join(nouns_filtered.values()) if nouns_filtered else "personal belongings"
    user = (
        f"EMAIL PACJENTA:\n{body[:800]}\n\n"
        f"PRZEDMIOTY Z EMAILA: {nouns_str}\n"
        f"PŁEĆ PACJENTA: {gender}\n"
        f"IMIĘ PACJENTA: {sender_name or 'unknown'}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON z dwoma promptami po angielsku."
    )
    raw = _call_ai_with_fallback(system, user, 600, "flux_prompty", log, 13)
    result = _parse_json_safe(raw, "flux_prompty") if raw else None
    if not result or not isinstance(result, dict):
        objects_critical = f"CRITICAL OBJECTS: {nouns_str}."
        return {
            "prompt_pacjent": (
                f"{objects_critical} Top-down documentary photo of a round wooden psychiatric "
                f"examination table. Four Polaroid photos scattered, each showing "
                f"a {gender} in a white canvas straitjacket surrounded by: {nouns_str}. "
                f"Faded desaturated colors, 35mm grain, 1990s documentary style."
            ),
            "prompt_przedmioty": (
                f"{objects_critical} Top-down clinical evidence photo. Metal hospital tray "
                f"with objects laid out as evidence: {nouns_str}. Numbered evidence tags. "
                f"Cold fluorescent lighting, 1990s police evidence room, hyper-realistic."
            )
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #9 — leczenie specjalne
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_leczenie_specjalne(cfg: dict, body: str, res_text: str,
                                sender_name: str, nouns_dict: dict,
                                log: PsychLog) -> dict:
    sec = cfg.get("groq_9_leczenie_specjalne", {})
    system = sec.get("system", "")
    if not system:
        return {}
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    kontekst = _email_kontekst(body, sender_name, nouns_dict)

    zasady_raw = ""
    if res_text and "### TYLER DURDEN" in res_text:
        tyler_sec = res_text.split("### TYLER DURDEN", 1)[1]
        zasady_raw = tyler_sec[:3000]
    elif res_text:
        zasady_raw = res_text[:3000]

    user = (
        f"{kontekst}\n"
        f"ZASADY I MANIFESTY TYLERA DURDENA:\n{zasady_raw}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Na podstawie POWYŻSZYCH ZASAD TYLERA stwórz 8 metod terapeutycznych. "
        f"Każda cytuje konkretną zasadę Tylera z powyższego tekstu. "
        f"Każda nawiązuje do emaila. Styl: Szwejk + Monty Python. "
        f"Brak → '__BRAK__'. Zwróć TYLKO czysty JSON."
    )
    raw = _call_ai_with_fallback(system, user, 3500, "leczenie_specjalne", log, 14)
    result = _parse_json_safe(raw, "leczenie_specjalne") if raw else None

    if not result:
        return {
            "leczenie_specjalne": {
                "tytul": "PROTOKÓŁ LECZENIA SPECJALNEGO WG METODY DURDEN",
                "wstep": BRAK,
                "zasady": [{"numer": i, "zasada_tylera": BRAK,
                             "metoda_terapeutyczna": BRAK,
                             "dawkowanie": BRAK,
                             "podpis_komisji": "Dr. T. Durden"} for i in range(1, 9)],
                "zamkniecie": BRAK,
            }
        }
    return _zamien_braki(result)


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK — tone + completeness (zawsze, niezależnie od Groq)
# ─────────────────────────────────────────────────────────────────────────────

def _deepseek_tone_check(cfg: dict, raport: dict, log: PsychLog) -> dict:
    sec = cfg.get("deepseek_1_tone_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))
    raport_json = json.dumps(raport, ensure_ascii=False, separators=(',', ':'))
    max_chars = 12000
    if len(raport_json) > max_chars:
        raport_json = raport_json[:max_chars] + "...}"
    user = (
        f"RAPORT DO OCENY:\n{raport_json}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Pola '[BRAK DANYCH]' — zostaw bez zmian. "
        f"Zwróć TYLKO czysty JSON z poprawkami."
    )
    logger.info("[psych-raport] DeepSeek tone check START")
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        log.deepseek("tone_check", ["brak odpowiedzi — skip"])
        return raport
    result = _parse_json_safe(raw, "deepseek_tone")
    if not result or not isinstance(result, dict):
        log.deepseek("tone_check", ["parse error — skip"])
        return raport
    merged = _merge_dicts(raport, result)
    zmiany = [k for k in result if k in raport and result[k] != raport.get(k)]
    log.deepseek("tone_check", zmiany)
    return merged


def _deepseek_completeness_check(cfg: dict, raport: dict, body: str,
                                  log: PsychLog) -> dict:
    sec = cfg.get("deepseek_2_completeness_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))
    raport_json = json.dumps(raport, ensure_ascii=False, separators=(',', ':'))
    max_chars = 12000
    if len(raport_json) > max_chars:
        raport_json = raport_json[:max_chars] + "...}"
    user = (
        f"ORYGINALNY EMAIL:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"RAPORT DO SPRAWDZENIA:\n{raport_json}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Pola '[BRAK DANYCH]' — zostaw bez zmian. "
        f"Zwróć TYLKO czysty JSON z uzupełnieniami."
    )
    logger.info("[psych-raport] DeepSeek completeness check START")
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        log.deepseek("completeness_check", ["brak odpowiedzi — skip"])
        return raport
    result = _parse_json_safe(raw, "deepseek_completeness")
    if not result or not isinstance(result, dict):
        log.deepseek("completeness_check", ["parse error — skip"])
        return raport
    merged = _merge_dicts(raport, result)
    zmiany = [k for k in result if k in raport and result[k] != raport.get(k)]
    log.deepseek("completeness_check", zmiany)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# FLUX
# ─────────────────────────────────────────────────────────────────────────────

# Globalny set tokenów HF wyczerpanych (402/401/403) w tej sesji — shared z zwykly.py
# Importujemy ze zwykly żeby czarna lista była wspólna
try:
    from responders.zwykly import _HF_DEAD_TOKENS
except ImportError:
    _HF_DEAD_TOKENS: set = set()


def _get_hf_tokens() -> list:
    """Pobiera tokeny HF pomijając te na czarnej liście sesji."""
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(40)]
    all_tokens = [(n, v) for n in names if (v := os.getenv(n, "").strip())]
    active = [(n, v) for n, v in all_tokens if n not in _HF_DEAD_TOKENS]
    if len(all_tokens) != len(active):
        logger.debug("[psych-flux] Pomijam %d martwych tokenów HF", len(all_tokens) - len(active))
    return active


def _generate_flux(prompt: str, label: str,
                   steps: int = 28, guidance: float = 7.0,
                   width: int = 1024, height: int = 1024) -> str | None:
    tokens = _get_hf_tokens()
    if not tokens:
        logger.error("[psych-flux] Brak tokenów HF dla %s", label)
        return None

    seed = random.randint(0, 2 ** 32 - 1)
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "width": width,
            "height": height,
            "seed": seed,
        }
    }

    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(HF_API_URL, headers=headers, json=payload, timeout=HF_TIMEOUT)
            if resp.status_code == 200:
                logger.info("[psych-flux] %s OK token=%s", label, name)
                try:
                    from PIL import Image as PILImage
                    pil = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
                    buf = io.BytesIO()
                    pil.save(buf, format="JPEG", quality=92, optimize=True)
                    return base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:
                    return base64.b64encode(resp.content).decode("ascii")
            elif resp.status_code == 402:
                _HF_DEAD_TOKENS.add(name)
                logger.warning("[psych-flux] 402 token=%s — wyczerpane kredyty, czarna lista (%d łącznie)",
                               name, len(_HF_DEAD_TOKENS))
            elif resp.status_code in (401, 403):
                _HF_DEAD_TOKENS.add(name)
                logger.warning("[psych-flux] HTTP %d token=%s — nieważny, czarna lista",
                               resp.status_code, name)
            elif resp.status_code == 429:
                continue
            else:
                logger.warning("[psych-flux] HTTP %d token=%s", resp.status_code, name)
        except Exception as e:
            logger.warning("[psych-flux] %s wyjątek token=%s: %s", label, name, e)

    return None


def _generate_photos_parallel(prompt_pacjent: str, prompt_przedmioty: str,
                               log: PsychLog) -> tuple:
    from flask import current_app as flask_app
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    app_obj = flask_app._get_current_object()

    def gen_pacjent():
        with app_obj.app_context():
            return _generate_flux(prompt_pacjent, "photo_pacjent")

    def gen_przedmioty():
        with app_obj.app_context():
            return _generate_flux(prompt_przedmioty, "photo_przedmioty")

    b64_pacjent = b64_przedmioty = None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(gen_pacjent)
            f2 = ex.submit(gen_przedmioty)
            try:
                b64_pacjent = f1.result(timeout=120)
            except Exception as e:
                with app_obj.app_context():
                    app_obj.logger.warning("[psych-flux] photo_pacjent błąd: %s", e)
            try:
                b64_przedmioty = f2.result(timeout=120)
            except Exception as e:
                with app_obj.app_context():
                    app_obj.logger.warning("[psych-flux] photo_przedmioty błąd: %s", e)
    except Exception as e:
        with app_obj.app_context():
            app_obj.logger.error("[psych-flux] ThreadPoolExecutor błąd: %s", e)

    log.flux(bool(b64_pacjent), bool(b64_przedmioty))

    def _wrap(b64, suffix):
        if not b64:
            return None
        return {"base64": b64, "content_type": "image/jpeg",
                "filename": f"psych_{suffix}_{ts}.jpg"}

    return _wrap(b64_pacjent, "pacjent"), _wrap(b64_przedmioty, "przedmioty")


# ─────────────────────────────────────────────────────────────────────────────
# DOCX BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_docx(raport: dict, photo_pacjent_b64: str | None,
                photo_przedmioty_b64: str | None, cfg: dict,
                log: PsychLog) -> str | None:
    try:
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError as e:
        logger.error("[psych-docx] Brak python-docx: %s", e)
        return None

    szpital = cfg.get("szpital", {})
    doc = Document()

    for section in doc.sections:
        section.top_margin    = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin   = Cm(3.0)
        section.right_margin  = Cm(2.5)

    BLACK = RGBColor(0x0A, 0x0A, 0x0A)
    DKRED = RGBColor(0x7A, 0x0F, 0x0F)
    GREY  = RGBColor(0x55, 0x55, 0x55)
    LGREY = RGBColor(0x99, 0x99, 0x99)
    FADED = RGBColor(0x33, 0x33, 0x33)
    RED   = RGBColor(0xCC, 0x00, 0x00)   # kolor [BRAK DANYCH]

    TW = "Courier New"
    TS = 10

    def _font(run, name=TW, size=TS, bold=False, italic=False, color=BLACK):
        run.font.name = name
        run.font.size = Pt(size)
        run.bold = bold
        run.italic = italic
        run.font.color.rgb = color

    def maszyna(text, bold=False, italic=False, color=BLACK,
                size=TS, space_before=0, space_after=3):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after  = Pt(space_after)
        tekst = str(text) if text else ""
        # Obsługa [BRAK DANYCH] — czerwony
        if tekst == BRAK:
            r = p.add_run(BRAK)
            _font(r, bold=True, color=RED, size=size)
        else:
            r = p.add_run(tekst)
            _font(r, bold=bold, italic=italic, color=color, size=size)
        return p

    def naglowek(text, color=DKRED, size=11):
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run(text.upper())
        _font(r, bold=True, color=color, size=size)
        sep = doc.add_paragraph()
        sep.paragraph_format.space_before = Pt(0)
        sep.paragraph_format.space_after  = Pt(4)
        r2 = sep.add_run("=" * 68)
        _font(r2, color=LGREY, size=7)
        return p

    def podnaglowek(text, color=FADED, size=10):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run(f"--- {text} ---")
        _font(r, italic=True, color=color, size=size)
        return p

    def pole(label, value, size=TS):
        tekst = str(value) if value else ""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(1)
        rl = p.add_run(f"{label.upper()}: ")
        _font(rl, bold=True, size=size)
        if tekst == BRAK:
            rv = p.add_run(BRAK)
            _font(rv, bold=True, color=RED, size=size)
        else:
            rv = p.add_run(tekst)
            _font(rv, size=size)

    def separator():
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run("- " * 34)
        _font(r, color=LGREY, size=7)

    def punkt_listy(text, numer=None, size=TS):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(2)
        p.paragraph_format.left_indent  = Cm(0.8)
        prefix = f"{numer}." if numer else "  *"
        tekst = str(text)
        r = p.add_run(f"{prefix}  {tekst}")
        if tekst == BRAK:
            _font(r, bold=True, color=RED, size=size)
        else:
            _font(r, size=size)

    def cytat_blok(text, size=9):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after  = Pt(4)
        p.paragraph_format.left_indent  = Cm(1.2)
        p.paragraph_format.right_indent = Cm(0.5)
        tekst = str(text)
        r = p.add_run(tekst)
        if tekst == BRAK:
            _font(r, bold=True, color=RED, size=size)
        else:
            _font(r, italic=True, color=GREY, size=size)

    def nota_kursywa(text, size=9):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(6)
        p.paragraph_format.left_indent  = Cm(0.6)
        tekst = str(text)
        r = p.add_run(f"[Nota: {tekst}]")
        if BRAK in tekst:
            _font(r, bold=True, color=RED, size=size)
        else:
            _font(r, italic=True, color=GREY, size=size)

    def podpis_odrecznie(text, size=16):
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        r = p.add_run(text)
        r.font.name = "Courier New"
        r.font.size = Pt(size)
        r.bold = True
        r.italic = True
        r.font.color.rgb = DKRED

    def insert_photo(b64: str, caption: str, width_cm: float = 13.0):
        if not b64:
            return
        try:
            img_bytes = base64.b64decode(b64)
            stream = io.BytesIO(img_bytes)
            doc.add_picture(stream, width=Cm(width_cm))
            cap = doc.add_paragraph(caption)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in cap.runs:
                _font(r, italic=True, color=GREY, size=8)
        except Exception as e:
            logger.warning("[psych-docx] Błąd zdjęcia: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # NAGŁÓWEK
    # ══════════════════════════════════════════════════════════════════════════
    p_nazwa = doc.add_paragraph()
    p_nazwa.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_nazwa = p_nazwa.add_run(szpital.get("nazwa", "Szpital Psychiatryczny im. Tylera Durdena").upper())
    _font(r_nazwa, bold=True, size=13)

    p_adr = doc.add_paragraph()
    p_adr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_adr = p_adr.add_run(szpital.get("adres", ""))
    _font(r_adr, size=9, color=GREY)

    p_odd = doc.add_paragraph()
    p_odd.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_odd = p_odd.add_run(szpital.get("oddzial", ""))
    _font(r_odd, size=9, color=GREY)

    doc.add_paragraph()
    p_tyt = doc.add_paragraph()
    p_tyt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_tyt = p_tyt.add_run("HISTORIA CHOROBY — KARTA PRZYJECIA I HOSPITALIZACJI")
    _font(r_tyt, bold=True, size=11)

    nr = raport.get("numer_historii_choroby", f"NY-2026-{random.randint(10000,99999)}")
    data_przyj = raport.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    p_nr = doc.add_paragraph()
    p_nr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_nr = p_nr.add_run(
        f"Nr: {nr}   |   Data przyjecia: {data_przyj}   |   "
        f"Lekarz: {szpital.get('lekarz', 'Dr. T. Durden')}"
    )
    _font(r_nr, size=9, color=GREY)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SKIEROWANIE
    # ══════════════════════════════════════════════════════════════════════════
    skier = raport.get("skierowanie", {})
    if isinstance(skier, dict) and skier.get("instytucja_kierujaca"):
        naglowek("SKIEROWANIE DO SZPITALA PSYCHIATRYCZNEGO")
        pole("Instytucja kierująca", skier.get("instytucja_kierujaca", BRAK))
        pole("Lekarz kierujący", skier.get("lekarz_kierujacy", BRAK))
        pole("Pacjent", skier.get("pacjent_imie_nazwisko", BRAK))
        pole("Data skierowania", skier.get("data_skierowania", BRAK))
        pole("Rozpoznanie wstępne", skier.get("rozpzowanie_wstepne", BRAK))

        powody = skier.get("powody_skierowania", [])
        if powody:
            doc.add_paragraph()
            podnaglowek("Powody skierowania")
            if isinstance(powody, list):
                for i, p in enumerate(powody, 1):
                    punkt_listy(str(p), numer=i, size=9)
            else:
                maszyna(str(powody), size=9)

        obs = skier.get("obserwacje_wstepne", "")
        if obs:
            doc.add_paragraph()
            podnaglowek("Obserwacje wstępne")
            maszyna(obs, size=9, space_after=4)

        podpis_skier = skier.get("podpis_lekarza", "")
        if podpis_skier and podpis_skier != BRAK:
            doc.add_paragraph()
            maszyna("Podpisano:", bold=True, size=9)
            podpis_odrecznie(podpis_skier, size=14)
        separator()

    # ══════════════════════════════════════════════════════════════════════════
    # I. DANE PACJENTA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("I. DANE PACJENTA")
    dp = raport.get("dane_pacjenta", {})
    pole("Imię i nazwisko",  dp.get("imie_nazwisko", BRAK))
    pole("Wiek",             dp.get("wiek", BRAK))
    pole("Adres",            dp.get("adres", BRAK))
    pole("Zawód",            dp.get("zawod", BRAK))
    pole("Stan cywilny",     dp.get("stan_cywilny", BRAK))
    pole("Nr ubezpieczenia", dp.get("numer_ubezpieczenia", BRAK))

    if photo_pacjent_b64:
        doc.add_paragraph()
        podnaglowek("DOKUMENTACJA FOTOGRAFICZNA — PRZYJĘCIE")
        insert_photo(photo_pacjent_b64,
                     "Fot. 1 — Pacjent w kaftanie bezpieczenstwa. Oddzial B. Material dowodowy.")
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # II. POWÓD PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("II. POWOD PRZYJECIA")
    powod = raport.get("powod_przyjecia", BRAK)
    maszyna(powod, size=10, space_after=4)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # III. CYTATY Z IZBY PRZYJĘĆ
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("III. CYTATY Z IZBY PRZYJEC")
    cytaty = raport.get("cytaty_z_przyjecia", [BRAK])
    if isinstance(cytaty, list):
        for i, c in enumerate(cytaty, 1):
            maszyna(f"[{i}]", bold=True, size=9, space_after=0)
            cytat_blok(str(c), size=9)
    elif cytaty:
        cytat_blok(str(cytaty), size=9)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # IV. PROTOKÓŁ DEPOZYTU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("IV. PROTOKOL DEPOZYTU — PRZEDMIOTY SKONFISKOWANE")
    dep = raport.get("depozyt", {})
    if isinstance(dep, dict):
        lista = dep.get("lista_przedmiotow", [BRAK])
        proto = dep.get("protokol_depozytu", BRAK)
        if lista:
            for i, item in enumerate(lista, 1):
                punkt_listy(str(item), numer=i, size=9)
        if proto:
            doc.add_paragraph()
            nota_kursywa(proto, size=9)

    if photo_przedmioty_b64:
        doc.add_paragraph()
        podnaglowek("DOKUMENTACJA FOTOGRAFICZNA — DOWODY RZECZOWE")
        insert_photo(photo_przedmioty_b64,
                     "Fot. 2 — Przedmioty skonfiskowane. Protokol dowodow rzeczowych.")
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # V. FARMAKOLOGIA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("V. FARMAKOLOGIA — PELNA LISTA LEKOW")
    farm = raport.get("farmakologia", {})
    leki_lista = farm.get("leki", []) if isinstance(farm, dict) else []

    if leki_lista:
        for i, lek in enumerate(leki_lista, 1):
            if not isinstance(lek, dict):
                continue
            doc.add_paragraph()
            p_lek = doc.add_paragraph()
            p_lek.paragraph_format.space_before = Pt(4)
            p_lek.paragraph_format.space_after  = Pt(1)
            p_lek.paragraph_format.left_indent  = Cm(0.4)
            nazwa = str(lek.get("nazwa", BRAK)).upper()
            r_lek = p_lek.add_run(f"{i}.  {nazwa}")
            if BRAK in nazwa:
                _font(r_lek, bold=True, color=RED, size=10)
            else:
                _font(r_lek, bold=True, size=10)

            for field_label, field_key in [
                ("Przedmioty odebrane", "rzeczownik_zrodlowy"),
                ("Wskazanie", "wskazanie"),
                ("Dawkowanie", "dawkowanie"),
            ]:
                val = str(lek.get(field_key, BRAK))
                p_f = doc.add_paragraph()
                p_f.paragraph_format.left_indent = Cm(1.2)
                p_f.paragraph_format.space_after = Pt(0)
                r_fl = p_f.add_run(f"{field_label}: ")
                _font(r_fl, bold=True, size=9)
                r_fv = p_f.add_run(val)
                if val == BRAK:
                    _font(r_fv, bold=True, color=RED, size=9)
                else:
                    _font(r_fv, size=9)

    nota_farm = farm.get("nota_farmaceutyczna", "") if isinstance(farm, dict) else ""
    if nota_farm:
        doc.add_paragraph()
        nota_kursywa(nota_farm, size=9)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # VI. PRZEBIEG HOSPITALIZACJI
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VI. PRZEBIEG HOSPITALIZACJI — 14 DNI")
    dni_all = (raport.get("hospitalizacja_tydzien_1", []) or []) + \
              (raport.get("hospitalizacja_tydzien_2", []) or [])

    for d in dni_all:
        if not isinstance(d, dict):
            continue
        dzien = d.get("dzien", "?")
        data  = d.get("data", "")

        p_dzien = doc.add_paragraph()
        p_dzien.paragraph_format.space_before = Pt(8)
        p_dzien.paragraph_format.space_after  = Pt(1)
        r_dzien = p_dzien.add_run(f"DZIEN {dzien}   /   {data}")
        _font(r_dzien, bold=True, size=10, color=DKRED)

        zdarz = d.get("zdarzenie", BRAK)
        maszyna(zdarz, size=9, space_before=2, space_after=2)

        for field_label, field_key in [("Podano", "lek"), ("Ocena", "stan_pacjenta")]:
            val = str(d.get(field_key, BRAK))
            p_f = doc.add_paragraph()
            p_f.paragraph_format.left_indent = Cm(0.5)
            p_f.paragraph_format.space_after = Pt(1)
            r_fl = p_f.add_run(f"{field_label}: ")
            _font(r_fl, bold=True, size=9)
            r_fv = p_f.add_run(val)
            if val == BRAK:
                _font(r_fv, bold=True, color=RED, size=9)
            else:
                _font(r_fv, size=9, color=GREY)

        nota = d.get("nota_lekarska", "")
        if nota and nota != BRAK:
            nota_kursywa(nota, size=8)
        separator()

    # ══════════════════════════════════════════════════════════════════════════
    # VII. KARTA WYPISU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VII. KARTA WYPISU")
    wypis = raport.get("wypis", {})
    if isinstance(wypis, dict):
        pole("Dzień wypisu", wypis.get("dzien_wypisu", BRAK))
        pole("Powód wypisu", wypis.get("powod_wypisu", BRAK))

        stan_wip = wypis.get("stan_przy_wypisie", BRAK)
        doc.add_paragraph()
        podnaglowek("Stan pacjenta przy wypisie")
        maszyna(stan_wip, size=10, space_after=4)

        doc.add_paragraph()
        podnaglowek("Zalecenia po wypisie")
        zal = wypis.get("zalecenia_po_wypisie", [BRAK])
        if isinstance(zal, list):
            for i, z in enumerate(zal, 1):
                punkt_listy(str(z), numer=i, size=9)
        elif zal:
            maszyna(str(zal), size=9)

        poz = wypis.get("opis_pozegnania", BRAK)
        doc.add_paragraph()
        cytat_blok(poz, size=9)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # VIII. DIAGNOZA PSYCHIATRYCZNA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VIII. DIAGNOZA PSYCHIATRYCZNA")

    for diag_key, diag_label in [("diagnoza_wstepna", "Diagnoza Wstępna"),
                                   ("diagnoza_dodatkowa", "Diagnoza Dodatkowa (współistniejąca)")]:
        dg = raport.get(diag_key, {})
        if isinstance(dg, dict):
            podnaglowek(diag_label)
            p_dg = doc.add_paragraph()
            p_dg.paragraph_format.space_after = Pt(2)
            nazwa_lac = dg.get("nazwa_lacinska", BRAK)
            r_dg1 = p_dg.add_run(nazwa_lac)
            if nazwa_lac == BRAK:
                _font(r_dg1, bold=True, color=RED, size=11)
            else:
                _font(r_dg1, bold=True, size=11, color=DKRED)
            if dg.get("nazwa_polska") and dg["nazwa_polska"] != BRAK:
                r_dg2 = p_dg.add_run(f"  /  pol.: {dg['nazwa_polska']}")
                _font(r_dg2, size=10, italic=True)
            if dg.get("kod_dsm"):
                pole("Kod DSM", dg["kod_dsm"], size=9)
            if dg.get("opis_kliniczny"):
                maszyna(dg["opis_kliniczny"], size=9, space_before=4, space_after=4)

    objawy = raport.get("objawy", [])
    if objawy:
        doc.add_paragraph()
        podnaglowek("Objawy kliniczne")
        for i, obj in enumerate(objawy, 1):
            punkt_listy(str(obj), numer=i, size=9)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # IX. ZALECENIA TERAPEUTYCZNE
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("IX. ZALECENIA TERAPEUTYCZNE")
    zt = raport.get("zalecenia_tylera", {})
    if isinstance(zt, dict):
        if zt.get("naglowek"):
            p_zth = doc.add_paragraph()
            p_zth.paragraph_format.space_after = Pt(6)
            r_zth = p_zth.add_run(str(zt["naglowek"]).upper())
            _font(r_zth, bold=True, size=10, color=DKRED)

        for key in ["zadanie_1", "zadanie_2", "zadanie_3"]:
            if zt.get(key):
                doc.add_paragraph()
                maszyna(str(zt[key]), size=10, space_before=4, space_after=4)

        if zt.get("podpis") and zt["podpis"] != BRAK:
            doc.add_paragraph()
            maszyna("Podpisano:", bold=True, size=9)
            podpis_odrecznie(str(zt["podpis"]), size=16)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # X. ROKOWANIE
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("X. ROKOWANIE")
    rok = raport.get("rokowanie", BRAK)
    maszyna(rok, size=10, color=DKRED if rok != BRAK else RED, space_after=4)
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # X-bis. LECZENIE SPECJALNE
    # ══════════════════════════════════════════════════════════════════════════
    leczenie = raport.get("leczenie_specjalne", {})
    zasady_spec = []
    if isinstance(leczenie, dict) and leczenie.get("zasady"):
        zasady_spec = leczenie["zasady"]
    elif isinstance(leczenie, list):
        zasady_spec = leczenie

    if zasady_spec:
        naglowek("X-BIS. LECZENIE SPECJALNE — METODY TERAPEUTYCZNE WG DR. T. DURDENA")

        if isinstance(leczenie, dict):
            wstep_txt = leczenie.get("wstep", "")
            if wstep_txt and wstep_txt != BRAK:
                p_intro = doc.add_paragraph()
                r_intro = p_intro.add_run(str(wstep_txt))
                _font(r_intro, italic=True, size=9, color=GREY)

        for idx, zasada in enumerate(zasady_spec, 1):
            doc.add_paragraph()
            p_z_h = doc.add_paragraph()
            p_z_h.paragraph_format.space_before = Pt(6)
            r_z_h = p_z_h.add_run(f"METODA TERAPEUTYCZNA NR {idx}:")
            _font(r_z_h, bold=True, size=10, color=DKRED)

            if isinstance(zasada, dict):
                zasada_txt    = zasada.get("zasada_tylera", BRAK)
                metoda_txt    = zasada.get("metoda_terapeutyczna", BRAK)
                dawkowanie_txt = zasada.get("dawkowanie", BRAK)
                podpis_txt    = zasada.get("podpis_komisji", "")
            else:
                zasada_txt = str(zasada)
                metoda_txt = dawkowanie_txt = podpis_txt = ""

            maszyna(zasada_txt, size=9, space_before=2, space_after=2)

            for field_label, field_val in [
                ("Metoda terapeutyczna", metoda_txt),
                ("Dawkowanie", dawkowanie_txt),
            ]:
                if field_val:
                    p_f = doc.add_paragraph()
                    p_f.paragraph_format.left_indent = Cm(0.5)
                    p_f.paragraph_format.space_after = Pt(2)
                    r_fl = p_f.add_run(f"{field_label}: ")
                    _font(r_fl, bold=True, size=9)
                    r_fv = p_f.add_run(str(field_val))
                    if field_val == BRAK:
                        _font(r_fv, bold=True, color=RED, size=9)
                    else:
                        _font(r_fv, italic=True, size=9, color=GREY)

            if podpis_txt and podpis_txt != BRAK:
                p_pd = doc.add_paragraph()
                p_pd.paragraph_format.left_indent = Cm(0.5)
                r_pd = p_pd.add_run(str(podpis_txt))
                _font(r_pd, italic=True, size=8, color=DKRED)

        if isinstance(leczenie, dict) and leczenie.get("zamkniecie"):
            p_zam = doc.add_paragraph()
            r_zam = p_zam.add_run(str(leczenie["zamkniecie"]))
            _font(r_zam, italic=True, bold=True, size=10, color=DKRED)
        separator()

    # ══════════════════════════════════════════════════════════════════════════
    # XI. INCYDENTY SPECJALNE
    # ══════════════════════════════════════════════════════════════════════════
    incydenty = raport.get("incydenty_specjalne", [])
    if incydenty:
        naglowek("XI. INCYDENTY SPECJALNE (protokoly wewnetrzne)")
        for i, inc in enumerate(incydenty, 1):
            doc.add_paragraph()
            p_inc_h = doc.add_paragraph()
            r_inc_h = p_inc_h.add_run(f"PROTOKOL INCYDENTU NR {i}:")
            _font(r_inc_h, bold=True, size=9, color=DKRED)
            maszyna(str(inc), size=9, space_before=0, space_after=4)
        separator()

    # ══════════════════════════════════════════════════════════════════════════
    # XII. PODPIS I NOTATKI PERSONELU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("XII. PODPIS I NOTATKI PERSONELU")
    pole("Lekarz prowadzący", szpital.get("lekarz", "Dr. T. Durden, MD, PhD, FIGHT"))
    doc.add_paragraph()
    podpis_odrecznie("Tyler Durden", size=18)
    doc.add_paragraph()

    notatki_piel = raport.get("notatki_pielegniarek", [])
    if notatki_piel:
        podnaglowek("Notatki pielęgniarek dyżurnych")
        for nota in notatki_piel:
            if isinstance(nota, dict):
                imie_p  = nota.get("imie_pielegniarki", "Pielęgniarka")
                data_p  = nota.get("data", "")
                tresc_p = nota.get("tresc", BRAK)
                p_np = doc.add_paragraph()
                p_np.paragraph_format.space_before = Pt(4)
                r_np = p_np.add_run(
                    f"{imie_p}" + (f"  /  {data_p}" if data_p else "") + ":"
                )
                _font(r_np, bold=True, size=9, color=FADED)
                if tresc_p:
                    nota_kursywa(tresc_p, size=9)
            elif isinstance(nota, str):
                nota_kursywa(nota, size=9)
        doc.add_paragraph()

    notatki_sprz = raport.get("notatki_sprzataczki", [])
    if notatki_sprz:
        podnaglowek("Notatki sprzątaczki (dołączone z urzędu)")
        for nota in notatki_sprz:
            if isinstance(nota, dict):
                data_s  = nota.get("data", "")
                tresc_s = nota.get("tresc", BRAK)
                if data_s:
                    p_ns = doc.add_paragraph()
                    r_ns = p_ns.add_run(f"{data_s}:")
                    _font(r_ns, bold=True, size=8, color=FADED)
                if tresc_s:
                    nota_kursywa(tresc_s, size=8)
            elif isinstance(nota, str):
                nota_kursywa(nota, size=8)
        doc.add_paragraph()

    maszyna("Kontrasygnata administracyjna:", bold=True, size=9)
    podpis_odrecznie("Marla Singer", size=15)

    # ══════════════════════════════════════════════════════════════════════════
    # ZAPIS
    # ══════════════════════════════════════════════════════════════════════════
    buf = io.BytesIO()
    doc.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    rozmiar_kb = len(buf.getvalue()) // 1024
    braki = _zbierz_braki(raport)
    log.docx_info(rozmiar_kb, braki)

    logger.info("[psych-docx] DOCX OK (%dKB), braki: %d", rozmiar_kb, len(braki))
    return b64


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA PUBLICZNA
# ─────────────────────────────────────────────────────────────────────────────

def build_raport(body: str, previous_body: str | None, res_text: str,
                 nouns_dict: dict, sender_name: str = "",
                 gender: str = "patient") -> dict:
    """
    Główna funkcja modułu.

    Zwraca:
      {raport_pdf, psych_photo_1, psych_photo_2, log_psych}
    """
    from flask import current_app as flask_app

    logger.info("[psych-raport] START build_raport sender=%s", sender_name)
    app_obj = flask_app._get_current_object()
    cfg = _load_cfg()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Inicjuj log ───────────────────────────────────────────────────────────
    log = PsychLog(sender_name, body)
    log.nouns_before(nouns_dict or {})

    # ── Filtr rzeczowników ────────────────────────────────────────────────────
    nouns_dict = _filtruj_rzeczowniki_fizyczne(cfg, body, nouns_dict or {}, log)
    log.nouns_after(nouns_dict)
    logger.info("[psych-raport] Fizyczne rzeczowniki: %d", len(nouns_dict))

    # ── Skierowanie (sync) ────────────────────────────────────────────────────
    sekcja_skier = {}
    try:
        sekcja_skier = _sekcja_skierowanie(cfg, body, sender_name, nouns_dict, log)
    except Exception as e:
        logger.error("[psych-raport] Skierowanie błąd: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 1 — równolegle
    # ══════════════════════════════════════════════════════════════════════════
    def _r1_pacjent():
        with app_obj.app_context():
            return _sekcja_pacjent(cfg, body, sender_name, nouns_dict, log)

    def _r1_depozyt():
        with app_obj.app_context():
            return _sekcja_depozyt_leki(cfg, body, nouns_dict, sender_name, log)

    def _r1_diagnozy():
        with app_obj.app_context():
            return _sekcja_diagnozy(cfg, body, previous_body or "", sender_name, nouns_dict, log)

    def _r1_flux():
        with app_obj.app_context():
            return _sekcja_flux_prompty(cfg, body, nouns_dict, sender_name, gender, log)

    sekcja_pacjent = sekcja_dep_leki = sekcja_diagnozy = sekcja_flux = {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            "pacjent":  ex.submit(_r1_pacjent),
            "depozyt":  ex.submit(_r1_depozyt),
            "diagnozy": ex.submit(_r1_diagnozy),
            "flux":     ex.submit(_r1_flux),
        }
        for name, fut in futures.items():
            try:
                result = fut.result(timeout=90)
                if name == "pacjent":
                    sekcja_pacjent  = result
                elif name == "depozyt":
                    sekcja_dep_leki = result
                elif name == "diagnozy":
                    sekcja_diagnozy = result
                elif name == "flux":
                    sekcja_flux     = result
                logger.info("[psych-raport] Runda1 %s OK", name)
            except Exception as e:
                logger.error("[psych-raport] Runda1 %s błąd: %s", name, e)

    data_przyjecia = sekcja_pacjent.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    leki_lista     = sekcja_dep_leki.get("farmakologia", {}).get("leki", [])

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 2 — równolegle
    # ══════════════════════════════════════════════════════════════════════════
    def _r2_tydzien1():
        with app_obj.app_context():
            return _sekcja_tydzien(cfg, body, leki_lista, 1, data_przyjecia,
                                   sender_name, nouns_dict, log)

    def _r2_tydzien2():
        with app_obj.app_context():
            return _sekcja_tydzien(cfg, body, leki_lista, 2, data_przyjecia,
                                   sender_name, nouns_dict, log)

    def _r2_wypis():
        with app_obj.app_context():
            return _sekcja_wypis(cfg, body, data_przyjecia, sender_name, nouns_dict, log)

    dni_1_7 = dni_8_14 = []
    sekcja_wypis = {}

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures2 = {
            "tydzien1": ex.submit(_r2_tydzien1),
            "tydzien2": ex.submit(_r2_tydzien2),
            "wypis":    ex.submit(_r2_wypis),
        }
        for name, fut in futures2.items():
            try:
                result = fut.result(timeout=90)
                if name == "tydzien1":
                    dni_1_7      = result
                elif name == "tydzien2":
                    dni_8_14     = result
                elif name == "wypis":
                    sekcja_wypis = result
                logger.info("[psych-raport] Runda2 %s OK", name)
            except Exception as e:
                logger.error("[psych-raport] Runda2 %s błąd: %s", name, e)

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 3 — zalecenia
    # ══════════════════════════════════════════════════════════════════════════
    sekcja_zalecenia = {}
    try:
        sekcja_zalecenia = _sekcja_zalecenia(
            cfg, body, dni_1_7, dni_8_14, sender_name, nouns_dict, log
        )
    except Exception as e:
        logger.error("[psych-raport] Runda3 zalecenia błąd: %s", e)

    # ── Leczenie specjalne (czeka na zalecenia) ───────────────────────────────
    sekcja_leczenie = {}
    try:
        sekcja_leczenie = _sekcja_leczenie_specjalne(
            cfg, body, res_text, sender_name, nouns_dict, log
        )
    except Exception as e:
        logger.error("[psych-raport] Leczenie specjalne błąd: %s", e)

    # ── Scal raport ───────────────────────────────────────────────────────────
    raport = {}
    raport.update(sekcja_pacjent)
    raport["skierowanie"]               = sekcja_skier.get("skierowanie", {})
    raport["depozyt"]                   = sekcja_dep_leki.get("depozyt", {})
    raport["farmakologia"]              = sekcja_dep_leki.get("farmakologia", {})
    raport["hospitalizacja_tydzien_1"]  = dni_1_7
    raport["hospitalizacja_tydzien_2"]  = dni_8_14
    raport.update(sekcja_wypis)
    raport.update(sekcja_diagnozy)
    raport.update(sekcja_zalecenia)
    raport["leczenie_specjalne"]        = sekcja_leczenie.get("leczenie_specjalne", {})

    logger.info("[psych-raport] Scalono %d kluczy przed DeepSeek", len(raport))

    # ── DeepSeek tone + completeness (zawsze) ────────────────────────────────
    raport = _deepseek_tone_check(cfg, raport, log)
    raport = _deepseek_completeness_check(cfg, raport, body, log)

    # Końcowe zamienienie wszystkich markerów wewnętrznych na [BRAK DANYCH]
    raport = _zamien_braki(raport)

    # ── FLUX ──────────────────────────────────────────────────────────────────
    prompt_pacjent    = sekcja_flux.get("prompt_pacjent", "")
    prompt_przedmioty = sekcja_flux.get("prompt_przedmioty", "")
    photo_1, photo_2  = _generate_photos_parallel(prompt_pacjent, prompt_przedmioty, log)

    # ── DOCX ──────────────────────────────────────────────────────────────────
    photo_1_b64 = photo_1["base64"] if photo_1 else None
    photo_2_b64 = photo_2["base64"] if photo_2 else None
    docx_b64    = _build_docx(raport, photo_1_b64, photo_2_b64, cfg, log)

    # ── Log jako załącznik ────────────────────────────────────────────────────
    log_dict = log.build()

    if not docx_b64:
        logger.error("[psych-raport] DOCX nie wygenerowany")
        return {
            "raport_pdf":    None,
            "psych_photo_1": photo_1,
            "psych_photo_2": photo_2,
            "log_psych":     log_dict,
        }

    imie = raport.get("dane_pacjenta", {}).get("imie_nazwisko", "pacjent")
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', str(imie))[:30]

    raport_pdf_dict = {
        "base64":       docx_b64,
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "filename":     f"raport_psychiatryczny_{safe}_{ts}.docx",
    }

    logger.info(
        "[psych-raport] DONE raport=%s photo1=%s photo2=%s log=%s",
        raport_pdf_dict["filename"],
        photo_1["filename"] if photo_1 else "brak",
        photo_2["filename"] if photo_2 else "brak",
        log_dict["filename"],
    )

    return {
        "raport_pdf":    raport_pdf_dict,
        "psych_photo_1": photo_1,
        "psych_photo_2": photo_2,
        "log_psych":     log_dict,
    }
