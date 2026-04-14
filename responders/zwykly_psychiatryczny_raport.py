"""
responders/zwykly_psychiatryczny_raport.py

Moduł obsługujący CAŁY pipeline raportu psychiatrycznego:
  - 10 wywołań DeepSeek (każda sekcja osobno)
      Runda 1 (równolegle): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
      Runda 2 (równolegle): #3a tydzień1 dni 1-4 | #3b tydzień1 dni 5-7
                            #4a tydzień2 dni 8-11 | #4b tydzień2 dni 12-14 | #5 wypis
      Runda 3: #7a zalecenia+rokowanie | #7b notatki_pielegniarek | #7c notatki_sprzataczki+incydenty
  - 2 wywołania DeepSeek (tone check + completeness check, merge JSON)
  - 2 zdjęcia FLUX generowane sekwencyjnie
  - Budowanie DOCX (python-docx)
  - Zwraca dict: {raport_pdf, psych_photo_1, psych_photo_2}

ZMIANY v5:
  - _sekcja_tydzien podzielona na chunki (3-4 dni każdy) → koniec z uciętymi JSON
  - zalecenia podzielone na 3 osobne wywołania (7a/7b/7c) → koniec z powtarzalnością
  - Każdy chunk przekazuje listę już użytych motywów → zakaz powtórzeń
  - max_tokens obniżone do bezpiecznych wartości per chunk
"""

import os
import io
import re
import json
import base64
import random
import requests
import time
import concurrent.futures
from datetime import datetime, timedelta
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER
from core.config import (
    HF_API_URL,
    HF_STEPS,
    HF_GUIDANCE,
    HF_TIMEOUT,
    MAX_DLUGOSC_EMAIL,
    HF_TOKEN_BLACKLIST,
)
from core.logging_reporter import get_logger

_HF_DEAD_TOKENS: set[str] = HF_TOKEN_BLACKLIST.copy()  # Kopia globalnej blacklist

# ─────────────────────────────────────────────────────────────────────────────
# ŚCIEŻKI
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
RAPORT_JSON = os.path.join(PROMPTS_DIR, "zwykly_raport.json")
SUBSTITUTE_IMAGE_PATH = os.path.join(BASE_DIR, "images", "zastepczy.jpg")

def _parse_json_safe(raw: str, section: str) -> dict | list | None:
    if not raw:
        return None
    try:
        clean = re.sub(r'^```[a-z]*', '', raw.strip(), flags=re.M)
        clean = re.sub(r'```\s*$', '', clean, flags=re.M)
        m = re.search(r'[\[{].*[\]}]', clean, re.DOTALL)
        if m:
            clean = m.group(0)
        result = json.loads(clean.strip())
        current_app.logger.info("[psych-raport] JSON OK sekcja=%s", section)
        return result
    except json.JSONDecodeError as e:
        current_app.logger.warning("[psych-raport] JSON błąd sekcja=%s: %s — próba naprawy uciętego JSON",
                                   section, e)
        try:
            raw_stripped = re.sub(r'^```[a-z]*', '', raw.strip(), flags=re.M)
            raw_stripped = re.sub(r'```\s*$', '', raw_stripped, flags=re.M).strip()
            start = next((i for i, c in enumerate(raw_stripped) if c in '{['), None)
            if start is None:
                return None
            fragment = raw_stripped[start:]
            opens    = fragment.count('{') - fragment.count('}')
            opens_sq = fragment.count('[') - fragment.count(']')
            lines = fragment.rstrip().splitlines()
            while lines:
                last = lines[-1].rstrip()
                if last.endswith((',', '{', '[')):
                    lines[-1] = last.rstrip(',')
                    break
                if last.endswith('"') and last.count('"') % 2 == 1:
                    lines.pop()
                    continue
                break
            repaired = '\n'.join(lines)
            repaired += (']' * max(opens_sq, 0)) + ('}' * max(opens, 0))
            result = json.loads(repaired)
            current_app.logger.warning("[psych-raport] JSON naprawiony sekcja=%s (ucięty output)", section)
            return result
        except Exception as e2:
            current_app.logger.warning("[psych-raport] JSON naprawa nieudana sekcja=%s: %s | raw=%.150s",
                                       section, e2, raw)
            return None
    except Exception as e:
        current_app.logger.warning("[psych-raport] JSON błąd sekcja=%s: %s | raw=%.150s",
                                   section, e, raw)
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


def _load_substitute_image() -> dict | None:
    if not os.path.exists(SUBSTITUTE_IMAGE_PATH):
        current_app.logger.warning("[psych-test] Brak pliku zastępczego: %s", SUBSTITUTE_IMAGE_PATH)
        return None
    try:
        with open(SUBSTITUTE_IMAGE_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {
            "base64":       b64,
            "content_type": "image/jpeg",
            "filename":     "zastepczy.jpg",
        }
    except Exception as e:
        current_app.logger.warning("[psych-test] Błąd odczytu zastepczy.jpg: %s", e)
        return None


def _add_text_below_image(image_obj: dict, text: str, panel_index: int) -> dict:
    """
    Rozszerza obrazek o 18% na dole i dopisuje tekst Pillow.
    Zwraca nowy dict z zaktualizowanym base64/filename.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        raw = base64.b64decode(image_obj["base64"])
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        W, H = img.size

        # Pasek na dole — 18% wysokości, min 80px
        bar_h = max(80, int(H * 0.18))
        new_img = Image.new("RGB", (W, H + bar_h), (10, 10, 10))
        new_img.paste(img, (0, 0))

        draw = ImageDraw.Draw(new_img)

        PADDING = 24
        max_w = W - PADDING * 2

        def load_font(size):
            for font_path in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            ]:
                try:
                    return ImageFont.truetype(font_path, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        def wrap_text(txt, fnt, max_px):
            words = txt.split()
            lines_out = []
            current = ""
            for word in words:
                test = (current + " " + word).strip()
                bbox = draw.textbbox((0, 0), test, font=fnt)
                if bbox[2] - bbox[0] <= max_px:
                    current = test
                else:
                    if current:
                        lines_out.append(current)
                    current = word
            if current:
                lines_out.append(current)
            return lines_out

        # Dobierz font_size tak żeby tekst zmieścił się w max 4 liniach w pasku
        font_size = max(10, bar_h // 4)
        for attempt in range(14):
            font = load_font(font_size)
            lines_out = wrap_text(text, font, max_w)
            line_h = font_size + 6
            total_h = len(lines_out) * line_h
            if total_h <= bar_h - 8 and len(lines_out) <= 4:
                break
            font_size = max(10, font_size - 2)

        lines_out = lines_out[:4]

        # Rysuj tekst — wyśrodkowany w pasku
        line_h = font_size + 6
        total_text_h = len(lines_out) * line_h
        y = H + (bar_h - total_text_h) // 2
        for line in lines_out:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            # cień
            draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(220, 210, 180))
            y += line_h

        buf = io.BytesIO()
        new_img.save(buf, format="JPEG", quality=85, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"psych_{ts}_substitute.jpg"

        result = dict(image_obj)
        result["base64"] = b64
        result["filename"] = filename
        result["size_jpg"] = f"{len(buf.getvalue()) // 1024}KB"
        result["caption"] = text
        return result

    except Exception as e:
        current_app.logger.warning("[psych-txt] Błąd dopisywania tekstu: %s", e)
        return image_obj


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
# DEEPSEEK #1 — dane pacjenta + powód + cytaty
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_pacjent(cfg: dict, body: str, sender_name: str) -> dict:
    sec = cfg.get("deepseek_1_pacjent", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"SENDER_NAME (priorytet dla imienia): {sender_name or '(brak)'}\n\n"
        f"SCHEMAT JSON do wypełnienia:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "pacjent")
    if not result:
        fb = cfg.get("fallback_dane_pacjenta", {})
        current_app.logger.warning("[psych-raport] sekcja_pacjent → fallback")
        return fb
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #2 — depozyt + leki
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_depozyt_leki(cfg: dict, body: str, nouns_dict: dict) -> dict:
    sec = cfg.get("deepseek_2_depozyt_leki", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    nouns_str = ", ".join(nouns_dict.values()) if nouns_dict else "(brak rzeczowników)"
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"RZECZOWNIKI Z EMAILA (każdy musi mieć swój lek): {nouns_str}\n\n"
        f"SCHEMAT JSON do wypełnienia:\n{schema}\n\n"
        f"Pamiętaj: JEDEN LEK per rzeczownik, nazwa leku nawiązuje do rzeczownika. Zwróć TYLKO czysty JSON."
    )
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "depozyt_leki")
    if not result:
        current_app.logger.warning("[psych-raport] sekcja_depozyt_leki → fallback")
        return {
            "depozyt": {
                "lista_przedmiotow": list(nouns_dict.values()) if nouns_dict else ["przedmiot nieznany"],
                "protokol_depozytu": "Odebrano przedmioty niebezpieczne. Pacjent protestował."
            },
            "farmakologia": {
                "leki": [{"nazwa": "Nihilizyna 500mg", "rzeczownik_zrodlowy": "email",
                          "wskazanie": "nadmierny optymizm", "dawkowanie": "2x dziennie po każdej nadziei"}],
                "nota_farmaceutyczna": "Farmakoterapia wdrożona. Rokowanie: złe."
            }
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #3/#4 — tygodnie hospitalizacji (CHUNKI po 3-4 dni)
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_tydzien_chunk(cfg: dict, body: str, leki: list,
                           tydzien: int, data_przyjecia: str,
                           dni_od: int, dni_do: int,
                           uzyte_motywy: list | None = None) -> list:
    """
    Generuje chunk dni hospitalizacji od dni_od do dni_do (włącznie).
    dni_od/dni_do to numery dni w hospitalizacji (1-14).
    uzyte_motywy — lista krótkich opisów zdarzeń już wygenerowanych,
                   żeby model nie powtarzał tych samych motywów.
    """
    sec_key = f"deepseek_{2 + tydzien}_tydzien{tydzien}"
    sec = cfg.get(sec_key, {})
    system = sec.get("system", "")

    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
    except Exception:
        base_date = datetime.now()

    # Oblicz daty dla tego chunku
    daty = {
        d: (base_date + timedelta(days=d - 1)).strftime("%d.%m.%Y")
        for d in range(dni_od, dni_do + 1)
    }
    daty_str = "\n".join(f"Dzień {d}: {dt}" for d, dt in daty.items())

    leki_str = json.dumps(leki, ensure_ascii=False, indent=2) if leki else "[]"

    # Schema dynamiczny dla konkretnych dni
    schema_days = [
        {
            "dzien": d,
            "data": daty[d],
            "zdarzenie": f"Min. 4-5 zdań. Styl Szwejka. Nawiązuje do emaila. Brak → '__BRAK__'",
            "lek": "Nazwa leku + dawka lub '__BRAK__'",
            "stan_pacjenta": "Jedno zdanie nihilistyczne lub '__BRAK__'",
            "nota_lekarska": "2-3 zdania lub '__BRAK__'"
        }
        for d in range(dni_od, dni_do + 1)
    ]

    zakaz_str = ""
    if uzyte_motywy:
        zakaz_str = (
            f"\nBEZWZGLĘDNY ZAKAZ powtarzania tych motywów (już użyte w poprzednich dniach):\n"
            + "\n".join(f"- {m}" for m in uzyte_motywy[:12])
            + "\nKażdy dzień MUSI mieć INNE zdarzenie — nowy motyw, nowa sytuacja.\n"
        )

    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"LISTA LEKÓW DO UŻYCIA:\n{leki_str}\n\n"
        f"DATY HOSPITALIZACJI — użyj DOKŁADNIE tych dat:\n{daty_str}\n"
        f"{zakaz_str}\n"
        f"SCHEMAT JSON (wygeneruj tablicę {dni_do - dni_od + 1} obiektów "
        f"dla dni {dni_od}-{dni_do}):\n"
        f"{json.dumps(schema_days, ensure_ascii=False, indent=2)}\n\n"
        f"Zwróć TYLKO czysty JSON z tablicą {dni_do - dni_od + 1} obiektów."
    )

    section_name = f"tydzien{tydzien}_dni{dni_od}-{dni_do}"
    # max_tokens: ~700 tokenów na dzień × liczba dni + bufor
    mt = (dni_do - dni_od + 1) * 700 + 300
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, section_name)

    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list) and len(v) > 0:
                return v

    # fallback
    current_app.logger.warning("[psych-raport] %s → fallback", section_name)
    return [
        {
            "dzien": d,
            "data": daty[d],
            "zdarzenie": "__BRAK__",
            "lek": "__BRAK__",
            "stan_pacjenta": "__BRAK__",
            "nota_lekarska": "__BRAK__"
        }
        for d in range(dni_od, dni_do + 1)
    ]


def _sekcja_tydzien(cfg: dict, body: str, leki: list, tydzien: int,
                    data_przyjecia: str) -> list:
    """
    Wrapper: generuje pełny tydzień w dwóch chunkach.
    Tydzień 1 → dni 1-4 + dni 5-7
    Tydzień 2 → dni 8-11 + dni 12-14
    Drugi chunk dostaje listę motywów z pierwszego → zero powtórzeń.
    """
    if tydzien == 1:
        start = 1
    else:
        start = 8

    mid  = start + 3   # po 4 dniach
    end  = start + 6   # 7 dni razem

    # Chunk A (4 dni)
    chunk_a = _sekcja_tydzien_chunk(
        cfg, body, leki, tydzien, data_przyjecia,
        dni_od=start, dni_do=mid
    )

    # Wyciągnij użyte motywy z chunk A
    uzyte = [
        str(d.get("zdarzenie", ""))[:100]
        for d in chunk_a
        if isinstance(d, dict) and d.get("zdarzenie") not in (None, "", "__BRAK__")
    ]

    # Chunk B (3 dni) z zakazem powtórzeń
    chunk_b = _sekcja_tydzien_chunk(
        cfg, body, leki, tydzien, data_przyjecia,
        dni_od=mid + 1, dni_do=end,
        uzyte_motywy=uzyte
    )

    return chunk_a + chunk_b


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #5 — wypis (dzień 15)
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_wypis(cfg: dict, body: str, data_przyjecia: str) -> dict:
    sec = cfg.get("deepseek_5_wypis", {})
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
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "wypis")
    if not result:
        return {"wypis": {
            "dzien_wypisu": f"Dzień 15, {data_wypisu}",
            "stan_przy_wypisie": "Pacjent osiągnął akceptowalny poziom beznadziei.",
            "powod_wypisu": "Wyczerpanie budżetu nadziei.",
            "zalecenia_po_wypisie": ["Unikać optymizmu.", "Nie planować remontów.", "Nie pisać emaili."],
            "opis_pozegnania": "Pacjent wyszedł bez słowa. Drzwi zostawił otwarte."
        }}
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #6 — łacińskie diagnozy
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_diagnozy(cfg: dict, body: str, previous_body: str) -> dict:
    sec = cfg.get("deepseek_6_diagnozy_lacina", {})
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        + (f"POPRZEDNI EMAIL (dla diagnozy_dodatkowej):\n{previous_body[:1000]}\n\n"
           if previous_body else "")
        + f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "diagnozy")
    if not result:
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
# DEEPSEEK #7 — zalecenia + notatki + rokowanie (TRZY OSOBNE WYWOŁANIA)
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_zalecenia(cfg: dict, body: str, dni_1_7: list, dni_8_14: list) -> dict:
    """
    Podzielone na TRZY osobne wywołania DeepSeek:
      deepseek_7a — zalecenia_tylera + rokowanie          (max_tokens: 1500)
      deepseek_7b — notatki_pielegniarek                  (max_tokens: 2500)
      deepseek_7c — notatki_sprzataczki + incydenty       (max_tokens: 2500)
    Wyniki scalane w jeden dict.
    """
    sec_7 = cfg.get("deepseek_7_zalecenia_notatki", {})
    system_7 = sec_7.get("system", "")

    # Kontekst zdarzeń z hospitalizacji
    zdarzenia = []
    for d in (dni_1_7 or []) + (dni_8_14 or []):
        if isinstance(d, dict) and d.get("zdarzenie") not in (None, "", "__BRAK__"):
            zdarzenia.append(f"Dzień {d.get('dzien', '?')}: {str(d['zdarzenie'])[:120]}")
    zdarzenia_str = "\n".join(zdarzenia[:14]) if zdarzenia else "(brak)"

    email_fragment = body[:MAX_DLUGOSC_EMAIL]

    # ── deepseek_7a — ZALECENIA + ROKOWANIE ──────────────────────────────────────
    schema_7a = json.dumps({
        "zalecenia_tylera": sec_7.get("schema", {}).get("zalecenia_tylera", {
            "naglowek": "RACHUNEK ZA WYZWOLENIE — ZADANIA OBOWIĄZKOWE",
            "zadanie_1": "...",
            "zadanie_2": "...",
            "zadanie_3": "...",
            "podpis": "Tyler Durden"
        }),
        "rokowanie": sec_7.get("schema", {}).get("rokowanie",
            "Min. 5-6 zdań. Bezlitosne. Nawiązuje do emaila.")
    }, ensure_ascii=False, indent=2)

    user_7a = (
        f"EMAIL PACJENTA (PRIORYTET — każde pole MUSI nawiązywać do treści emaila):\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (wypełnij TYLKO te klucze):\n{schema_7a}\n\n"
        f"zalecenia_tylera.zadanie_1/2/3: min. 5-6 zdań każde, konkretny przedmiot/plan/rzecz z emaila.\n"
        f"rokowanie: min. 5-6 zdań, bezlitosne, każde zdanie nawiązuje do emaila.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_7a = call_deepseek(system_7, user_7a, MODEL_TYLER)
    result_7a = _parse_json_safe(raw_7a, "zalecenia_7a") or {}

    # ── deepseek_7b — NOTATKI PIELĘGNIAREK ───────────────────────────────────────
    schema_7b = json.dumps({
        "notatki_pielegniarek": sec_7.get("schema", {}).get("notatki_pielegniarek",
            "Lista MINIMUM 10 obiektów: {imie_pielegniarki, data, tresc}")
    }, ensure_ascii=False, indent=2)

    user_7b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (wypełnij TYLKO klucz notatki_pielegniarek):\n{schema_7b}\n\n"
        f"notatki_pielegniarek: MINIMUM 10 obiektów. Każdy: imie_pielegniarki, data (DD.MM.YYYY), "
        f"tresc (3-4 zdania gwarą polską — śląska/mazurska/podlaska mieszanka, ciepła dosadna kobieta ze wsi, "
        f"nawiązuje do KONKRETNYCH zachowań pacjenta z emaila i zdarzenia z hospitalizacji).\n"
        f"KAŻDA notatka musi nawiązywać do INNEGO zdarzenia i INNEGO dnia — bezwzględny zakaz powtórzeń.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_7b = call_deepseek(system_7, user_7b, MODEL_TYLER)
    result_7b = _parse_json_safe(raw_7b, "zalecenia_7b") or {}

    # ── deepseek_7c — NOTATKI SPRZĄTACZKI + INCYDENTY ────────────────────────────
    schema_7c = json.dumps({
        "notatki_sprzataczki": sec_7.get("schema", {}).get("notatki_sprzataczki",
            "Lista MINIMUM 10 obiektów: {data, tresc}"),
        "incydenty_specjalne": sec_7.get("schema", {}).get("incydenty_specjalne",
            "Lista MINIMUM 10 incydentów po 4-5 zdań")
    }, ensure_ascii=False, indent=2)

    user_7c = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (wypełnij TYLKO klucze notatki_sprzataczki i incydenty_specjalne):\n{schema_7c}\n\n"
        f"notatki_sprzataczki: MINIMUM 10 obiektów. Każdy: data (DD.MM.YYYY), "
        f"tresc (2-3 zdania — co znalazła sprzątając salę, gwarą polską, humor Szwejka/Monty Python, "
        f"nawiązuje do emaila pacjenta). KAŻDA notatka musi mówić o INNYM znalezisku.\n"
        f"incydenty_specjalne: MINIMUM 10 incydentów, każdy 4-5 zdań, NAPRAWDĘ absurdalnych i śmiesznych, "
        f"nawiązujących do emaila. KAŻDY incydent INNY motyw. "
        f"Format: 'Protokół Incydentu [nr]: [tytuł]. [4-5 zdań]'.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_7c = call_deepseek(system_7, user_7c, MODEL_TYLER)
    result_7c = _parse_json_safe(raw_7c, "zalecenia_7c") or {}

    # ── Scal wyniki trzech wywołań ────────────────────────────────────────────
    result = {}
    result.update(result_7a)
    result.update(result_7b)
    result.update(result_7c)

    if not result:
        return {
            "zalecenia_tylera": {
                "naglowek": "ZALECENIA TERAPEUTYCZNE — PROTOKÓŁ AWARYJNY",
                "zadanie_1": "Zidentyfikować i wyeliminować główne źródło złudzeń.",
                "zadanie_2": "Wyrzeknij się publicznie planów opisanych w wiadomości przychodzącej.",
                "zadanie_3": "Spalić wszystkie notatki związane z treścią emaila.",
                "podpis": "Dr. Tyler Durden, Ordynator Oddziału Beznadziei"
            },
            "rokowanie": "Trudne. Pacjent przejawia objawy niezdrowego optymizmu.",
            "notatki_pielegniarek": [],
            "notatki_sprzataczki": [],
            "incydenty_specjalne": []
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #8 — prompty FLUX
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_flux_prompty(cfg: dict, body: str, nouns_dict: dict,
                          sender_name: str, gender: str, test_mode: bool = False) -> dict:
    sec = cfg.get("deepseek_8_flux_prompty", {})
    if test_mode:
        current_app.logger.info("[psych-raport] test_mode — pomijam generowanie promptów FLUX")
        return {"prompt_pacjent": "", "prompt_przedmioty": ""}
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
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "flux_prompty")
    if not result or not isinstance(result, dict):
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

# Klucze pomijane w obu DeepSeek — zbyt duże i już wygenerowane z właściwym stylem
_DEEPSEEK_SKIP = {
    "hospitalizacja_tydzien_1",
    "hospitalizacja_tydzien_2",
    "notatki_pielegniarek",
    "notatki_sprzataczki",
    "incydenty_specjalne",
    "cytaty_z_przyjecia",   # długie, DeepSeek generuje je już z właściwym stylem
    "wypis",                # też długie, styl już OK z DeepSeek
}


def _deepseek_tone_check(cfg: dict, raport: dict) -> dict:
    sec = cfg.get("deepseek_1_tone_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))

    raport_slim = {k: v for k, v in raport.items() if k not in _DEEPSEEK_SKIP}

    user = (
        f"RAPORT DO OCENY I POPRAWY:\n{json.dumps(raport_slim, ensure_ascii=False, indent=2)}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Zwróć TYLKO czysty JSON z poprawkami (ta sama struktura kluczy)."
    )
    current_app.logger.info("[psych-raport] DeepSeek tone check START (slim=%d kluczy)",
                            len(raport_slim))
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        current_app.logger.warning("[psych-raport] DeepSeek tone check → brak odpowiedzi, skip")
        return raport
    result = _parse_json_safe(raw, "deepseek_tone")
    if not result or not isinstance(result, dict):
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

    raport_slim = {k: v for k, v in raport.items() if k not in _DEEPSEEK_SKIP}

    user = (
        f"ORYGINALNY EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"RAPORT DO SPRAWDZENIA:\n{json.dumps(raport_slim, ensure_ascii=False, indent=2)}\n\n"
        f"INSTRUKCJE:\n{instrukcje}\n\n"
        f"Zwróć TYLKO czysty JSON z uzupełnionymi nawiązaniami do emaila."
    )
    current_app.logger.info("[psych-raport] DeepSeek completeness check START (slim=%d kluczy)",
                            len(raport_slim))
    raw = call_deepseek(system, user, MODEL_TYLER)
    if not raw:
        current_app.logger.warning("[psych-raport] DeepSeek completeness → brak odpowiedzi, skip")
        return raport
    result = _parse_json_safe(raw, "deepseek_completeness")
    if not result or not isinstance(result, dict):
        return raport
    merged = _merge_dicts(raport, result)
    current_app.logger.info("[psych-raport] DeepSeek completeness OK — merge zastosowany")
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #3 — relacje świadków
# ─────────────────────────────────────────────────────────────────────────────

def _sekcja_relacje_swiadkow(cfg: dict, body: str, raport: dict) -> dict:
    """
    Generuje relacje świadków: ksiądz, dostawca jedzenia, kurier, rodzina, hydraulik, serwisant automatu do kawy.
    Każda relacja minimum 4-5 zdań, w stylu gwarowym, absurdalnie śmieszna, nawiązująca do emaila.
    """
    sec = cfg.get("deepseek_3_relacje_swiadkow", {})
    if not sec:
        current_app.logger.warning("[psych-raport] Brak konfiguracji deepseek_3_relacje_swiadkow")
        return {"relacje_swiadkow": []}
    
    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    
    # Przygotuj kontekst z raportu: imię pacjenta, stan cywilny, kluczowe zachowania
    imie_pacjenta = raport.get("dane_pacjenta", {}).get("imie_nazwisko", "pacjent")
    stan_cywilny = raport.get("dane_pacjenta", {}).get("stan_cywilny", "")
    
    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"INFORMACJE O PACJENCIE:\n"
        f"- Imię: {imie_pacjenta}\n"
        f"- Stan cywilny: {stan_cywilny if stan_cywilny else 'nieznany'}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        f"Zwróć TYLKO czysty JSON z listą relacji świadków."
    )
    
    raw = call_deepseek(system, user, MODEL_TYLER)
    result = _parse_json_safe(raw, "relacje_swiadkow")
    
    if not result or not isinstance(result, dict):
        current_app.logger.warning("[psych-raport] _sekcja_relacje_swiadkow → brak wyników")
        return {"relacje_swiadkow": []}
    
    return result


# ─────────────────────────────────────────────────────────────────────────────
# FLUX — generowanie zdjęć
# ─────────────────────────────────────────────────────────────────────────────

def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(40)]
    return [
        (n, v)
        for n in names
        if n not in _HF_DEAD_TOKENS and (v := os.getenv(n, "").strip())
    ]


def _hf_credit_exhausted(resp: requests.Response) -> bool:
    if resp.status_code != 402:
        return False
    text = (resp.text or "").lower()
    return "depleted your monthly included credits" in text or "purchase pre-paid credits" in text


def _generate_flux(prompt: str, label: str,
                   steps: int = 28, guidance: float = 7.0,
                   width: int = 1024, height: int = 1024,
                   test_mode: bool = False) -> str | None:
    """Generuje obrazek FLUX. Zwraca base64 JPG lub None."""
    if os.getenv("HF_TOKENS_ACTIVE", "tak").strip().lower() == "nie":
        current_app.logger.info("[psych-flux] HF_TOKENS_ACTIVE=nie — pomijam FLUX (%s)", label)
        return None

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
    if test_mode:
        substitute = _load_substitute_image()
        if substitute:
            # Dodaj tekst na dole jak w obrazkach Flux
            caption = "pacjent" if label == "photo_pacjent" else "przedmioty"
            substitute = _add_text_below_image(substitute, f"Zdjęcie zastępcze - {caption}", 0)
            current_app.logger.info("[psych-flux] test_mode — używam zastepczy.jpg dla %s", label)
            return substitute.get("base64")
        current_app.logger.warning("[psych-flux] test_mode — brak zastepczy.jpg, pomijam %s", label)
        return None

    current_app.logger.info("[psych-flux] %s — prompt %.120s...", label, prompt)

    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(HF_API_URL, headers=headers,
                                 json=payload, timeout=HF_TIMEOUT)
            if resp.status_code == 200:
                current_app.logger.info("[psych-flux] %s OK token=%s (%dB)",
                                        label, name, len(resp.content))
                try:
                    from PIL import Image as PILImage
                    pil = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
                    buf = io.BytesIO()
                    pil.save(buf, format="JPEG", quality=92, optimize=True)
                    return base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception as e:
                    current_app.logger.warning("[psych-flux] PNG→JPG błąd: %s", e)
                    return base64.b64encode(resp.content).decode("ascii")
            elif resp.status_code == 402:
                _HF_DEAD_TOKENS.add(name)
                current_app.logger.warning(
                    "[psych-flux] %s 402 token=%s — wyczerpane kredyty, dodano do czarnej listy",
                    label, name
                )
                if _hf_credit_exhausted(resp):
                    current_app.logger.warning(
                        "[psych-flux] %s 402 wskazuje na globalne wyczerpanie kredytów — kończę próby",
                        label
                    )
                    break
            elif resp.status_code in (401, 403):
                _HF_DEAD_TOKENS.add(name)
                current_app.logger.warning(
                    "[psych-flux] %s HTTP %d token=%s — nieważny, dodano do czarnej listy",
                    label, resp.status_code, name
                )
            elif resp.status_code == 429:
                current_app.logger.warning("[psych-flux] %s 429 token=%s → następny", label, name)
            else:
                current_app.logger.warning("[psych-flux] %s HTTP %d token=%s",
                                           label, resp.status_code, name)
        except Exception as e:
            current_app.logger.warning("[psych-flux] %s wyjątek token=%s: %s", label, name, e)

    current_app.logger.error("[psych-flux] %s — wszystkie tokeny zawiodły", label)
    return None


def _generate_photos_parallel(prompt_pacjent: str, prompt_przedmioty: str, test_mode: bool = False) -> tuple:
    """
    Generuje oba zdjęcia równolegle. Zwraca (photo_pacjent, photo_przedmioty).
    """
    from flask import current_app as flask_app

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    app_obj = flask_app._get_current_object()

    def gen_pacjent():
        with app_obj.app_context():
            return _generate_flux(prompt_pacjent, "photo_pacjent", steps=28, guidance=7, test_mode=test_mode)

    def gen_przedmioty():
        with app_obj.app_context():
            return _generate_flux(prompt_przedmioty, "photo_przedmioty", steps=28, guidance=7, test_mode=test_mode)

    b64_pacjent    = None
    b64_przedmioty = None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_pacjent = executor.submit(gen_pacjent)
            fut_przedmioty = executor.submit(gen_przedmioty)
            try:
                b64_pacjent = fut_pacjent.result()
            except Exception as e:
                current_app.logger.warning("[psych-flux] photo_pacjent błąd: %s", e)
            try:
                b64_przedmioty = fut_przedmioty.result()
            except Exception as e:
                current_app.logger.warning("[psych-flux] photo_przedmioty błąd: %s", e)
    except Exception as e:
        current_app.logger.error("[psych-flux] Błąd generowania zdjęć: %s", e)

    def _wrap(b64, suffix):
        if not b64:
            return None
        if isinstance(b64, dict):
            return {
                "base64":       b64.get("base64"),
                "content_type": b64.get("content_type", "image/jpeg"),
                "filename":     b64.get("filename", f"psych_{suffix}_{ts}.jpg"),
            }
        return {
            "base64":       b64,
            "content_type": "image/jpeg",
            "filename":     f"psych_{suffix}_{ts}.jpg",
        }

    return _wrap(b64_pacjent, "pacjent"), _wrap(b64_przedmioty, "przedmioty")


# ─────────────────────────────────────────────────────────────────────────────
# BUDOWANIE DOCX
# ─────────────────────────────────────────────────────────────────────────────

def _build_docx(raport: dict, photo_pacjent_b64: str | None,
                photo_przedmioty_b64: str | None, cfg: dict) -> str | None:
    """
    Buduje DOCX z raportem psychiatrycznym.
    Zwraca base64 DOCX lub None.
    """
    try:
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError as e:
        current_app.logger.error("[psych-docx] Brak python-docx: %s", e)
        return None

    szpital = cfg.get("szpital", {})
    doc     = Document()

    for section in doc.sections:
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    # Dodaj stopkę z numerami stron
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    section = doc.sections[0]
    footer = section.footer
    footer_paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_run = footer_paragraph.add_run()
    footer_run.add_field('PAGE')
    footer_run.add_text(' / ')
    footer_run.add_field('NUMPAGES')
    footer_run.font.size = Pt(9)
    footer_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    RED   = RGBColor(0x99, 0x1A, 0x1A)
    DARK  = RGBColor(0x0D, 0x0D, 0x0D)
    GREY  = RGBColor(0x66, 0x66, 0x66)
    LGREY = RGBColor(0x99, 0x99, 0x99)

    def heading(text, level=1, color=DARK, size=14):
        h = doc.add_heading(text, level=level)
        h.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for run in h.runs:
            run.font.size      = Pt(size)
            run.font.color.rgb = color
        return h

    def para(text, bold=False, italic=False, color=DARK, size=10, align=None):
        p = doc.add_paragraph()
        if align:
            p.alignment = align
        r = p.add_run(str(text))
        r.bold           = bold
        r.italic         = italic
        r.font.size      = Pt(size)
        r.font.color.rgb = color
        return p

    def field(label, value, label_color=DARK, val_color=DARK, size=10):
        if not value:
            return
        p  = doc.add_paragraph()
        rl = p.add_run(f"{label}: ")
        rl.bold            = True
        rl.font.size       = Pt(size)
        rl.font.color.rgb  = label_color
        rv = p.add_run(str(value))
        rv.font.size       = Pt(size)
        rv.font.color.rgb  = val_color

    def separator():
        p = doc.add_paragraph()
        p.paragraph_format.space_after  = Pt(2)
        p.paragraph_format.space_before = Pt(2)
        r = p.add_run("─" * 72)
        r.font.size      = Pt(7)
        r.font.color.rgb = LGREY

    def insert_photo(b64: str, caption: str, width_cm: float = 14.0):
        if not b64:
            return
        try:
            img_bytes = base64.b64decode(b64)
            stream    = io.BytesIO(img_bytes)
            doc.add_picture(stream, width=Cm(width_cm))
            cap = doc.add_paragraph(caption)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in cap.runs:
                r.font.size      = Pt(8)
                r.font.italic    = True
                r.font.color.rgb = GREY
        except Exception as e:
            current_app.logger.warning("[psych-docx] Błąd wstawiania zdjęcia: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # NAGŁÓWEK SZPITALA
    # ══════════════════════════════════════════════════════════════════════════
    h1 = doc.add_heading(szpital.get("nazwa", "Szpital Psychiatryczny im. Tylera Durdena"), 1)
    h1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in h1.runs:
        r.font.size      = Pt(14)
        r.font.color.rgb = DARK

    p_adr = doc.add_paragraph(szpital.get("adres", ""))
    p_adr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in p_adr.runs:
        r.font.size      = Pt(9)
        r.font.color.rgb = GREY

    p_odd = doc.add_paragraph(szpital.get("oddzial", ""))
    p_odd.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in p_odd.runs:
        r.font.size      = Pt(9)
        r.font.color.rgb = GREY

    doc.add_paragraph()

    tyt = doc.add_heading("HISTORIA CHOROBY — KARTA PRZYJĘCIA I HOSPITALIZACJI", 2)
    tyt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in tyt.runs:
        r.font.size = Pt(12)

    nr         = raport.get("numer_historii_choroby", "NY-2026-00000")
    data_przyj = raport.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    nr_p = doc.add_paragraph(f"Nr: {nr}  |  Data przyjęcia: {data_przyj}  |  "
                              f"Lekarz prowadzący: {szpital.get('lekarz', 'Dr. T. Durden')}")
    nr_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in nr_p.runs:
        r.font.size      = Pt(9)
        r.font.color.rgb = GREY

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 1 — DANE PACJENTA
    # ══════════════════════════════════════════════════════════════════════════
    heading("I. DANE PACJENTA", 2, RED, 11)
    dp = raport.get("dane_pacjenta", {})
    field("Imię i nazwisko", dp.get("imie_nazwisko", ""))
    field("Wiek",            dp.get("wiek", ""))
    field("Adres",           dp.get("adres", ""))
    field("Zawód",           dp.get("zawod", ""))
    field("Stan cywilny",    dp.get("stan_cywilny", ""))
    field("Nr ubezpieczenia",dp.get("numer_ubezpieczenia", ""))
    doc.add_paragraph()

    if photo_pacjent_b64:
        heading("DOKUMENTACJA FOTOGRAFICZNA — PRZYJĘCIE", 3, GREY, 9)
        insert_photo(photo_pacjent_b64,
                     "Fot. 1 — Pacjent w kaftanie bezpieczeństwa. Oddział B. Materiał dowodowy.")
        doc.add_paragraph()

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 2 — POWÓD PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("II. POWÓD PRZYJĘCIA", 2, RED, 11)
    para(raport.get("powod_przyjecia", ""), size=10)
    doc.add_paragraph()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 3 — CYTATY Z PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("III. CYTATY Z IZBY PRZYJĘĆ", 2, RED, 11)
    cytaty = raport.get("cytaty_z_przyjecia", "")
    if isinstance(cytaty, list):
        for c in cytaty:
            para(str(c), italic=True, color=GREY, size=9)
    else:
        para(str(cytaty), italic=True, color=GREY, size=9)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 4 — DEPOZYT
    # ══════════════════════════════════════════════════════════════════════════
    heading("IV. PROTOKÓŁ DEPOZYTU — PRZEDMIOTY SKONFISKOWANE", 2, RED, 11)
    dep = raport.get("depozyt", {})
    if isinstance(dep, dict):
        lista = dep.get("lista_przedmiotow", [])
        proto = dep.get("protokol_depozytu", "")
        if isinstance(lista, str):
            lista = [x.strip() for x in lista.split(",") if x.strip()]
        for item in lista:
            p_item = doc.add_paragraph(style="List Bullet")
            r_item = p_item.add_run(str(item))
            r_item.font.size      = Pt(10)
            r_item.font.color.rgb = DARK
        if proto:
            doc.add_paragraph()
            para(proto, italic=True, color=GREY, size=9)
    doc.add_paragraph()

    if photo_przedmioty_b64:
        heading("DOKUMENTACJA FOTOGRAFICZNA — DOWODY RZECZOWE", 3, GREY, 9)
        insert_photo(photo_przedmioty_b64,
                     "Fot. 2 — Przedmioty skonfiskowane przy przyjęciu. "
                     "Protokół dowodów rzeczowych, Oddział B.")
        doc.add_paragraph()

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 5 — FARMAKOLOGIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("V. FARMAKOLOGIA — PEŁNA LISTA LEKÓW ZASTOSOWANYCH", 2, RED, 11)
    farm       = raport.get("farmakologia", {})
    leki_lista = farm.get("leki", []) if isinstance(farm, dict) else []

    if leki_lista:
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        hdr = t.rows[0].cells
        for i, label in enumerate(["Nazwa leku", "Przedmioty odebrane", "Wskazanie", "Dawkowanie"]):
            hdr[i].text = ""
            r = hdr[i].paragraphs[0].add_run(label)
            r.bold           = True
            r.font.size      = Pt(9)
            r.font.color.rgb = RED
        for lek in leki_lista:
            if not isinstance(lek, dict):
                continue
            row = t.add_row().cells
            row[0].text = str(lek.get("nazwa", ""))
            row[1].text = str(lek.get("rzeczownik_zrodlowy", ""))
            row[2].text = str(lek.get("wskazanie", ""))
            row[3].text = str(lek.get("dawkowanie", ""))
            for cell in row:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)
        doc.add_paragraph()

    nota_farm = farm.get("nota_farmaceutyczna", "") if isinstance(farm, dict) else ""
    if nota_farm:
        para(nota_farm, italic=True, color=GREY, size=9)

    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 6 — HOSPITALIZACJA (14 dni)
    # ══════════════════════════════════════════════════════════════════════════
    heading("VI. PRZEBIEG HOSPITALIZACJI — 14 DNI", 2, RED, 11)

    dni_all = (raport.get("hospitalizacja_tydzien_1", []) or []) + \
              (raport.get("hospitalizacja_tydzien_2", []) or [])

    for d in dni_all:
        if not isinstance(d, dict):
            continue
        dzien = d.get("dzien", "")
        data  = d.get("data", "")
        zdarz = d.get("zdarzenie", "")
        lek   = d.get("lek", "")
        stan  = d.get("stan_pacjenta", "")
        nota  = d.get("nota_lekarska", "")

        p_day = doc.add_paragraph(style="List Bullet")
        r_day = p_day.add_run(f"Dzień {dzien}  [{data}]")
        r_day.bold           = True
        r_day.font.size      = Pt(10)
        r_day.font.color.rgb = RED

        lines = []
        if zdarz and zdarz != "__BRAK__":
            lines.append(f"Zdarzenie: {zdarz}")
        if lek and lek != "__BRAK__":
            lines.append(f"Lek: {lek}")
        if stan and stan != "__BRAK__":
            lines.append(f"Stan: {stan}")
        for line in lines:
            p_line = doc.add_paragraph()
            p_line.paragraph_format.left_indent = Pt(24)
            r_line = p_line.add_run(line)
            r_line.font.size      = Pt(9)
            r_line.font.color.rgb = DARK

        if nota and nota != "__BRAK__":
            p_nota = doc.add_paragraph()
            p_nota.paragraph_format.left_indent = Pt(24)
            r_nota = p_nota.add_run(f"↳ {nota}")
            r_nota.italic         = True
            r_nota.font.size      = Pt(8)
            r_nota.font.color.rgb = GREY

    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 7 — WYPIS
    # ══════════════════════════════════════════════════════════════════════════
    heading("VII. KARTA WYPISU", 2, RED, 11)
    wypis = raport.get("wypis", {})
    if isinstance(wypis, dict):
        field("Dzień wypisu", wypis.get("dzien_wypisu", ""))
        field("Powód wypisu", wypis.get("powod_wypisu", ""))
        doc.add_paragraph()
        para(wypis.get("stan_przy_wypisie", ""), size=10)
        doc.add_paragraph()
        heading("Zalecenia po wypisie:", 3, DARK, 10)
        zal = wypis.get("zalecenia_po_wypisie", [])
        if isinstance(zal, list):
            for z in zal:
                p_z = doc.add_paragraph(style="List Bullet")
                p_z.add_run(str(z)).font.size = Pt(10)
        elif zal:
            para(str(zal), size=10)
        doc.add_paragraph()
        if wypis.get("opis_pozegnania"):
            para(wypis["opis_pozegnania"], italic=True, color=GREY, size=9)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 8 — DIAGNOZY
    # ══════════════════════════════════════════════════════════════════════════
    heading("VIII. DIAGNOZA PSYCHIATRYCZNA", 2, RED, 11)

    dw = raport.get("diagnoza_wstepna", {})
    if isinstance(dw, dict):
        heading("Diagnoza Wstępna:", 3, DARK, 10)
        p_diag = doc.add_paragraph()
        r1 = p_diag.add_run(dw.get("nazwa_lacinska", ""))
        r1.bold           = True
        r1.font.size      = Pt(11)
        r1.font.color.rgb = RED
        if dw.get("nazwa_polska"):
            r2 = p_diag.add_run(f" (pol. {dw['nazwa_polska']})")
            r2.font.size      = Pt(10)
            r2.font.color.rgb = DARK
        if dw.get("kod_dsm"):
            field("Kod DSM", dw["kod_dsm"], size=9)
        if dw.get("opis_kliniczny"):
            para(dw["opis_kliniczny"], size=10)
    doc.add_paragraph()

    dd = raport.get("diagnoza_dodatkowa", {})
    if isinstance(dd, dict):
        heading("Diagnoza Dodatkowa (współistniejąca):", 3, DARK, 10)
        p_dd = doc.add_paragraph()
        r1 = p_dd.add_run(dd.get("nazwa_lacinska", ""))
        r1.bold           = True
        r1.font.size      = Pt(11)
        r1.font.color.rgb = RED
        if dd.get("nazwa_polska"):
            r2 = p_dd.add_run(f" (pol. {dd['nazwa_polska']})")
            r2.font.size      = Pt(10)
            r2.font.color.rgb = DARK
        if dd.get("opis_kliniczny"):
            para(dd["opis_kliniczny"], size=10)
    doc.add_paragraph()

    objawy = raport.get("objawy", [])
    if objawy:
        heading("Objawy kliniczne:", 3, DARK, 10)
        for obj in objawy:
            p_obj = doc.add_paragraph(style="List Bullet")
            p_obj.add_run(str(obj)).font.size = Pt(10)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 9 — ZALECENIA TYLERA
    # ══════════════════════════════════════════════════════════════════════════
    heading("IX. ZALECENIA TERAPEUTYCZNE", 2, RED, 11)
    zt = raport.get("zalecenia_tylera", {})
    if isinstance(zt, dict):
        if zt.get("naglowek"):
            para(zt["naglowek"], bold=True, color=RED, size=10)
        for key in ["zadanie_1", "zadanie_2", "zadanie_3"]:
            if zt.get(key) and zt[key] != "__BRAK__":
                p_z = doc.add_paragraph(style="List Number")
                p_z.add_run(str(zt[key])).font.size = Pt(10)
        if zt.get("podpis"):
            doc.add_paragraph()
            para(zt["podpis"], italic=True, color=GREY, size=9)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 10 — ROKOWANIE
    # ══════════════════════════════════════════════════════════════════════════
    heading("X. ROKOWANIE", 2, RED, 11)
    rokowanie = raport.get("rokowanie", "").strip() if isinstance(raport.get("rokowanie"), str) else ""
    if not rokowanie or rokowanie == "__BRAK__":
        rokowanie = "---brak---"
    p_rok = doc.add_paragraph()
    r_rok = p_rok.add_run(rokowanie)
    r_rok.font.size      = Pt(10)
    r_rok.font.color.rgb = RED
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 11 — INCYDENTY SPECJALNE
    # ══════════════════════════════════════════════════════════════════════════
    incydenty = raport.get("incydenty_specjalne", [])
    if incydenty:
        heading("XI. INCYDENTY SPECJALNE (protokoły wewnętrzne)", 2, RED, 11)
        for inc in incydenty:
            if inc == "__BRAK__":
                continue
            p_inc = doc.add_paragraph(style="List Bullet")
            p_inc.add_run(str(inc)).font.size = Pt(10)
        doc.add_paragraph()
        separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 12 — RELACJE ŚWIADKÓW
    # ══════════════════════════════════════════════════════════════════════════
    heading("XII. RELACJE ŚWIADKÓW", 2, RED, 11)
    relacje_swiadkow = raport.get("relacje_swiadkow", [])
    if relacje_swiadkow:
        heading("Świadkowie (osoby trzecie) — zeznania:", 3, DARK, 9)
        if isinstance(relacje_swiadkow, list):
            for sw in relacje_swiadkow:
                if isinstance(sw, dict):
                    imie = sw.get("imie_swiadka", "")
                    zawod = sw.get("zawod", "")
                    data = sw.get("data", "")
                    tresc = sw.get("tresc", "")
                    if tresc == "__BRAK__":
                        continue
                    
                    # Nagłówek świadka
                    header_parts = []
                    if imie and imie != "__BRAK__":
                        header_parts.append(imie)
                    if zawod and zawod != "__BRAK__":
                        header_parts.append(f"({zawod})")
                    if data and data != "__BRAK__":
                        header_parts.append(data)
                    
                    if header_parts:
                        p_hdr = doc.add_paragraph()
                        p_hdr.paragraph_format.left_indent = Pt(12)
                        r_hdr = p_hdr.add_run("  ".join(header_parts))
                        r_hdr.bold = True
                        r_hdr.font.size = Pt(8)
                        r_hdr.font.color.rgb = DARK
                    
                    if tresc:
                        para(tresc, italic=True, color=GREY, size=8)
                else:
                    if str(sw) != "__BRAK__":
                        para(str(sw), italic=True, color=GREY, size=9)
        else:
            para(str(relacje_swiadkow), italic=True, color=GREY, size=9)
        doc.add_paragraph()
    else:
        para("Brak relacji świadków w dokumentacji.", italic=True, color=GREY, size=9)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 13 — PODPIS + NOTATKI
    # ══════════════════════════════════════════════════════════════════════════
    heading("XIII. PODPIS I NOTATKI PERSONELU", 2, RED, 11)
    field("Lekarz prowadzący", szpital.get("lekarz", "Dr. T. Durden, MD, PhD, FIGHT"))
    doc.add_paragraph()

    notatki_p = raport.get("notatki_pielegniarek") or raport.get("notatka_pielegniarki")
    if notatki_p:
        heading("Notatki pielęgniarek:", 3, DARK, 9)
        if isinstance(notatki_p, list):
            for n in notatki_p:
                if isinstance(n, dict):
                    imie  = n.get("imie_pielegniarki", "")
                    data  = n.get("data", "")
                    tresc = n.get("tresc", "")
                    if tresc == "__BRAK__":
                        continue
                    header_parts = [x for x in [imie, data] if x and x != "__BRAK__"]
                    if header_parts:
                        p_hdr = doc.add_paragraph()
                        p_hdr.paragraph_format.left_indent = Pt(12)
                        r_hdr = p_hdr.add_run("  ".join(header_parts))
                        r_hdr.bold      = True
                        r_hdr.font.size = Pt(8)
                        r_hdr.font.color.rgb = DARK
                    if tresc:
                        para(tresc, italic=True, color=GREY, size=8)
                else:
                    if str(n) != "__BRAK__":
                        para(str(n), italic=True, color=GREY, size=9)
        else:
            para(str(notatki_p), italic=True, color=GREY, size=9)
        doc.add_paragraph()

    notatki_s = raport.get("notatki_sprzataczki") or raport.get("notatka_sprzataczki")
    if notatki_s:
        heading("Notatki sprzątaczki:", 3, DARK, 9)
        if isinstance(notatki_s, list):
            for n in notatki_s:
                if isinstance(n, dict):
                    data  = n.get("data", "")
                    tresc = n.get("tresc", "")
                    if tresc == "__BRAK__":
                        continue
                    if data and data != "__BRAK__":
                        p_hdr = doc.add_paragraph()
                        p_hdr.paragraph_format.left_indent = Pt(12)
                        r_hdr = p_hdr.add_run(data)
                        r_hdr.bold      = True
                        r_hdr.font.size = Pt(8)
                        r_hdr.font.color.rgb = DARK
                    if tresc:
                        para(tresc, italic=True, color=GREY, size=8)
                else:
                    if str(n) != "__BRAK__":
                        para(str(n), italic=True, color=GREY, size=9)
        else:
            para(str(notatki_s), italic=True, color=GREY, size=9)

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
                 gender: str = "patient", test_mode: bool = False) -> dict:
    """
    Główna funkcja modułu.

    Równoległość DeepSeek:
      Runda 1 (niezależne): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
      Runda 2 (równolegle): #3a t1 dni 1-4 | #3b t1 dni 5-7
                            #4a t2 dni 8-11 | #4b t2 dni 12-14 | #5 wypis
                            (chunki w tygodniach sekwencyjnie wewnątrz wątku,
                             ale oba tygodnie + wypis równolegle między sobą)
      Runda 3: #7a zalecenia+rokowanie | #7b notatki_pielegniarek | #7c notatki_sprzataczki+incydenty
               (trzy wywołania równolegle)
      Potem: DeepSeek tone + completeness (sekwencyjnie)
      Potem: FLUX oba zdjęcia równolegle
      Potem: DOCX
    """
    from flask import current_app as flask_app

    current_app.logger.info("[psych-raport] START build_raport")
    app_obj = flask_app._get_current_object()
    cfg = _load_cfg()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 1 — niezależne sekcje DeepSeek równolegle
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
            return _sekcja_flux_prompty(cfg, body, nouns_dict, sender_name, gender, test_mode=test_mode)

    sekcja_pacjent  = {}
    sekcja_dep_leki = {}
    sekcja_diagnozy = {}
    sekcja_flux     = {}

    for name, fn in [
        ("pacjent", _r1_pacjent),
        ("depozyt", _r1_depozyt),
        ("diagnozy", _r1_diagnozy),
        ("flux", _r1_flux),
    ]:
        try:
            result = fn()
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

    # ══════════════════════════════════════════════════════════════════════════
    # RUNDA 2 — tygodnie + wypis równolegle
    # (wewnątrz każdego tygodnia chunki są sekwencyjne, żeby chunk B
    #  mógł dostać listę motywów z chunk A)
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

    for name, fn in [
        ("tydzien1", _r2_tydzien1),
        ("tydzien2", _r2_tydzien2),
        ("wypis", _r2_wypis),
    ]:
        try:
            result = fn()
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
    # RUNDA 3 — zalecenia/notatki/incydenty (trzy wywołania równolegle)
    # ══════════════════════════════════════════════════════════════════════════
    def _r3_zalecenia():
        with app_obj.app_context():
            return _sekcja_zalecenia(cfg, body, dni_1_7, dni_8_14)

    try:
        sekcja_zalecenia = _r3_zalecenia()
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

    # ── Relacje świadków ─────────────────────────────────────────────────────
    try:
        relacje_result = _sekcja_relacje_swiadkow(cfg, body, raport)
        if relacje_result and "relacje_swiadkow" in relacje_result:
            raport["relacje_swiadkow"] = relacje_result["relacje_swiadkow"]
            current_app.logger.info("[psych-raport] Relacje świadków OK (%d relacji)",
                                    len(relacje_result["relacje_swiadkow"]))
        else:
            raport["relacje_swiadkow"] = []
            current_app.logger.warning("[psych-raport] Brak relacji świadków")
    except Exception as e:
        current_app.logger.error("[psych-raport] Błąd generowania relacji świadków: %s", e)
        raport["relacje_swiadkow"] = []

    # ── FLUX — oba zdjęcia równolegle ─────────────────────────────────────────
    prompt_pacjent    = sekcja_flux.get("prompt_pacjent", "")
    prompt_przedmioty = sekcja_flux.get("prompt_przedmioty", "")
    photo_1, photo_2  = _generate_photos_parallel(prompt_pacjent, prompt_przedmioty, test_mode=test_mode)
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
