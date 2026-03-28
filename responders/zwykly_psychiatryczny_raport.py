"""
responders/zwykly_psychiatryczny_raport.py

Moduł obsługujący CAŁY pipeline raportu psychiatrycznego:
  - 8 wywołań Groq (każda sekcja osobno, rotacja tokenów, fallback per sekcja)
  - 2 wywołania DeepSeek (tone check + completeness check, merge JSON)
  - 2 zdjęcia FLUX równolegle (ThreadPoolExecutor):
      psych_photo_1 = pacjent w kaftanie otoczony przedmiotami
      psych_photo_2 = same przedmioty jako dowody rzeczowe
  - Budowanie DOCX (python-docx)
  - Zwraca dict: {raport_pdf, psych_photo_1, psych_photo_2}

Import w zwykly.py:
    from responders.zwykly_psychiatryczny_raport import build_raport
    raport_result = build_raport(body, previous_body, res_text, nouns_dict, sender_name)

ZMIANY v2:
  - _sekcja_tydzien: głębsze szukanie listy w odpowiedzi Groq (zagnieżdżone klucze)
  - _sekcja_tydzien: fallback zróżnicowany per dzień (nie ten sam tekst x14)
  - _sekcja_tydzien: max_tokens podniesione do 4096
  - _sekcja_tydzien: logowanie surowej odpowiedzi Groq gdy parse_json zwróci None
  - _sekcja_depozyt_leki: fallback generuje leki z nouns_dict zamiast jednej Nihilizyny
  - _sekcja_pacjent: lepszy fallback z data_przyjecia = dziś
  - _parse_json_safe: bardziej agresywne wyciąganie JSON z odpowiedzi otoczonych tekstem
  - _deepseek_tone_check / _deepseek_completeness_check: zabezpieczenie przed bardzo
    dużym JSON-em (kompresja + limit)
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
from flask import current_app

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
# ŚCIEŻKI
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
RAPORT_JSON = os.path.join(PROMPTS_DIR, "zwykly_raport.json")

# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK ZDARZEŃ — zróżnicowane per dzień (nie kopiuj tego samego x14)
# ─────────────────────────────────────────────────────────────────────────────
_FALLBACK_ZDARZENIA = [
    "Przyjęcie pacjenta. Opór przy odebraniu przedmiotów osobistych. Pacjent twierdził że bez swoich rzeczy 'nie jest sobą' — co tylko potwierdziło diagnozę.",
    "Pierwsza noc na oddziale. Pacjent wielokrotnie wstawał. Pytał o telefon. Odmówiono.",
    "Sesja diagnostyczna — wywiad wstępny. Pacjent odpowiadał monosylabami lub milczał.",
    "Podanie leków o 8:00 i 20:00. Pacjent odmówił przy pierwszym podaniu, po 20 min. zgodził się.",
    "Obchód lekarski. Stan bez zmian. Pacjent pytał kiedy wyjdzie. Nie udzielono odpowiedzi.",
    "Terapia grupowa — pacjent siedział z boku, nie uczestniczył aktywnie.",
    "Noc spokojna. Brak incydentów. Pacjent spał do 6:30.",
    "Sesja indywidualna z psychiatrą. Pacjent opisywał codzienną rutynę jako 'jedyną pewną rzecz'.",
    "Badania laboratoryjne. Pacjent pytał o wyniki — poinformowano że 'w normie szpitalnej'.",
    "Aktywność na oddziale — pacjent przez godzinę siedział przy oknie i obserwował parking.",
    "Wizyta pielęgniarki nocnej. Pacjent nie spał, twierdził że 'myśli za głośno'.",
    "Obchód poranny. Lekarz odnotował nieznaczną poprawę nastroju. Pacjent zaprzeczył.",
    "Sesja diagnostyczna końcowa. Testy psychologiczne. Wyniki przekazane do dokumentacji.",
    "Dzień przedwypisowy. Pacjent zapakował rzeczy o 6 rano. Czekał przy drzwiach do 14:00.",
]

_FALLBACK_LEKI = [
    "Nihilizyna 500mg prolongatum",
    "Resignum 200mg",
    "Defeatex 100mg",
    "Nihilizyna 500mg prolongatum",
    "Resignum 200mg",
    "Melanchol 150mg",
    "Nihilizyna 500mg prolongatum",
    "Defeatex 100mg",
    "Resignum 200mg",
    "Melanchol 150mg",
    "Nihilizyna 500mg prolongatum",
    "Defeatex 100mg",
    "Resignum 200mg + Nihilizyna 500mg",
    "Odstawienie leków (dzień wypisu)",
]

_FALLBACK_STANY = [
    "Stabilny w sensie klinicznym, niestabilny egzystencjalnie.",
    "Pobudzony. Trudności z adaptacją.",
    "Wyciszony. Współpraca minimalna.",
    "Bez zmian. Opór bierny przy farmakoterapii.",
    "Apatyczny. Nie reaguje na bodźce zewnętrzne powyżej progu konieczności.",
    "Lekko pobudzony wieczorem. Noc spokojna.",
    "Stabilny. Sen przerwany.",
    "Nastrój nieco lepszy — pacjent sam ocenił jako 'bez różnicy'.",
    "Bez zmian klinicznych.",
    "Płaski afekt. Kontakt zachowany.",
    "Pobudzenie nocne. Rano wyciszony.",
    "Nastrój wyrównany w normach oddziałowych.",
    "Gotowość do wypisu — klinicznie wątpliwa, administracyjnie zatwierdzona.",
    "Wypisany. Stan przy wypisie: taki jak przy przyjęciu, tylko droższy.",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — Groq rotacja tokenów
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
        current_app.logger.warning("[psych-raport] Groq HTTP %d", resp.status_code)
    except Exception as e:
        current_app.logger.warning("[psych-raport] Groq wyjątek: %s", e)
    return None


def _call_groq_with_retry(system: str, user: str, max_tokens: int = 4096,
                           section_name: str = "?", max_attempts: int = 3) -> str | None:
    """
    Wywołuje Groq z rotacją kluczy. Każdy klucz próbuje max_attempts razy.
    Zwraca odpowiedź lub None jeśli wszystkie klucze zawiodły.
    """
    keys = _get_groq_keys()
    if not keys:
        current_app.logger.error("[psych-raport] Brak kluczy Groq dla sekcji %s", section_name)
        return None

    for attempt in range(max_attempts):
        for name, key in keys:
            result = _call_groq_single(key, system, user, max_tokens)
            if result and result != "RATE_LIMIT":
                current_app.logger.info("[psych-raport] %s OK (klucz=%s attempt=%d)",
                                        section_name, name, attempt + 1)
                return result
            if result == "RATE_LIMIT":
                current_app.logger.warning("[psych-raport] %s RATE_LIMIT klucz=%s → następny",
                                           section_name, name)
                continue
        current_app.logger.warning("[psych-raport] %s — wszystkie klucze zawiodły attempt=%d/%d",
                                   section_name, attempt + 1, max_attempts)

    current_app.logger.error("[psych-raport] %s — brak odpowiedzi po %d próbach",
                             section_name, max_attempts)
    return None


def _parse_json_safe(raw: str, section: str) -> dict | list | None:
    """
    Wyciąga JSON z odpowiedzi Groq. Obsługuje:
    - czysty JSON
    - JSON owinięty w ```json...```
    - JSON poprzedzony lub zakończony tekstem
    - ucięty JSON (próba naprawy)
    """
    if not raw:
        return None
    try:
        # Krok 1: usuń markdown code fences
        clean = re.sub(r'^```[a-z]*\s*', '', raw.strip(), flags=re.M)
        clean = re.sub(r'\s*```\s*$', '', clean, flags=re.M).strip()

        # Krok 2: znajdź pierwszy { lub [ i wytnij od niego do końca
        start_idx = None
        for i, ch in enumerate(clean):
            if ch in '{[':
                start_idx = i
                break
        if start_idx is not None:
            clean = clean[start_idx:]

        # Krok 3: znajdź ostatni pasujący nawias zamykający
        # (obcinamy śmieciowy tekst po JSON-ie)
        # Szybka heurystyka: cofaj od końca aż napotkasz } lub ]
        end_idx = len(clean)
        for i in range(len(clean) - 1, -1, -1):
            if clean[i] in '}]':
                end_idx = i + 1
                break
        clean = clean[:end_idx]

        result = json.loads(clean.strip())
        current_app.logger.info("[psych-raport] JSON OK sekcja=%s", section)
        return result
    except Exception as e:
        # Próba naprawy uciętego JSONa — zamknij brakujące nawiasy
        current_app.logger.warning("[psych-raport] JSON błąd sekcja=%s: %s | próba naprawy...", section, e)
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
                    current_app.logger.warning(
                        "[psych-raport] JSON naprawiony sekcja=%s (doklejono '%s')", section, suffix)
                    return result
        except Exception as e2:
            current_app.logger.warning(
                "[psych-raport] JSON naprawa nieudana sekcja=%s: %s | raw=%.300s",
                section, e2, raw)
        return None


def _extract_list_from_result(result, min_len: int = 5) -> list | None:
    """
    Wyciąga listę z różnych możliwych struktur odpowiedzi Groq.
    Szuka listy o co najmniej min_len elementach, na kilku poziomach zagnieżdżenia.
    """
    if isinstance(result, list) and len(result) >= min_len:
        return result

    if isinstance(result, dict):
        # Poziom 1 — bezpośrednie wartości
        for v in result.values():
            if isinstance(v, list) and len(v) >= min_len:
                return v
        # Poziom 2 — zagnieżdżone dict → list
        for v in result.values():
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and len(vv) >= min_len:
                        return vv
        # Poziom 3 — jakiekolwiek listy, nawet krótsze (fallback miękki)
        for v in result.values():
            if isinstance(v, list) and len(v) > 0:
                current_app.logger.warning(
                    "[psych-raport] _extract_list: znaleziono listę %d elem. (< min %d)",
                    len(v), min_len)
                return v

    return None


def _merge_dicts(base: dict, override: dict) -> dict:
    """Głębokie scalenie — override nadpisuje wartości w base."""
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
        current_app.logger.error("[psych-raport] Brak zwykly_raport.json: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #1 — dane pacjenta + powód + cytaty
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_pacjent(cfg: dict, body: str, sender_name: str) -> dict:
    sec = cfg.get("groq_1_pacjent", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"SENDER_NAME (priorytet dla imienia): {sender_name or '(brak)'}\n\n"
        f"SCHEMAT JSON do wypełnienia:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_groq_with_retry(system, user, 2000, "pacjent")
    if not raw:
        current_app.logger.warning("[psych-raport] sekcja_pacjent → brak odpowiedzi Groq, fallback")
    result = _parse_json_safe(raw, "pacjent") if raw else None
    if not result:
        fb = cfg.get("fallback_dane_pacjenta", {})
        # Uzupełnij datę przyjęcia na dziś jeśli fallback jej nie ma
        if isinstance(fb, dict) and not fb.get("data_przyjecia"):
            fb = dict(fb)
            fb["data_przyjecia"] = datetime.now().strftime("%d.%m.%Y")
        current_app.logger.warning("[psych-raport] sekcja_pacjent → fallback")
        return fb
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #2 — depozyt + leki
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_depozyt_leki(cfg: dict, body: str, nouns_dict: dict) -> dict:
    sec = cfg.get("groq_2_depozyt_leki", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    nouns_str = ", ".join(nouns_dict.values()) if nouns_dict else "(brak rzeczowników)"
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"RZECZOWNIKI Z EMAILA (każdy musi mieć swój lek): {nouns_str}\n\n"
        f"SCHEMAT JSON do wypełnienia:\n{schema}\n\n"
        f"Pamiętaj: JEDEN LEK per rzeczownik, nazwa leku nawiązuje do rzeczownika. Zwróć TYLKO czysty JSON."
    )
    raw = _call_groq_with_retry(system, user, 2500, "depozyt_leki")
    if not raw:
        current_app.logger.warning("[psych-raport] sekcja_depozyt_leki → brak odpowiedzi Groq")
    result = _parse_json_safe(raw, "depozyt_leki") if raw else None
    if not result:
        current_app.logger.warning("[psych-raport] sekcja_depozyt_leki → fallback (generowany z nouns_dict)")
        # Generuj leki z rzeczowników — nie jeden fallbackowy lek
        przedmioty = list(nouns_dict.values()) if nouns_dict else ["przedmiot nieznany"]
        leki = []
        for rzecz in przedmioty[:8]:
            leki.append({
                "nazwa": f"{rzecz.capitalize()}azyna {random.randint(50, 500)}mg",
                "rzeczownik_zrodlowy": rzecz,
                "wskazanie": f"patologiczne przywiązanie do obiektu '{rzecz}'",
                "dawkowanie": f"1x dziennie, do odwołania"
            })
        return {
            "depozyt": {
                "lista_przedmiotow": przedmioty,
                "protokol_depozytu": "Odebrano przedmioty niebezpieczne. Pacjent protestował."
            },
            "farmakologia": {
                "leki": leki,
                "nota_farmaceutyczna": "Farmakoterapia wdrożona. Rokowanie: złe."
            }
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #3/#4 — tydzień 1 (dni 1-7) i tydzień 2 (dni 8-14)
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_tydzien(cfg: dict, body: str, leki: list, tydzien: int,
                    data_przyjecia: str) -> list:
    """
    tydzien=1 → dni 1-7, tydzien=2 → dni 8-14

    FIX v2:
    - max_tokens podniesione do 4096
    - szukanie listy na wielu poziomach zagnieżdżenia (_extract_list_from_result)
    - logowanie surowej odpowiedzi gdy parse zwróci None
    - fallback zróżnicowany per dzień (nie ten sam tekst x7)
    """
    sec_key = f"groq_{2 + tydzien}_tydzien{tydzien}"
    sec = cfg.get(sec_key, {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)

    # Oblicz daty
    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
    except Exception:
        base_date = datetime.now()
    start_day = (tydzien - 1) * 7 + 1
    daty = [(base_date + timedelta(days=start_day - 1 + i)).strftime("%d.%m.%Y")
            for i in range(7)]
    daty_str = "\n".join(f"Dzień {start_day + i}: {d}" for i, d in enumerate(daty))

    leki_str = json.dumps(leki, ensure_ascii=False, indent=2) if leki else "[]"

    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"LISTA LEKÓW DO UŻYCIA (używaj tych leków w opisach dni):\n{leki_str}\n\n"
        f"DATY HOSPITALIZACJI — użyj DOKŁADNIE tych dat:\n{daty_str}\n\n"
        f"SCHEMAT JSON (wygeneruj tablicę 7 obiektów dla dni {start_day}-{start_day+6}):\n{schema}\n\n"
        f"KRYTYCZNE: Każdy z 7 dni MUSI mieć inne, unikalne 'zdarzenie' nawiązujące do treści emaila pacjenta. "
        f"NIE KOPIUJ tego samego zdarzenia do różnych dni. "
        f"Zwróć TYLKO czysty JSON z tablicą 7 obiektów."
    )
    section_name = f"tydzien{tydzien}"

    # Podnosimy max_tokens do 4096 — 7 obiektów z bogatymi opisami wymaga więcej miejsca
    raw = _call_groq_with_retry(system, user, 4096, section_name)

    if not raw:
        current_app.logger.error("[psych-raport] %s → Groq zwrócił None", section_name)
        return _fallback_dni(start_day, daty, leki)

    result = _parse_json_safe(raw, section_name)

    if result is None:
        current_app.logger.error(
            "[psych-raport] %s → parse_json_safe zwrócił None | raw_preview=%.500s",
            section_name, raw
        )
        return _fallback_dni(start_day, daty, leki)

    # Wyciągnij listę z dowolnej głębokości struktury
    lista = _extract_list_from_result(result, min_len=5)
    if lista is not None:
        current_app.logger.info("[psych-raport] %s → lista %d elementów OK", section_name, len(lista))
        return lista

    current_app.logger.warning(
        "[psych-raport] %s → nie znaleziono listy w result (type=%s) | raw_preview=%.300s",
        section_name, type(result).__name__, raw
    )
    return _fallback_dni(start_day, daty, leki)


def _fallback_dni(start_day: int, daty: list, leki: list) -> list:
    """
    Zróżnicowany fallback dla 7 dni — każdy dzień ma inny opis.
    Korzysta ze stałych _FALLBACK_ZDARZENIA / _FALLBACK_LEKI / _FALLBACK_STANY.
    """
    wynik = []
    for i in range(7):
        idx = (start_day - 1 + i) % len(_FALLBACK_ZDARZENIA)
        # Jeśli mamy leki z Groq — używaj ich rotacyjnie
        if leki and isinstance(leki, list) and len(leki) > 0:
            lek_obj = leki[i % len(leki)]
            lek_str = lek_obj.get("nazwa", _FALLBACK_LEKI[idx]) if isinstance(lek_obj, dict) else str(lek_obj)
        else:
            lek_str = _FALLBACK_LEKI[idx]
        wynik.append({
            "dzien": start_day + i,
            "data": daty[i],
            "zdarzenie": _FALLBACK_ZDARZENIA[idx],
            "lek": lek_str,
            "stan_pacjenta": _FALLBACK_STANY[idx],
            "nota_lekarska": "",
        })
    return wynik


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #5 — wypis (dzień 15)
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_wypis(cfg: dict, body: str, data_przyjecia: str) -> dict:
    sec = cfg.get("groq_5_wypis", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
        data_wypisu = (base_date + timedelta(days=14)).strftime("%d.%m.%Y")
    except Exception:
        data_wypisu = datetime.now().strftime("%d.%m.%Y")
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"DATA WYPISU (dzień 15): {data_wypisu}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_groq_with_retry(system, user, 1500, "wypis")
    result = _parse_json_safe(raw, "wypis") if raw else None
    if not result:
        if raw:
            current_app.logger.error("[psych-raport] wypis → parse None | raw=%.300s", raw)
        return {"wypis": {
            "dzien_wypisu": f"Dzień 15, {data_wypisu}",
            "stan_przy_wypisie": "Pacjent osiągnął akceptowalny poziom beznadziei.",
            "powod_wypisu": "Wyczerpanie budżetu nadziei.",
            "zalecenia_po_wypisie": ["Unikać optymizmu.", "Nie planować remontów.", "Nie pisać emaili."],
            "opis_pozegnania": "Pacjent wyszedł bez słowa. Drzwi zostawił otwarte."
        }}
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #6 — łacińskie diagnozy
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_diagnozy(cfg: dict, body: str, previous_body: str) -> dict:
    sec = cfg.get("groq_6_diagnozy_lacina", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        + (f"POPRZEDNI EMAIL (dla diagnozy_dodatkowej):\n{previous_body[:1000]}\n\n"
           if previous_body else "")
        + f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_groq_with_retry(system, user, 1500, "diagnozy")
    result = _parse_json_safe(raw, "diagnozy") if raw else None
    if not result:
        if raw:
            current_app.logger.error("[psych-raport] diagnozy → parse None | raw=%.300s", raw)
        return {
            "diagnoza_wstepna": {
                "nazwa_lacinska": "Syndroma Emaili Desperati",
                "nazwa_polska": "Desperackie Emailowanie",
                "kod_dsm": "DSM-TD-2026-001",
                "opis_kliniczny": "Przewlekłe wysyłanie emaili z objawami nadziei."
            },
            "diagnoza_dodatkowa": {
                "nazwa_lacinska": "Morbus Optimismus Pathologicus",
                "nazwa_polska": "Patologiczny Optymizm",
                "kod_dsm": "DSM-TD-2026-002",
                "opis_kliniczny": "Choroba współistniejąca. Brak rokowań."
            },
            "objawy": ["Nadmierny optymizm epistolarny",
                       "Fiksacja na punkcie nierealnych planów",
                       "Urojenia poprawy sytuacji"]
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #7 — zalecenia + notatki + rokowanie
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_zalecenia(cfg: dict, body: str, dni_1_7: list, dni_8_14: list) -> dict:
    sec = cfg.get("groq_7_zalecenia_notatki", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)

    # Wyciągnij kilka kluczowych zdarzeń z hospitalizacji jako kontekst
    zdarzenia = []
    for d in (dni_1_7 or []) + (dni_8_14 or []):
        if isinstance(d, dict) and d.get("zdarzenie"):
            zdarzenia.append(f"Dzień {d.get('dzien', '?')}: {str(d['zdarzenie'])[:150]}")
    zdarzenia_str = "\n".join(zdarzenia[:8]) if zdarzenia else "(brak)"

    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = _call_groq_with_retry(system, user, 2000, "zalecenia")
    result = _parse_json_safe(raw, "zalecenia") if raw else None
    if not result:
        if raw:
            current_app.logger.error("[psych-raport] zalecenia → parse None | raw=%.300s", raw)
        return {
            "zalecenia_tylera": {
                "naglowek": "RACHUNEK ZA WYZWOLENIE",
                "zadanie_1": "ZNISZCZENIE: Zniszczyć pierwszy napotkany przedmiot nadziei.",
                "zadanie_2": "UPOKORZENIE: Wyrzeknij się planów publicznie.",
                "zadanie_3": "DESTRUKCJA: Spalić notatki z planami na przyszłość.",
                "podpis": "Dr. Tyler Durden, Ordynator Oddziału Beznadziei"
            },
            "rokowanie": "Bez szans. Ale przynajmniej uczciwe.",
            "notatka_pielegniarki": "Pacjent był... pacjentem. Siostra Kazimiera.",
            "notatka_sprzataczki": "Pod łóżkiem znalazłam nadzieję. Wyrzuciłam.",
            "incydenty_specjalne": ["Incydent z oknem — zamknięte.", "Incydent z lekarstwami — połknięte."]
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GROQ #8 — prompty FLUX
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_flux_prompty(cfg: dict, body: str, nouns_dict: dict,
                          sender_name: str, gender: str) -> dict:
    sec = cfg.get("groq_8_flux_prompty", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    nouns_str = ", ".join(nouns_dict.values()) if nouns_dict else "everyday objects"
    user = (
        f"EMAIL PACJENTA:\n{body[:800]}\n\n"
        f"PRZEDMIOTY Z EMAILA (skonfiskowane — muszą być w obu promptach): {nouns_str}\n"
        f"PŁEĆ PACJENTA: {gender}\n"
        f"IMIĘ PACJENTA: {sender_name or 'unknown'}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON z dwoma promptami FLUX po angielsku."
    )
    raw = _call_groq_with_retry(system, user, 600, "flux_prompty")
    result = _parse_json_safe(raw, "flux_prompty") if raw else None
    if not result or not isinstance(result, dict):
        # hardkodowany fallback
        objects_critical = f"CRITICAL OBJECTS: {nouns_str}."
        return {
            "prompt_pacjent": (
                f"{objects_critical} Top-down documentary photo of a round wooden psychiatric "
                f"examination table. Five Polaroid photos scattered across the table, each showing "
                f"a {gender} in a white canvas straitjacket surrounded by: {nouns_str}. "
                f"Faded desaturated colors, 35mm grain, 1990s documentary style, "
                f"fluorescent light, institutional green walls."
            ),
            "prompt_przedmioty": (
                f"{objects_critical} Top-down clinical evidence photograph. Metal hospital tray "
                f"with the following objects laid out as evidence: {nouns_str}. Each object has "
                f"a small numbered evidence tag. Cold harsh fluorescent lighting, "
                f"1990s police evidence room aesthetic, hyper-realistic, sterile white background."
            )
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #1 — tone check
# ─────────────────────────────────────────────────────────────────────────────

def _deepseek_tone_check(cfg: dict, raport: dict) -> dict:
    sec = cfg.get("deepseek_1_tone_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))
    # Kompaktowy JSON (bez indent) — mniej tokenów, mniejsze ryzyko ucięcia
    raport_json = json.dumps(raport, ensure_ascii=False, separators=(',', ':'))
    # Zabezpieczenie przed zbyt długim payloadem — skróć jeśli potrzeba
    max_raport_chars = 12000
    if len(raport_json) > max_raport_chars:
        current_app.logger.warning(
            "[psych-raport] DeepSeek tone: raport_json zbyt duży (%d znaków), skracam do %d",
            len(raport_json), max_raport_chars)
        raport_json = raport_json[:max_raport_chars] + "...}"
    user = (
        f"RAPORT DO OCENY I POPRAWY:\n{raport_json}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Zwróć TYLKO czysty JSON z poprawkami (ta sama struktura kluczy)."
    )
    current_app.logger.info("[psych-raport] DeepSeek tone check START (~%d znaków)", len(user))
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        current_app.logger.warning("[psych-raport] DeepSeek tone check → brak odpowiedzi, skip")
        return raport
    result = _parse_json_safe(raw, "deepseek_tone")
    if not result or not isinstance(result, dict):
        current_app.logger.warning("[psych-raport] DeepSeek tone check → parse None, skip")
        return raport
    merged = _merge_dicts(raport, result)
    current_app.logger.info("[psych-raport] DeepSeek tone check OK — merge zastosowany")
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #2 — completeness check
# ─────────────────────────────────────────────────────────────────────────────

def _deepseek_completeness_check(cfg: dict, raport: dict, body: str) -> dict:
    sec = cfg.get("deepseek_2_completeness_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))
    # Kompaktowy JSON (bez indent) — mniej tokenów, mniejsze ryzyko ucięcia
    raport_json = json.dumps(raport, ensure_ascii=False, separators=(',', ':'))
    # Zabezpieczenie przed zbyt długim payloadem
    max_raport_chars = 12000
    if len(raport_json) > max_raport_chars:
        current_app.logger.warning(
            "[psych-raport] DeepSeek completeness: raport_json zbyt duży (%d znaków), skracam do %d",
            len(raport_json), max_raport_chars)
        raport_json = raport_json[:max_raport_chars] + "...}"
    user = (
        f"ORYGINALNY EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"RAPORT DO SPRAWDZENIA:\n{raport_json}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Zwróć TYLKO czysty JSON z uzupełnionymi nawiązaniami do emaila."
    )
    current_app.logger.info("[psych-raport] DeepSeek completeness check START (~%d znaków)", len(user))
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        current_app.logger.warning("[psych-raport] DeepSeek completeness → brak odpowiedzi, skip")
        return raport
    result = _parse_json_safe(raw, "deepseek_completeness")
    if not result or not isinstance(result, dict):
        current_app.logger.warning("[psych-raport] DeepSeek completeness → parse None, skip")
        return raport
    merged = _merge_dicts(raport, result)
    current_app.logger.info("[psych-raport] DeepSeek completeness OK — merge zastosowany")
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# FLUX — generowanie zdjęć
# ─────────────────────────────────────────────────────────────────────────────

def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(40)]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


def _generate_flux(prompt: str, label: str,
                   steps: int = 28, guidance: float = 7.0,
                   width: int = 1024, height: int = 1024) -> str | None:
    """Generuje obrazek FLUX. Zwraca base64 JPG lub None."""
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[psych-flux] Brak tokenów HF dla %s", label)
        return None

    seed    = random.randint(0, 2 ** 32 - 1)
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": steps,
            "guidance_scale":      guidance,
            "width":               width,
            "height":              height,
            "seed":                seed,
        }
    }
    current_app.logger.info("[psych-flux] %s — prompt %.120s...", label, prompt)

    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(HF_API_URL, headers=headers,
                                 json=payload, timeout=HF_TIMEOUT)
            if resp.status_code == 200:
                current_app.logger.info("[psych-flux] %s OK token=%s (%dB)",
                                        label, name, len(resp.content))
                # PNG → JPG
                try:
                    from PIL import Image as PILImage
                    pil = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
                    buf = io.BytesIO()
                    pil.save(buf, format="JPEG", quality=92, optimize=True)
                    return base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception as e:
                    current_app.logger.warning("[psych-flux] PNG→JPG błąd: %s", e)
                    return base64.b64encode(resp.content).decode("ascii")
            elif resp.status_code == 429:
                current_app.logger.warning("[psych-flux] %s 429 token=%s → następny", label, name)
            else:
                current_app.logger.warning("[psych-flux] %s HTTP %d token=%s",
                                           label, resp.status_code, name)
        except Exception as e:
            current_app.logger.warning("[psych-flux] %s wyjątek token=%s: %s", label, name, e)

    current_app.logger.error("[psych-flux] %s — wszystkie tokeny zawiodły", label)
    return None


def _generate_photos_parallel(prompt_pacjent: str, prompt_przedmioty: str) -> tuple:
    """
    Generuje oba zdjęcia równolegle. Zwraca (photo_pacjent, photo_przedmioty).
    Jeśli HF_TOKEN wyczerpany → zwraca None dla danego zdjęcia, nie blokuje całości.
    """
    from concurrent.futures import ThreadPoolExecutor
    from flask import current_app as flask_app

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    app_obj = flask_app._get_current_object()

    def gen_pacjent():
        with app_obj.app_context():
            return _generate_flux(prompt_pacjent, "photo_pacjent", steps=28, guidance=7)

    def gen_przedmioty():
        with app_obj.app_context():
            return _generate_flux(prompt_przedmioty, "photo_przedmioty", steps=28, guidance=7)

    b64_pacjent    = None
    b64_przedmioty = None

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(gen_pacjent)
            f2 = ex.submit(gen_przedmioty)
            try:
                b64_pacjent    = f1.result(timeout=120)
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
            app_obj.logger.error("[psych-flux] Błąd ThreadPoolExecutor: %s", e)

    def _wrap(b64, suffix):
        if not b64:
            return None
        return {
            "base64":       b64,
            "content_type": "image/jpeg",
            "filename":     f"psych_{suffix}_{ts}.jpg",
        }

    return _wrap(b64_pacjent, "pacjent"), _wrap(b64_przedmioty, "przedmioty")


# ─────────────────────────────────────────────────────────────────────────────
# BUDOWANIE DOCX
# ─────────────────────────────────────────────────────────────────────────────

def _register_fonts_docx():
    """Zachowane dla spójności importów."""
    pass


def _build_docx(raport: dict, photo_pacjent_b64: str | None,
                photo_przedmioty_b64: str | None, cfg: dict) -> str | None:
    """
    Buduje DOCX z raportem psychiatrycznym.
    Styl: maszynopis (Courier New), bez tabel, podpis kursywą.
    Zwraca base64 DOCX lub None.
    """
    try:
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError as e:
        current_app.logger.error("[psych-docx] Brak python-docx: %s", e)
        return None

    szpital = cfg.get("szpital", {})
    doc     = Document()

    # ── Marginesy — szersze, jak stary maszynopis ──────────────────────────
    for section in doc.sections:
        section.top_margin    = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin   = Cm(3.0)
        section.right_margin  = Cm(2.5)

    # ── Kolory ────────────────────────────────────────────────────────────
    BLACK  = RGBColor(0x0A, 0x0A, 0x0A)   # prawie czarny — tusz maszyny
    DKRED  = RGBColor(0x7A, 0x0F, 0x0F)   # ciemna czerwień dla nagłówków
    GREY   = RGBColor(0x55, 0x55, 0x55)   # szary dla not i komentarzy
    LGREY  = RGBColor(0x99, 0x99, 0x99)   # jasny szary dla separatorów
    FADED  = RGBColor(0x33, 0x33, 0x33)   # wyblakły tusz

    TYPEWRITER = "Courier New"            # główna czcionka maszynopisu
    TYPEWRITER_SIZE = 10                  # 10pt — klasyczny maszynopis

    # ── Helpery ───────────────────────────────────────────────────────────

    def _set_font(run, name=TYPEWRITER, size=TYPEWRITER_SIZE,
                  bold=False, italic=False, color=BLACK):
        run.font.name    = name
        run.font.size    = Pt(size)
        run.bold         = bold
        run.italic       = italic
        run.font.color.rgb = color

    def maszyna(text, bold=False, italic=False, color=BLACK,
                size=TYPEWRITER_SIZE, align=None, space_before=0, space_after=3):
        """Akapit w stylu maszynopisu."""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after  = Pt(space_after)
        if align:
            p.alignment = align
        r = p.add_run(str(text) if text else "")
        _set_font(r, bold=bold, italic=italic, color=color, size=size)
        return p

    def naglowek(text, color=DKRED, size=11, upper=True):
        """Nagłówek sekcji — Courier New bold, caps, podkreślony wizualnie."""
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run(text.upper() if upper else text)
        _set_font(r, bold=True, color=color, size=size)
        # Separator pod nagłówkiem
        sep = doc.add_paragraph()
        sep.paragraph_format.space_before = Pt(0)
        sep.paragraph_format.space_after  = Pt(4)
        r2 = sep.add_run("=" * 68)
        _set_font(r2, color=LGREY, size=7)
        return p

    def podnaglowek(text, color=FADED, size=10):
        """Podtytuł — mniejszy, kursywa."""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run(f"--- {text} ---")
        _set_font(r, italic=True, color=color, size=size)
        return p

    def pole(label, value, size=TYPEWRITER_SIZE):
        """Pole danych: ETYKIETA: wartość."""
        if not value:
            return
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(1)
        rl = p.add_run(f"{label.upper()}: ")
        _set_font(rl, bold=True, size=size)
        rv = p.add_run(str(value))
        _set_font(rv, size=size)

    def separator_lekki():
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(2)
        r = p.add_run("- " * 34)
        _set_font(r, color=LGREY, size=7)

    def punkt_listy(text, numer=None, size=TYPEWRITER_SIZE):
        """Punkt listy bez stylu Word — czysty maszynopis."""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(2)
        p.paragraph_format.left_indent  = Cm(0.8)
        prefix = f"{numer}." if numer else "  *"
        r = p.add_run(f"{prefix}  {str(text)}")
        _set_font(r, size=size)

    def cytat_blok(text, size=9):
        """Cytat — wcięty, kursywa."""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after  = Pt(4)
        p.paragraph_format.left_indent  = Cm(1.2)
        p.paragraph_format.right_indent = Cm(0.5)
        r = p.add_run(str(text))
        _set_font(r, italic=True, color=GREY, size=size)

    def nota_kursywa(text, size=9):
        """Nieoficjalna nota — szary kursywa."""
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(6)
        p.paragraph_format.left_indent  = Cm(0.6)
        r = p.add_run(f"[Nota: {str(text)}]")
        _set_font(r, italic=True, color=GREY, size=size)

    def podpis_odrecznie(text, size=16):
        """
        Symulacja podpisu odręcznego — bold italic duże, Courier New
        (brak prawdziwej czcionki kaligraficznej w środowisku serwerowym).
        """
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        r = p.add_run(text)
        r.font.name    = "Courier New"
        r.font.size    = Pt(size)
        r.bold         = True
        r.italic       = True
        r.font.color.rgb = DKRED
        return p

    def insert_photo(b64: str, caption: str, width_cm: float = 13.0):
        if not b64:
            return
        try:
            img_bytes = base64.b64decode(b64)
            stream    = io.BytesIO(img_bytes)
            doc.add_picture(stream, width=Cm(width_cm))
            cap = doc.add_paragraph(caption)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in cap.runs:
                _set_font(r, italic=True, color=GREY, size=8)
        except Exception as e:
            current_app.logger.warning("[psych-docx] Błąd wstawiania zdjęcia: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # NAGŁÓWEK INSTYTUCJI
    # ══════════════════════════════════════════════════════════════════════════
    p_nazwa = doc.add_paragraph()
    p_nazwa.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_nazwa = p_nazwa.add_run(szpital.get("nazwa", "Szpital Psychiatryczny im. Tylera Durdena").upper())
    _set_font(r_nazwa, bold=True, size=13)

    p_adr = doc.add_paragraph()
    p_adr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_adr = p_adr.add_run(szpital.get("adres", ""))
    _set_font(r_adr, size=9, color=GREY)

    p_odd = doc.add_paragraph()
    p_odd.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_odd = p_odd.add_run(szpital.get("oddzial", ""))
    _set_font(r_odd, size=9, color=GREY)

    doc.add_paragraph()

    p_tyt = doc.add_paragraph()
    p_tyt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_tyt = p_tyt.add_run("HISTORIA CHOROBY — KARTA PRZYJECIA I HOSPITALIZACJI")
    _set_font(r_tyt, bold=True, size=11)

    nr         = raport.get("numer_historii_choroby", "NY-2026-99999")
    data_przyj = raport.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    p_nr = doc.add_paragraph()
    p_nr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_nr = p_nr.add_run(
        f"Nr: {nr}   |   Data przyjecia: {data_przyj}   |   "
        f"Lekarz prowadzacy: {szpital.get('lekarz', 'Dr. T. Durden')}"
    )
    _set_font(r_nr, size=9, color=GREY)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # I. DANE PACJENTA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("I. DANE PACJENTA")
    dp = raport.get("dane_pacjenta", {})
    pole("Imie i nazwisko",   dp.get("imie_nazwisko", ""))
    pole("Wiek",              dp.get("wiek", ""))
    pole("Adres",             dp.get("adres", ""))
    pole("Zawod",             dp.get("zawod", ""))
    pole("Stan cywilny",      dp.get("stan_cywilny", ""))
    pole("Nr ubezpieczenia",  dp.get("numer_ubezpieczenia", ""))

    if photo_pacjent_b64:
        doc.add_paragraph()
        podnaglowek("DOKUMENTACJA FOTOGRAFICZNA — PRZYJECIE")
        insert_photo(photo_pacjent_b64,
                     "Fot. 1 — Pacjent w kaftanie bezpieczenstwa. Oddzial B. Material dowodowy.")

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # II. POWÓD PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("II. POWOD PRZYJECIA")
    powod = raport.get("powod_przyjecia", "")
    if powod:
        maszyna(powod, size=10, space_after=4)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # III. CYTATY Z IZBY PRZYJĘĆ
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("III. CYTATY Z IZBY PRZYJEC")
    cytaty = raport.get("cytaty_z_przyjecia", "")
    if isinstance(cytaty, list):
        for i, c in enumerate(cytaty, 1):
            maszyna(f"[{i}]", bold=True, size=9, space_after=0)
            cytat_blok(str(c), size=9)
    elif cytaty:
        cytat_blok(str(cytaty), size=9)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # IV. PROTOKÓŁ DEPOZYTU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("IV. PROTOKOL DEPOZYTU — PRZEDMIOTY SKONFISKOWANE")
    dep = raport.get("depozyt", {})
    if isinstance(dep, dict):
        lista = dep.get("lista_przedmiotow", [])
        proto = dep.get("protokol_depozytu", "")
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
                     "Fot. 2 — Przedmioty skonfiskowane przy przyjęciu. Protokol dowodow rzeczowych.")

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # V. FARMAKOLOGIA — format akapitowy, bez tabel
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("V. FARMAKOLOGIA — PELNA LISTA LEKOW ZASTOSOWANYCH")
    farm = raport.get("farmakologia", {})
    leki_lista = farm.get("leki", []) if isinstance(farm, dict) else []

    if leki_lista:
        for i, lek in enumerate(leki_lista, 1):
            if not isinstance(lek, dict):
                continue
            doc.add_paragraph()
            # Nazwa leku jako podtytuł
            p_lek = doc.add_paragraph()
            p_lek.paragraph_format.space_before = Pt(4)
            p_lek.paragraph_format.space_after  = Pt(1)
            p_lek.paragraph_format.left_indent  = Cm(0.4)
            r_lek = p_lek.add_run(f"{i}.  {lek.get('nazwa', '???').upper()}")
            _set_font(r_lek, bold=True, size=10)

            # Przedmioty odebrane (zamiast "rzeczownik źródłowy")
            p_rz = doc.add_paragraph()
            p_rz.paragraph_format.left_indent = Cm(1.2)
            p_rz.paragraph_format.space_after = Pt(0)
            r_rz_l = p_rz.add_run("Przedmioty odebrane: ")
            _set_font(r_rz_l, bold=True, size=9)
            r_rz_v = p_rz.add_run(str(lek.get("rzeczownik_zrodlowy", "")))
            _set_font(r_rz_v, size=9)

            # Wskazanie
            p_ws = doc.add_paragraph()
            p_ws.paragraph_format.left_indent = Cm(1.2)
            p_ws.paragraph_format.space_after = Pt(0)
            r_ws_l = p_ws.add_run("Wskazanie: ")
            _set_font(r_ws_l, bold=True, size=9)
            r_ws_v = p_ws.add_run(str(lek.get("wskazanie", "")))
            _set_font(r_ws_v, size=9)

            # Dawkowanie
            p_dw = doc.add_paragraph()
            p_dw.paragraph_format.left_indent = Cm(1.2)
            p_dw.paragraph_format.space_after = Pt(6)
            r_dw_l = p_dw.add_run("Dawkowanie: ")
            _set_font(r_dw_l, bold=True, size=9)
            r_dw_v = p_dw.add_run(str(lek.get("dawkowanie", "")))
            _set_font(r_dw_v, italic=True, size=9, color=GREY)

    nota_farm = farm.get("nota_farmaceutyczna", "") if isinstance(farm, dict) else ""
    if nota_farm:
        doc.add_paragraph()
        nota_kursywa(nota_farm, size=9)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # VI. PRZEBIEG HOSPITALIZACJI — format akapitowy, bez tabel
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VI. PRZEBIEG HOSPITALIZACJI — 14 DNI")

    dni_all = (raport.get("hospitalizacja_tydzien_1", []) or []) + \
              (raport.get("hospitalizacja_tydzien_2", []) or [])

    for d in dni_all:
        if not isinstance(d, dict):
            continue

        dzien = d.get("dzien", "?")
        data  = d.get("data", "")

        # Nagłówek dnia
        p_dzien = doc.add_paragraph()
        p_dzien.paragraph_format.space_before = Pt(8)
        p_dzien.paragraph_format.space_after  = Pt(1)
        r_dzien = p_dzien.add_run(f"DZIEN {dzien}   /   {data}")
        _set_font(r_dzien, bold=True, size=10, color=DKRED)

        # Zdarzenie
        zdarz = d.get("zdarzenie", "")
        if zdarz:
            maszyna(zdarz, size=9, space_before=2, space_after=2)

        # Lek
        lek_d = d.get("lek", "")
        if lek_d:
            p_ld = doc.add_paragraph()
            p_ld.paragraph_format.left_indent = Cm(0.5)
            p_ld.paragraph_format.space_after = Pt(1)
            r_ld_l = p_ld.add_run("Podano: ")
            _set_font(r_ld_l, bold=True, size=9)
            r_ld_v = p_ld.add_run(str(lek_d))
            _set_font(r_ld_v, size=9)

        # Stan pacjenta
        stan = d.get("stan_pacjenta", "")
        if stan:
            p_st = doc.add_paragraph()
            p_st.paragraph_format.left_indent = Cm(0.5)
            p_st.paragraph_format.space_after = Pt(1)
            r_st_l = p_st.add_run("Ocena: ")
            _set_font(r_st_l, bold=True, size=9)
            r_st_v = p_st.add_run(str(stan))
            _set_font(r_st_v, size=9, color=GREY)

        # Nota lekarska
        nota = d.get("nota_lekarska", "")
        if nota:
            nota_kursywa(nota, size=8)

        separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # VII. KARTA WYPISU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VII. KARTA WYPISU")
    wypis = raport.get("wypis", {})
    if isinstance(wypis, dict):
        pole("Dzien wypisu",  wypis.get("dzien_wypisu", ""))
        pole("Powod wypisu",  wypis.get("powod_wypisu", ""))

        stan_wip = wypis.get("stan_przy_wypisie", "")
        if stan_wip:
            doc.add_paragraph()
            podnaglowek("Stan pacjenta przy wypisie")
            maszyna(stan_wip, size=10, space_after=4)

        doc.add_paragraph()
        podnaglowek("Zalecenia po wypisie")
        zal = wypis.get("zalecenia_po_wypisie", [])
        if isinstance(zal, list):
            for i, z in enumerate(zal, 1):
                punkt_listy(str(z), numer=i, size=9)
        elif zal:
            maszyna(str(zal), size=9)

        poz = wypis.get("opis_pozegnania", "")
        if poz:
            doc.add_paragraph()
            cytat_blok(poz, size=9)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # VIII. DIAGNOZA PSYCHIATRYCZNA
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("VIII. DIAGNOZA PSYCHIATRYCZNA")

    dw = raport.get("diagnoza_wstepna", {})
    if isinstance(dw, dict) and dw.get("nazwa_lacinska"):
        podnaglowek("Diagnoza Wstepna")
        p_dg = doc.add_paragraph()
        p_dg.paragraph_format.space_after = Pt(2)
        r_dg1 = p_dg.add_run(dw.get("nazwa_lacinska", ""))
        _set_font(r_dg1, bold=True, size=11, color=DKRED)
        if dw.get("nazwa_polska"):
            r_dg2 = p_dg.add_run(f"  /  pol.: {dw['nazwa_polska']}")
            _set_font(r_dg2, size=10, italic=True)
        if dw.get("kod_dsm"):
            pole("Kod DSM", dw["kod_dsm"], size=9)
        if dw.get("opis_kliniczny"):
            maszyna(dw["opis_kliniczny"], size=9, space_before=4, space_after=4)

    dd = raport.get("diagnoza_dodatkowa", {})
    if isinstance(dd, dict) and dd.get("nazwa_lacinska"):
        doc.add_paragraph()
        podnaglowek("Diagnoza Dodatkowa (wspolistniejaca)")
        p_dd = doc.add_paragraph()
        p_dd.paragraph_format.space_after = Pt(2)
        r_dd1 = p_dd.add_run(dd.get("nazwa_lacinska", ""))
        _set_font(r_dd1, bold=True, size=11, color=DKRED)
        if dd.get("nazwa_polska"):
            r_dd2 = p_dd.add_run(f"  /  pol.: {dd['nazwa_polska']}")
            _set_font(r_dd2, size=10, italic=True)
        if dd.get("kod_dsm"):
            pole("Kod DSM", dd["kod_dsm"], size=9)
        if dd.get("opis_kliniczny"):
            maszyna(dd["opis_kliniczny"], size=9, space_before=4, space_after=4)

    objawy = raport.get("objawy", [])
    if objawy:
        doc.add_paragraph()
        podnaglowek("Objawy kliniczne")
        for i, obj in enumerate(objawy, 1):
            punkt_listy(str(obj), numer=i, size=9)

    separator_lekki()

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
            _set_font(r_zth, bold=True, size=10, color=DKRED)

        for key in ["zadanie_1", "zadanie_2", "zadanie_3"]:
            if zt.get(key):
                doc.add_paragraph()
                maszyna(str(zt[key]), size=10, space_before=4, space_after=4)

        if zt.get("podpis"):
            doc.add_paragraph()
            maszyna("Podpisano:", bold=True, size=9)
            podpis_odrecznie(str(zt["podpis"]), size=16)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # X. ROKOWANIE
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("X. ROKOWANIE")
    rok = raport.get("rokowanie", "")
    if rok:
        maszyna(rok, size=10, color=DKRED, space_after=4)

    separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # XI. INCYDENTY SPECJALNE
    # ══════════════════════════════════════════════════════════════════════════
    incydenty = raport.get("incydenty_specjalne", [])
    if incydenty:
        naglowek("XI. INCYDENTY SPECJALNE (protokoly wewnetrzne)")
        for i, inc in enumerate(incydenty, 1):
            doc.add_paragraph()
            p_inc_h = doc.add_paragraph()
            p_inc_h.paragraph_format.space_after = Pt(1)
            r_inc_h = p_inc_h.add_run(f"PROTOKOL INCYDENTU NR {i}:")
            _set_font(r_inc_h, bold=True, size=9, color=DKRED)
            maszyna(str(inc), size=9, space_before=0, space_after=4)

        separator_lekki()

    # ══════════════════════════════════════════════════════════════════════════
    # XII. PODPIS I NOTATKI PERSONELU
    # ══════════════════════════════════════════════════════════════════════════
    naglowek("XII. PODPIS I NOTATKI PERSONELU")

    pole("Lekarz prowadzacy", szpital.get("lekarz", "Dr. T. Durden, MD, PhD, FIGHT"))
    doc.add_paragraph()
    podpis_odrecznie("Tyler Durden", size=18)

    doc.add_paragraph()

    if raport.get("notatka_pielegniarki"):
        podnaglowek("Notatka pielegn. dyżurnej")
        maszyna(raport["notatka_pielegniarki"], italic=True, color=GREY, size=9)
        doc.add_paragraph()

    if raport.get("notatka_sprzataczki"):
        podnaglowek("Notatka sprzataczki (zalaczona z urzedu)")
        maszyna(raport["notatka_sprzataczki"], italic=True, color=GREY, size=9)
        doc.add_paragraph()

    # Podpis Marli
    maszyna("Kontrasygnata administracyjna:", bold=True, size=9)
    podpis_odrecznie("Marla Singer", size=15)

    # ══════════════════════════════════════════════════════════════════════════
    # ZAPIS
    # ══════════════════════════════════════════════════════════════════════════
    buf = io.BytesIO()
    doc.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    current_app.logger.info("[psych-docx] DOCX OK (%dKB)", len(buf.getvalue()) // 1024)
    return b64


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA PUBLICZNA
# ─────────────────────────────────────────────────────────────────────────────

def build_raport(body: str, previous_body: str | None, res_text: str,
                 nouns_dict: dict, sender_name: str = "",
                 gender: str = "patient") -> dict:
    """
    Główna funkcja modułu.

    Równoległość Groq:
      Runda 1 (niezależne): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
      Runda 2 (czekają na #2): #3 tydzień1 | #4 tydzień2 | #5 wypis
      Runda 3 (czeka na #3+#4): #7 zalecenia
      Potem: DeepSeek tone + completeness (sekwencyjnie)
      Potem: FLUX oba zdjęcia równolegle
      Potem: DOCX

    Jeśli HF_TOKEN nie działa → psych_photo_1/2 = None, DOCX budowany bez zdjęć.
    """
    from concurrent.futures import ThreadPoolExecutor
    from flask import current_app as flask_app

    current_app.logger.info("[psych-raport] START build_raport")
    app_obj = flask_app._get_current_object()
    cfg = _load_cfg()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 1 — niezależne sekcje Groq równolegle
    # ══════════════════════════════════════════════════════════════════════════
    def _r1_pacjent():
        with app_obj.app_context():
            return _sekcja_pacjent(cfg, body, sender_name)

    def _r1_depozyt():
        with app_obj.app_context():
            return _sekcja_depozyt_leki(cfg, body, nouns_dict)

    def _r1_diagnozy():
        with app_obj.app_context():
            return _sekcja_diagnozy(cfg, body, previous_body)

    def _r1_flux():
        with app_obj.app_context():
            return _sekcja_flux_prompty(cfg, body, nouns_dict, sender_name, gender)

    sekcja_pacjent  = {}
    sekcja_dep_leki = {}
    sekcja_diagnozy = {}
    sekcja_flux     = {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            "pacjent":  ex.submit(_r1_pacjent),
            "depozyt":  ex.submit(_r1_depozyt),
            "diagnozy": ex.submit(_r1_diagnozy),
            "flux":     ex.submit(_r1_flux),
        }
        for name, fut in futures.items():
            try:
                result = fut.result(timeout=60)
                if name == "pacjent":
                    sekcja_pacjent  = result
                elif name == "depozyt":
                    sekcja_dep_leki = result
                elif name == "diagnozy":
                    sekcja_diagnozy = result
                elif name == "flux":
                    sekcja_flux     = result
                current_app.logger.info("[psych-raport] Runda1 %s OK", name)
            except Exception as e:
                current_app.logger.error("[psych-raport] Runda1 %s błąd: %s", name, e)

    data_przyjecia = sekcja_pacjent.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    leki_lista     = sekcja_dep_leki.get("farmakologia", {}).get("leki", [])

    current_app.logger.info(
        "[psych-raport] data_przyjecia=%s | leki_lista=%d szt.",
        data_przyjecia, len(leki_lista) if leki_lista else 0
    )

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 2 — zależą od depozyt (leki_lista) i data_przyjecia
    # ══════════════════════════════════════════════════════════════════════════
    def _r2_tydzien1():
        with app_obj.app_context():
            return _sekcja_tydzien(cfg, body, leki_lista, 1, data_przyjecia)

    def _r2_tydzien2():
        with app_obj.app_context():
            return _sekcja_tydzien(cfg, body, leki_lista, 2, data_przyjecia)

    def _r2_wypis():
        with app_obj.app_context():
            return _sekcja_wypis(cfg, body, data_przyjecia)

    dni_1_7      = []
    dni_8_14     = []
    sekcja_wypis = {}

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures2 = {
            "tydzien1": ex.submit(_r2_tydzien1),
            "tydzien2": ex.submit(_r2_tydzien2),
            "wypis":    ex.submit(_r2_wypis),
        }
        for name, fut in futures2.items():
            try:
                result = fut.result(timeout=60)
                if name == "tydzien1":
                    dni_1_7      = result
                elif name == "tydzien2":
                    dni_8_14     = result
                elif name == "wypis":
                    sekcja_wypis = result
                current_app.logger.info("[psych-raport] Runda2 %s OK (%s elementów)",
                                        name, len(result) if isinstance(result, list) else "?")
            except Exception as e:
                current_app.logger.error("[psych-raport] Runda2 %s błąd: %s", name, e)

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 3 — zależy od tydzien1 + tydzien2
    # ══════════════════════════════════════════════════════════════════════════
    try:
        sekcja_zalecenia = _sekcja_zalecenia(cfg, body, dni_1_7, dni_8_14)
        current_app.logger.info("[psych-raport] Runda3 zalecenia OK")
    except Exception as e:
        current_app.logger.error("[psych-raport] Runda3 zalecenia błąd: %s", e)
        sekcja_zalecenia = {}

    # ── Scal wszystkie sekcje ─────────────────────────────────────────────────
    raport = {}
    raport.update(sekcja_pacjent)
    raport["depozyt"]                  = sekcja_dep_leki.get("depozyt", {})
    raport["farmakologia"]             = sekcja_dep_leki.get("farmakologia", {})
    raport["hospitalizacja_tydzien_1"] = dni_1_7
    raport["hospitalizacja_tydzien_2"] = dni_8_14
    raport.update(sekcja_wypis)
    raport.update(sekcja_diagnozy)
    raport.update(sekcja_zalecenia)

    current_app.logger.info("[psych-raport] Scalono %d kluczy przed DeepSeek", len(raport))

    # ── DeepSeek tone check ───────────────────────────────────────────────────
    raport = _deepseek_tone_check(cfg, raport)
    current_app.logger.info("[psych-raport] DeepSeek#1 tone OK")

    # ── DeepSeek completeness check ──────────────────────────────────────────
    raport = _deepseek_completeness_check(cfg, raport, body)
    current_app.logger.info("[psych-raport] DeepSeek#2 completeness OK")

    # ── FLUX — oba zdjęcia równolegle
    prompt_pacjent    = sekcja_flux.get("prompt_pacjent", "")
    prompt_przedmioty = sekcja_flux.get("prompt_przedmioty", "")
    photo_1, photo_2  = _generate_photos_parallel(prompt_pacjent, prompt_przedmioty)
    current_app.logger.info("[psych-raport] FLUX photo1=%s photo2=%s",
                            bool(photo_1), bool(photo_2))

    # ── Buduj DOCX (zawsze — nawet bez zdjęć) ────────────────────────────────
    photo_1_b64 = photo_1["base64"] if photo_1 else None
    photo_2_b64 = photo_2["base64"] if photo_2 else None
    docx_b64    = _build_docx(raport, photo_1_b64, photo_2_b64, cfg)

    if not docx_b64:
        current_app.logger.error("[psych-raport] DOCX nie wygenerowany")
        return {"raport_pdf": None, "psych_photo_1": photo_1, "psych_photo_2": photo_2}

    imie = raport.get("dane_pacjenta", {}).get("imie_nazwisko", "pacjent")
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', imie)[:30]

    raport_pdf_dict = {
        "base64":       docx_b64,
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "filename":     f"raport_psychiatryczny_{safe}_{ts}.docx",
    }

    current_app.logger.info(
        "[psych-raport] DONE raport=%s photo1=%s photo2=%s",
        raport_pdf_dict["filename"],
        photo_1["filename"] if photo_1 else "brak",
        photo_2["filename"] if photo_2 else "brak",
    )

    return {
        "raport_pdf":    raport_pdf_dict,
        "psych_photo_1": photo_1,
        "psych_photo_2": photo_2,
    }
