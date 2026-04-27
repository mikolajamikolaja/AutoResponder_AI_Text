"""
responders/zwykly_psychiatryczny_raport.py

Moduł obsługujący CAŁY pipeline raportu psychiatrycznego:
  - 10 wywołań DeepSeek (każda sekcja osobno)
      Runda 1 (równolegle): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
      Runda 2 (równolegle): #3 dni 1,3 | #4 dni 14,30 | #5 wypis
      Runda 3: #7a zalecenia+rokowanie | #7b notatki_pielegniarek (3) | #7c notatki_sprzataczki (3)+incydenty (2)
  - 2 wywołania DeepSeek (tone check + completeness check, merge JSON)
  - 2 zdjęcia FLUX generowane sekwencyjnie
  - Budowanie DOCX (python-docx)
  - Zwraca dict: {raport_pdf, psych_photo_1, psych_photo_2}

ZMIANY v6:
  - Hospitalizacja skrócona: tylko dni 1, 3, 14, 30
  - Zalecenia: 1 zadanie (nie 3)
  - Incydenty: 2 (nie 10)
  - Notatki pielęgniarek: 3 (nie 10)
  - Notatki sprzątaczek: 3 (nie 10)
  - Klucze groq_* → deepseek_* wszędzie
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
)
from core.logging_reporter import get_logger
from core.hf_token_manager import get_active_tokens, mark_dead, is_dead

# ─────────────────────────────────────────────────────────────────────────────
# ŚCIEŻKI
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
RAPORT_JSON = os.path.join(PROMPTS_DIR, "zwykly_raport.json")
SUBSTITUTE_IMAGE_PATH = os.path.join(BASE_DIR, "images", "zastepczy.jpg")


def _strip_json_text(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text, flags=re.M)
    text = re.sub(r"```\s*$", "", text, flags=re.M)
    return text.strip()


def _fix_unicode_escapes(raw: str) -> str:
    """Naprawia nieprawidłowe sekwencje escape Unicode w JSON."""
    # Usuwa niekompletne \uXXXX (mniej niż 4 cyfry hex)
    raw = re.sub(r"\\u[0-9a-fA-F]{0,3}(?![0-9a-fA-F])", "", raw)
    # Usuwa pojedyncze backslash na końcu linii (mogą powodować problemy)
    raw = re.sub(r"\\\s*$", "", raw, flags=re.M)
    # Zamienia nieprawidłowe escape sequences na bezpieczne wersje
    raw = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r"\\\\", raw)
    return raw


def _extract_python_dicts(raw: str) -> list | None:
    """
    Wyciąga jeden lub wiele Python dict-ów z surowego tekstu używając
    skanowania nawiasów (bezpieczniejsze niż regex dla zagnieżdżonych cudzysłowów).
    Zwraca listę sparsowanych obiektów lub None jeśli nic nie znaleziono.

    Obsługuje główny przypadek: DeepSeek zwraca N osobnych Python dict-ów
    jeden pod drugim zamiast JSON array.
    """
    results = []
    i = 0
    n = len(raw)
    while i < n:
        # Znajdź następne '{'
        while i < n and raw[i] != '{':
            i += 1
        if i >= n:
            break
        # Skanuj do pasującego '}' z obsługą stringów i zagnieżdżeń
        depth = 0
        in_str = False
        str_char = None
        escape = False
        start = i
        j = i
        while j < n:
            ch = raw[j]
            if escape:
                escape = False
                j += 1
                continue
            if ch == '\\' and in_str:
                escape = True
                j += 1
                continue
            if not in_str:
                if ch in ('"', "'"):
                    in_str = True
                    str_char = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        fragment = raw[start : j + 1]
                        try:
                            import ast as _ast
                            obj = _ast.literal_eval(fragment)
                            if isinstance(obj, dict):
                                results.append(obj)
                        except Exception:
                            pass
                        i = j + 1
                        break
            else:
                if ch == str_char:
                    in_str = False
            j += 1
        else:
            break
    return results if results else None


def _convert_python_dict_to_json(raw: str) -> str:
    """
    Konwertuje odpowiedź DeepSeek w formacie Python dict/dicts na JSON.
    Obsługuje:
      - pojedynczy dict: {'k': 'v'} → {"k": "v"}
      - wiele dict-ów pod sobą: {'k':1}\n{'k':2} → [{"k":1},{"k":2}]
    Heurystyka: jeśli nie ma apostrofów jako delimitatorów stringów — zwraca raw.
    """
    stripped = raw.strip()
    if not stripped:
        return raw

    # Szybki test: musi zaczynać się od { lub [
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return raw

    # Jeśli to już poprawny JSON — nie ruszaj
    try:
        json.loads(stripped)
        return raw
    except json.JSONDecodeError:
        pass

    # Heurystyka: Python dict używa apostrofów jako delimitatorów kluczy
    # Szukamy wzorca ': ' lub "': " który wskazuje na Python dict
    has_python_style = "': " in raw or "',\n" in raw or "': \"" in raw
    if not has_python_style:
        return raw

    # Wyciągnij wszystkie Python dict-y ze skanowania nawiasów
    dicts = _extract_python_dicts(stripped)
    if not dicts:
        return raw

    if len(dicts) == 1:
        return json.dumps(dicts[0], ensure_ascii=False)
    else:
        # Wiele dict-ów → tablica JSON
        return json.dumps(dicts, ensure_ascii=False)


def _repair_leading_comma(raw: str) -> str:
    """
    Naprawia odpowiedzi DeepSeek zaczynające się od przecinka:
    ',\n  "klucz": "wartość"\n}' → '{\n  "klucz": "wartość"\n}'
    Wzorzec: model zwraca fragment JSON bez otwierającego '{' — zaczyna od ','
    """
    stripped = raw.strip()
    if not stripped.startswith(","):
        return raw
    # Usuń wiodący przecinek i ewentualne białe znaki po nim
    content = stripped.lstrip(",").strip()
    # Dodaj brakujące '{' na początku
    if not content.startswith("{"):
        content = "{" + content
    # Upewnij się że jest zamknięcie '}'
    if not content.endswith("}"):
        content = content + "}"
    current_app.logger.warning(
        "[psych-raport] _repair_leading_comma: naprawiono fragment JSON zaczynający się od ','"
    )
    return content


def _normalize_json_text(raw: str) -> str:
    raw = raw.replace("\r\n", "\n")
    # Napraw Python dict (apostrofy) zanim cokolwiek innego
    raw = _convert_python_dict_to_json(raw)
    # Napraw fragment zaczynający się od przecinka (brak '{')
    raw = _repair_leading_comma(raw)
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    raw = _fix_unicode_escapes(raw)
    return raw.strip()


_JSON_FORCE_SUFFIX = '\n\nOdpowiedź TYLKO w formacie JSON. Pierwszym znakiem MUSI być {. Ostatnim znakiem MUSI być }. Absolutny zakaz jakiegokolwiek tekstu, prozy, wyjaśnień ani komentarzy przed { lub po }. Zacznij od { :'
_JSON_FORCE_SYSTEM = (
    'KRYTYCZNE: Twoja odpowiedź to WYŁĄCZNIE czysty JSON. '
    'Pierwszym znakiem odpowiedzi MUSI być {. Ostatnim znakiem MUSI być }. '
    'Absolutny zakaz pisania czegokolwiek przed { — żadnej prozy, żadnych wyjaśnień, żadnego "Oto JSON:", żadnych komentarzy. '
    'Absolutny zakaz pisania czegokolwiek po } — żadnych podsumowań, żadnych uwag. '
    'Cała odpowiedź = jeden obiekt JSON od { do }. Nic poza tym.'
)


def _u(user_prompt: str) -> str:
    """Wymusza start odpowiedzi od '{' — zapobiega prozie i fragmentom JSON."""
    return user_prompt + _JSON_FORCE_SUFFIX


def _s(system_prompt: str) -> str:
    """Dodaje wymóg startu od '{' do system promptu."""
    if not system_prompt:
        return _JSON_FORCE_SYSTEM
    return system_prompt + '\n' + _JSON_FORCE_SYSTEM


def _wrap_section_list(section: str, data: object) -> object:
    if not isinstance(data, list):
        return data
    if section == "zalecenia_7b":
        return {"notatki_pielegniarek": data}
    if section == "zalecenia_7c":
        if (
            data
            and isinstance(data[0], dict)
            and data[0].get("data")
            and data[0].get("tresc")
        ):
            return {"notatki_sprzataczki": data}
        return {"incydenty_specjalne": data}
    return data


def _extract_best_json(raw: str) -> tuple[object | None, str | None]:
    decoder = json.JSONDecoder()
    best_obj = None
    best_text = None
    best_len = 0
    for match in re.finditer(r"[\[{]", raw):
        start = match.start()
        try:
            obj, end = decoder.raw_decode(raw[start:])
            if end > best_len:
                best_len = end
                best_obj = obj
                best_text = raw[start : start + end]
        except json.JSONDecodeError:
            continue
    return best_obj, best_text


def _repair_truncated_json(raw: str) -> str:
    raw = raw.strip()
    # Napraw fragment zaczynający się od przecinka (brak '{')
    raw = _repair_leading_comma(raw)
    # Napraw Python dict (apostrofy) przed dalszą naprawą
    raw = _convert_python_dict_to_json(raw)
    start = next((i for i, c in enumerate(raw) if c in "{["), 0)
    raw = raw[start:]
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string

    if in_string:
        raw += '"'

    raw = re.sub(r",\s*$", "", raw)

    stack = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()

    if stack:
        raw += "".join(reversed(stack))
    return raw


# Wymagane klucze dla każdej sekcji — jeśli brakuje po naprawie, logujemy WARNING
_REQUIRED_KEYS: dict[str, list[str]] = {
    "pacjent":               ["dane_pacjenta", "powod_przyjecia"],
    "diagnozy":              ["diagnoza_wstepna", "objawy"],
    "wypis":                 ["wypis"],
    "zalecenia_7a":          ["zalecenia_tylera", "rokowanie"],
    "zalecenia_7b":          ["notatki_pielegniarek"],
    "zalecenia_7c":          ["notatki_sprzataczki", "incydenty_specjalne"],
    "deepseek_completeness": [],  # merge dict — nie walidujemy
}


def _check_required_keys(result: object, section: str) -> None:
    """Loguje WARNING jeśli w dict brakuje wymaganych kluczy dla danej sekcji."""
    required = _REQUIRED_KEYS.get(section, [])
    if not required or not isinstance(result, dict):
        return
    missing = [k for k in required if k not in result]
    if missing:
        current_app.logger.warning(
            "[psych-raport] JSON brakujące pola sekcja=%s: %s",
            section,
            missing,
        )


def _parse_json_safe(raw: str, section: str) -> dict | list | None:
    if not raw:
        return None

    clean = _normalize_json_text(_strip_json_text(raw))
    if not clean:
        return None

    try:
        result = json.loads(clean)
        current_app.logger.info("[psych-raport] JSON OK sekcja=%s", section)
        result = _wrap_section_list(section, result)
        _check_required_keys(result, section)
        return result
    except json.JSONDecodeError as e:
        current_app.logger.warning(
            "[psych-raport] JSON błąd sekcja=%s: %s — próba naprawy uciętego JSON",
            section,
            e,
        )
        json_error = str(e)

    extracted, extracted_text = _extract_best_json(clean)
    if extracted is not None:
        current_app.logger.warning(
            "[psych-raport] JSON ekstrakcja sekcja=%s → %s znaków",
            section,
            len(extracted_text or ""),
        )
        result = _wrap_section_list(section, extracted)
        _check_required_keys(result, section)
        return result

    repaired = _repair_truncated_json(clean)
    try:
        result = json.loads(repaired)
        current_app.logger.warning(
            "[psych-raport] JSON naprawiony sekcja=%s (ucięty output)",
            section,
        )
        result = _wrap_section_list(section, result)
        _check_required_keys(result, section)
        return result
    except Exception as repair_e:
        extracted, extracted_text = _extract_best_json(repaired)
        if extracted is not None:
            current_app.logger.warning(
                "[psych-raport] JSON naprawiony po ekstrakcji sekcja=%s → %s znaków",
                section,
                len(extracted_text or ""),
            )
            result = _wrap_section_list(section, extracted)
            _check_required_keys(result, section)
            return result

    raw_len = len(raw) if raw else 0
    current_app.logger.warning(
        "[psych-raport] JSON naprawa nieudana sekcja=%s: %s | raw_len=%d",
        section,
        json_error,
        raw_len,
    )
    chunk_size = 800
    for idx in range(0, raw_len, chunk_size):
        current_app.logger.warning(
            "[psych-raport] raw[%d:%d] sekcja=%s >>> %s",
            idx,
            idx + chunk_size,
            section,
            raw[idx : idx + chunk_size],
        )
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
        current_app.logger.warning(
            "[psych-test] Brak pliku zastępczego: %s", SUBSTITUTE_IMAGE_PATH
        )
        return None
    try:
        with open(SUBSTITUTE_IMAGE_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {
            "base64": b64,
            "content_type": "image/jpeg",
            "filename": "zastepczy.jpg",
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
    """
    Podzielone na 3 osobne wywołania DeepSeek:
      1a — dane_pacjenta + numer_historii + data_przyjecia  (max_tokens: 1050)
      1b — powod_przyjecia                                   (max_tokens: 1800)
      1c — cytaty_z_przyjecia                               (max_tokens: 3750)
    """
    sec = cfg.get("deepseek_1_pacjent", {})
    system = sec.get("system", "")
    email_fragment = body[:MAX_DLUGOSC_EMAIL]

    # ── Call 1a — dane pacjenta + numer + data ────────────────────────────────
    sec_1a = cfg.get("deepseek_1a_dane_pacjenta", sec)
    system_1a = sec_1a.get("system", system)
    schema_1a = json.dumps(
        {
            "numer_historii_choroby": sec.get("schema", {}).get(
                "numer_historii_choroby", "Losowy numer NY-2026-XXXXX"
            ),
            "data_przyjecia": sec.get("schema", {}).get(
                "data_przyjecia", "Data z emaila lub dzisiejsza DD.MM.YYYY"
            ),
            "dane_pacjenta": sec.get("schema", {}).get("dane_pacjenta", {}),
        },
        ensure_ascii=False,
        indent=2,
    )
    user_1a = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SENDER_NAME (priorytet dla imienia): {sender_name or '(brak)'}\n\n"
        f"SCHEMAT JSON do wypełnienia (TYLKO te klucze):\n{schema_1a}\n\n"
        f"KRYTYCZNE: klucz 'dane_pacjenta' MUSI być zagnieżdżonym obiektem — "
        f"ZAKAZ zwracania pól imie_nazwisko/wiek/adres na górnym poziomie JSON.\n"
        f"LIMIT: każde pole max 300 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_1a = call_deepseek(_s(system_1a), _u(user_1a), MODEL_TYLER, max_tokens=1050)
    result_1a = _parse_json_safe(raw_1a, "pacjent_1a_dane")
    if not isinstance(result_1a, dict):
        result_1a = {}
    # Jeśli dane_pacjenta wróciły płaskie — zawiń je
    if "imie_nazwisko" in result_1a and "dane_pacjenta" not in result_1a:
        flat_keys = ["imie_nazwisko", "rodowod", "wiek", "adres", "zawod",
                     "stan_cywilny", "numer_ubezpieczenia"]
        result_1a["dane_pacjenta"] = {k: result_1a.pop(k) for k in flat_keys if k in result_1a}
    current_app.logger.info(
        "[psych-raport] 1a dane_pacjenta OK: %s",
        bool(result_1a.get("dane_pacjenta")),
    )

    # ── Call 1b — powód przyjęcia ─────────────────────────────────────────────
    sec_1b = cfg.get("deepseek_1b_powod_przyjecia", sec)
    system_1b = sec_1b.get("system", system)
    schema_1b = json.dumps(
        {
            "powod_przyjecia": sec.get("schema", {}).get(
                "powod_przyjecia",
                "MINIMUM 15 zdań. KAŻDE zdanie nawiązuje do konkretnego słowa z emaila.",
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_1b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SENDER_NAME: {sender_name or '(brak)'}\n\n"
        f"SCHEMAT JSON (TYLKO klucz powod_przyjecia):\n{schema_1b}\n\n"
        f"powod_przyjecia: MINIMUM 15 zdań. KAŻDE zdanie nawiązuje do konkretnego słowa z emaila. "
        f"Formalna dokumentacja medyczna z biurokratyczną powagą wobec nonsensu. Nihilizm w podtekście.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_1b = call_deepseek(_s(system_1b), _u(user_1b), MODEL_TYLER, max_tokens=1800)
    result_1b = _parse_json_safe(raw_1b, "pacjent_1b_powod")
    if not isinstance(result_1b, dict):
        result_1b = {}
    current_app.logger.info(
        "[psych-raport] 1b powod_przyjecia OK: %s",
        bool(result_1b.get("powod_przyjecia")),
    )

    # ── Call 1c — cytaty z przyjęcia ──────────────────────────────────────────
    sec_1c = cfg.get("deepseek_1c_cytaty", sec)
    system_1c = sec_1c.get("system", system)
    schema_1c = json.dumps(
        {
            "cytaty_z_przyjecia": sec.get("schema", {}).get(
                "cytaty_z_przyjecia",
                "Lista MINIMUM 4 cytatów. Każdy = PARAFRAZA REALNEGO zdania z emaila + komentarz min. 15 zdań.",
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_1c = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SENDER_NAME: {sender_name or '(brak)'}\n\n"
        f"SCHEMAT JSON (TYLKO klucz cytaty_z_przyjecia):\n{schema_1c}\n\n"
        f"cytaty_z_przyjecia: DOKŁADNIE 4 obiekty. Każdy obiekt: "
        f"'cytat' = PARAFRAZA REALNEGO zdania z emaila (ZAKAZ cytatu którego nadawca nie powiedział), "
        f"'komentarz' = nota kliniczna MINIMUM 15 zdań nawiązująca do emaila.\n"
        f"ABSOLUTNY ZAKAZ w komentarzu: Szwejk, Monty Python, Tyler Durden, Fight Club, "
        f"kafkowski, absurdystyczny. Komentarz MUSI brzmieć jak autentyczna nota psychiatryczna.\n"
        f"LIMIT: każdy komentarz max 400 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_1c = call_deepseek(_s(system_1c), _u(user_1c), MODEL_TYLER, max_tokens=3750)
    result_1c = _parse_json_safe(raw_1c, "pacjent_1c_cytaty")
    if not isinstance(result_1c, dict):
        result_1c = {}
    current_app.logger.info(
        "[psych-raport] 1c cytaty_z_przyjecia OK: %s",
        bool(result_1c.get("cytaty_z_przyjecia")),
    )

    # ── Scal wyniki 3 callów ──────────────────────────────────────────────────
    result = {}
    result.update(result_1a)
    result.update(result_1b)
    result.update(result_1c)

    if not result.get("dane_pacjenta"):
        fb = cfg.get("fallback_dane_pacjenta", {})
        current_app.logger.warning(
            "[psych-raport] sekcja_pacjent → fallback (brak dane_pacjenta)"
        )
        result.update(fb)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #2 — depozyt + leki
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_depozyt_leki(cfg: dict, body: str, nouns_dict: dict) -> dict:
    """
    Podzielone na 2 osobne wywołania DeepSeek:
      2a — depozyt przedmiotów          (max_tokens: 1200)
      2b — farmakologia (leki)          (max_tokens: 1350)
    """
    sec = cfg.get("deepseek_2_depozyt_leki", {})
    system = sec.get("system", "")
    email_fragment = body[:MAX_DLUGOSC_EMAIL]
    nouns_str = ", ".join(nouns_dict.values()) if nouns_dict else "(brak rzeczowników)"

    # ── Call 2a — depozyt ─────────────────────────────────────────────────────
    sec_2a = cfg.get("deepseek_2a_depozyt", sec)
    system_2a = sec_2a.get("system", system)
    schema_2a = json.dumps(
        {"depozyt": sec.get("schema", {}).get("depozyt", {})},
        ensure_ascii=False,
        indent=2,
    )
    user_2a = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"RZECZOWNIKI Z EMAILA (TYLKO fizycznie unoszalne): {nouns_str}\n\n"
        f"SCHEMAT JSON (TYLKO klucz depozyt):\n{schema_2a}\n\n"
        f"ZASADY: tylko fizycznie unoszalne przedmioty lub ich miniatury. "
        f"Każdy z żartobliwą cechą udziwnioną. Posiadanie opisz jako działające na niekorzyść pacjenta.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_2a = call_deepseek(_s(system_2a), _u(user_2a), MODEL_TYLER, max_tokens=1200)
    result_2a = _parse_json_safe(raw_2a, "depozyt_2a")
    if not isinstance(result_2a, dict):
        result_2a = {}
    current_app.logger.info(
        "[psych-raport] 2a depozyt OK: %s", bool(result_2a.get("depozyt"))
    )

    # ── Call 2b — farmakologia ────────────────────────────────────────────────
    sec_2b = cfg.get("deepseek_2b_farmakologia", sec)
    system_2b = sec_2b.get("system", system)
    schema_2b = json.dumps(
        {"farmakologia": sec.get("schema", {}).get("farmakologia", {})},
        ensure_ascii=False,
        indent=2,
    )
    user_2b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"RZECZOWNIKI Z EMAILA (każdy musi mieć swój lek): {nouns_str}\n\n"
        f"SCHEMAT JSON (TYLKO klucz farmakologia):\n{schema_2b}\n\n"
        f"ZASADY: JEDEN lek per rzeczownik. Nazwa leku nawiązuje do rzeczownika. "
        f"Dawkowanie ZAWSZE żartobliwe nawiązujące do emaila — NIGDY '1x dziennie'.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_2b = call_deepseek(_s(system_2b), _u(user_2b), MODEL_TYLER, max_tokens=1350)
    result_2b = _parse_json_safe(raw_2b, "depozyt_2b_farmakologia")
    if not isinstance(result_2b, dict):
        result_2b = {}
    current_app.logger.info(
        "[psych-raport] 2b farmakologia OK: %s", bool(result_2b.get("farmakologia"))
    )

    result = {}
    result.update(result_2a)
    result.update(result_2b)

    if not result or not isinstance(result, dict):
        return {
            "depozyt": {
                "lista_przedmiotow": (
                    list(nouns_dict.values()) if nouns_dict else ["przedmiot nieznany"]
                ),
                "protokol_depozytu": "Odebrano przedmioty niebezpieczne. Pacjent protestował.",
            },
            "farmakologia": {
                "leki": [
                    {
                        "nazwa": "Nihilizyna 500mg",
                        "rzeczownik_zrodlowy": "email",
                        "wskazanie": "nadmierny optymizm",
                        "dawkowanie": "2x dziennie po każdej nadziei",
                    }
                ],
                "nota_farmaceutyczna": "Farmakoterapia wdrożona. Rokowanie: złe.",
            },
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #3/#4 — tygodnie hospitalizacji (CHUNKI po 3-4 dni)
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_tydzien(
    cfg: dict, body: str, leki: list, tydzien: int, data_przyjecia: str
) -> list:
    """
    Generuje 2 wybrane dni hospitalizacji (skrócona wersja):
      tydzien=1 → dni 1 i 3
      tydzien=2 → dni 14 i 30
    Jeden call DeepSeek na oba dni naraz → mały JSON, brak ucięć.
    """
    sec_key = f"deepseek_{2 + tydzien}_tydzien{tydzien}"
    sec = cfg.get(sec_key, {})
    system = sec.get("system", "")

    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
    except Exception:
        base_date = datetime.now()

    if tydzien == 1:
        numery_dni = [1, 3]
    else:
        numery_dni = [14, 30]

    daty = {
        d: (base_date + timedelta(days=d - 1)).strftime("%d.%m.%Y") for d in numery_dni
    }
    daty_str = "\n".join(f"Dzień {d}: {dt}" for d, dt in daty.items())
    leki_str = json.dumps(leki, ensure_ascii=False, indent=2) if leki else "[]"

    schema_days = [
        {
            "dzien": d,
            "data": daty[d],
            "zdarzenie": "Min. 4-5 zdań. Formalna biurokratyczna powaga wobec absurdalnej sytuacji. Nawiązuje do emaila. Brak → '__BRAK__'",
            "lek": "Nazwa leku + dawka lub '__BRAK__'",
            "stan_pacjenta": "Jedno zdanie nihilistyczne lub '__BRAK__'",
            "nota_lekarska": "2-3 zdania lub '__BRAK__'",
        }
        for d in numery_dni
    ]

    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"LISTA LEKÓW DO UŻYCIA:\n{leki_str}\n\n"
        f"DATY — użyj DOKŁADNIE tych dat:\n{daty_str}\n\n"
        f"SCHEMAT JSON (wygeneruj tablicę 2 obiektów):\n"
        f"{json.dumps(schema_days, ensure_ascii=False, indent=2)}\n\n"
        f"Zwróć TYLKO czysty JSON z tablicą 2 obiektów."
    )

    section_name = f"tydzien{tydzien}_dni{'_'.join(str(d) for d in numery_dni)}"
    raw = call_deepseek(system, _u(user), MODEL_TYLER, max_tokens=2250)
    result = _parse_json_safe(raw, section_name)

    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list) and len(v) > 0:
                return v

    current_app.logger.warning("[psych-raport] %s → fallback", section_name)
    return [
        {
            "dzien": d,
            "data": daty[d],
            "zdarzenie": "__BRAK__",
            "lek": "__BRAK__",
            "stan_pacjenta": "__BRAK__",
            "nota_lekarska": "__BRAK__",
        }
        for d in numery_dni
    ]


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #5 — wypis (dzień 15)
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_wypis(cfg: dict, body: str, data_przyjecia: str) -> dict:
    """
    Podzielone na 2 osobne wywołania DeepSeek:
      5a — stan_przy_wypisie + powod_wypisu + dzien_wypisu   (max_tokens: 1800)
      5b — zalecenia_po_wypisie + opis_pozegnania             (max_tokens: 1800)
    """
    sec = cfg.get("deepseek_5_wypis", {})
    system = sec.get("system", "")
    email_fragment = body[:MAX_DLUGOSC_EMAIL]

    try:
        base_date = datetime.strptime(data_przyjecia, "%d.%m.%Y")
        data_wypisu = (base_date + timedelta(days=14)).strftime("%d.%m.%Y")
    except Exception:
        data_wypisu = datetime.now().strftime("%d.%m.%Y")

    wypis_schema = sec.get("schema", {}).get("wypis", {})

    # ── Call 5a — stan + powód + dzień ───────────────────────────────────────
    schema_5a = json.dumps(
        {
            "wypis": {
                "dzien_wypisu": wypis_schema.get("dzien_wypisu", f"Dzień 15, {data_wypisu}"),
                "stan_przy_wypisie": wypis_schema.get("stan_przy_wypisie", "4 zdania nawiązujące do emaila"),
                "powod_wypisu": wypis_schema.get("powod_wypisu", "2-3 zdania nawiązujące do emaila"),
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    user_5a = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"DATA WYPISU (dzień 15): {data_wypisu}\n\n"
        f"SCHEMAT JSON (TYLKO klucze w wypis: dzien_wypisu, stan_przy_wypisie, powod_wypisu):\n{schema_5a}\n\n"
        f"stan_przy_wypisie: 4 zdania. Absurdalna poprawa lub pogłębienie nihilizmu. Nawiązuje do emaila.\n"
        f"powod_wypisu: 2-3 zdania oficjalnego uzasadnienia wypisu nawiązującego do emaila.\n"
        f"LIMIT: każde pole max 300 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_5a = call_deepseek(system, _u(user_5a), MODEL_TYLER, max_tokens=1800)
    result_5a = _parse_json_safe(raw_5a, "wypis_5a")
    if not isinstance(result_5a, dict):
        result_5a = {}
    current_app.logger.info(
        "[psych-raport] 5a wypis stan OK: %s",
        bool(result_5a.get("wypis", {}).get("stan_przy_wypisie")),
    )

    # ── Call 5b — zalecenia + pożegnanie ─────────────────────────────────────
    schema_5b = json.dumps(
        {
            "wypis": {
                "zalecenia_po_wypisie": wypis_schema.get(
                    "zalecenia_po_wypisie",
                    "Lista 5-7 zaleceń absurdalnych nawiązujących do emaila",
                ),
                "opis_pozegnania": wypis_schema.get(
                    "opis_pozegnania",
                    "2-3 zdania opisu pożegnania pacjenta. Absurdalne. Nawiązuje do emaila.",
                ),
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    user_5b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SCHEMAT JSON (TYLKO klucze w wypis: zalecenia_po_wypisie, opis_pozegnania):\n{schema_5b}\n\n"
        f"zalecenia_po_wypisie: lista 5-7 konkretnych zaleceń absurdalnych nawiązujących do emaila. "
        f"Każde zalecenie to jedno zdanie — brak ogólników.\n"
        f"opis_pozegnania: 2-3 zdania opisujące jak pacjent wyszedł — absurdalnie, nawiązuje do emaila.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_5b = call_deepseek(system, _u(user_5b), MODEL_TYLER, max_tokens=1800)
    result_5b = _parse_json_safe(raw_5b, "wypis_5b")
    if not isinstance(result_5b, dict):
        result_5b = {}
    current_app.logger.info(
        "[psych-raport] 5b wypis zalecenia OK: %s",
        bool(result_5b.get("wypis", {}).get("zalecenia_po_wypisie")),
    )

    # ── Scal wypis z obu callów ───────────────────────────────────────────────
    wypis_merged = {}
    wypis_merged.update(result_5a.get("wypis", {}))
    wypis_merged.update(result_5b.get("wypis", {}))

    if wypis_merged:
        return {"wypis": wypis_merged}

    return {
        "wypis": {
            "dzien_wypisu": f"Dzień 15, {data_wypisu}",
            "stan_przy_wypisie": "Pacjent osiągnął akceptowalny poziom beznadziei.",
            "powod_wypisu": "Wyczerpanie budżetu nadziei.",
            "zalecenia_po_wypisie": [
                "Unikać optymizmu.",
                "Nie planować remontów.",
                "Nie pisać emaili.",
            ],
            "opis_pozegnania": "Pacjent wyszedł bez słowa. Drzwi zostawił otwarte.",
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #6 — łacińskie diagnozy
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_diagnozy(cfg: dict, body: str, previous_body: str) -> dict:
    """
    Podzielone na 2 osobne wywołania DeepSeek:
      6a — diagnoza_wstepna + diagnoza_dodatkowa   (max_tokens: 1800)
      6b — objawy + choroba_wspolistniejaca        (max_tokens: 1500)
    """
    sec = cfg.get("deepseek_6_diagnozy_lacina", {})
    system = sec.get("system", "")
    email_fragment = body[:MAX_DLUGOSC_EMAIL]
    prev_fragment = previous_body[:1000] if previous_body else ""

    # ── Call 6a — diagnozy wstępna + dodatkowa ────────────────────────────────
    sec_6a = cfg.get("deepseek_6a_diagnozy_glowne", sec)
    system_6a = sec_6a.get("system", system)
    schema_6a = json.dumps(
        {
            "diagnoza_wstepna": sec.get("schema", {}).get(
                "diagnoza_wstepna",
                {"nazwa_lacinska": "...", "nazwa_polska": "...", "kod_dsm": "...", "opis_kliniczny": "..."},
            ),
            "diagnoza_dodatkowa": sec.get("schema", {}).get(
                "diagnoza_dodatkowa",
                {"nazwa_lacinska": "...", "nazwa_polska": "...", "kod_dsm": "...", "opis_kliniczny": "..."},
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
    user_6a = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        + (f"POPRZEDNI EMAIL (dla diagnozy_dodatkowej):\n{prev_fragment}\n\n" if prev_fragment else "")
        + f"SCHEMAT JSON (TYLKO klucze diagnoza_wstepna i diagnoza_dodatkowa):\n{schema_6a}\n\n"
        f"Diagnozy: łacińskie nazwy absurdalne, nawiązujące do emaila. Styl Szwejka.\n"
        f"LIMIT: każde pole opis_kliniczny max 300 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_6a = call_deepseek(_s(system_6a), _u(user_6a), MODEL_TYLER, max_tokens=1800)
    result_6a = _parse_json_safe(raw_6a, "diagnozy_6a")
    if not isinstance(result_6a, dict):
        result_6a = {}
    current_app.logger.info(
        "[psych-raport] 6a diagnozy_wstepna OK: %s", bool(result_6a.get("diagnoza_wstepna"))
    )

    # ── Call 6b — objawy + choroba_wspolistniejaca ────────────────────────────
    sec_6b = cfg.get("deepseek_6b_objawy", sec)
    system_6b = sec_6b.get("system", system)
    schema_6b = json.dumps(
        {
            "objawy": sec.get("schema", {}).get(
                "objawy", "Lista 5-8 objawów klinicznych nawiązujących do emaila"
            ),
            "choroba_wspolistniejaca": sec.get("schema", {}).get(
                "choroba_wspolistniejaca",
                "Absurdalna choroba współistniejąca nawiązująca do emaila lub '__BRAK__'",
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
    user_6b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SCHEMAT JSON (TYLKO klucze objawy i choroba_wspolistniejaca):\n{schema_6b}\n\n"
        f"objawy: lista 5-8 konkretnych objawów klinicznych nawiązujących do słów z emaila. "
        f"Styl: formalna diagnostyka psychiatryczna + absurd Monty Pythona.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_6b = call_deepseek(_s(system_6b), _u(user_6b), MODEL_TYLER, max_tokens=1500)
    result_6b = _parse_json_safe(raw_6b, "diagnozy_6b_objawy")
    if not isinstance(result_6b, dict):
        result_6b = {}
    current_app.logger.info(
        "[psych-raport] 6b objawy OK: %s", bool(result_6b.get("objawy"))
    )

    result = {}
    result.update(result_6a)
    result.update(result_6b)

    if not result or not result.get("diagnoza_wstepna"):
        return {
            "diagnoza_wstepna": {
                "nazwa_lacinska": "Syndroma Emaili Desperati",
                "nazwa_polska": "Desperackie Emailowanie",
                "kod_dsm": "DSM-TD-2026-001",
                "opis_kliniczny": "Przewlekłe wysyłanie emaili z objawami nadziei.",
            },
            "diagnoza_dodatkowa": {
                "nazwa_lacinska": "Morbus Optimismus Pathologicus",
                "nazwa_polska": "Patologiczny Optymizm",
                "kod_dsm": "DSM-TD-2026-002",
                "opis_kliniczny": "Choroba współistniejąca. Brak rokowań.",
            },
            "objawy": [
                "Nadmierny optymizm epistolarny",
                "Fiksacja na punkcie nierealnych planów",
                "Urojenia poprawy sytuacji",
            ],
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #7 — zalecenia + notatki + rokowanie (TRZY OSOBNE WYWOŁANIA)
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_zalecenia(cfg: dict, body: str, dni_1_7: list, dni_8_14: list) -> dict:
    """
    Podzielone na PIĘĆ osobnych wywołań DeepSeek:
      deepseek_7a_zalecenia  — zalecenia_tylera             (max_tokens: 1500)
      deepseek_7a_rokowanie  — rokowanie                    (max_tokens: 1200)
      deepseek_7b            — notatki_pielegniarek         (max_tokens: 2700)
      deepseek_7c_sprzataczka — notatki_sprzataczki        (max_tokens: 1500)
      deepseek_7c_incydenty  — incydenty_specjalne          (max_tokens: 1500)
    Wyniki scalane w jeden dict.
    """
    sec_7 = cfg.get("deepseek_7_zalecenia_notatki", {})
    system_7 = sec_7.get("system", "")

    # Kontekst zdarzeń z hospitalizacji
    zdarzenia = []
    for d in (dni_1_7 or []) + (dni_8_14 or []):
        if isinstance(d, dict) and d.get("zdarzenie") not in (None, "", "__BRAK__"):
            zdarzenia.append(
                f"Dzień {d.get('dzien', '?')}: {str(d['zdarzenie'])[:120]}"
            )
    zdarzenia_str = "\n".join(zdarzenia[:14]) if zdarzenia else "(brak)"
    email_fragment = body[:MAX_DLUGOSC_EMAIL]

    # ── deepseek_7a_zalecenia — TYLKO zalecenia_tylera ───────────────────────
    schema_7a_zal = json.dumps(
        {
            "zalecenia_tylera": sec_7.get("schema", {}).get(
                "zalecenia_tylera",
                {
                    "naglowek": "RACHUNEK ZA WYZWOLENIE — ZADANIE OBOWIĄZKOWE",
                    "zadanie_1": "Min. 5-6 zdań. Konkretny przedmiot/plan/rzecz z emaila.",
                    "podpis": "Tyler Durden",
                },
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_7a_zal = (
        f"EMAIL PACJENTA (każde pole MUSI nawiązywać do treści emaila):\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (TYLKO klucz zalecenia_tylera):\n{schema_7a_zal}\n\n"
        f"zalecenia_tylera.zadanie_1: min. 5-6 zdań, konkretny przedmiot/plan/rzecz z emaila.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_7a_zal = call_deepseek(_s(system_7), _u(user_7a_zal), MODEL_TYLER, max_tokens=1500)
    result_7a_zal = _parse_json_safe(raw_7a_zal, "zalecenia_7a_zalecenia")
    if not isinstance(result_7a_zal, dict):
        result_7a_zal = {}
    current_app.logger.info(
        "[psych-raport] 7a_zalecenia OK: %s", bool(result_7a_zal.get("zalecenia_tylera"))
    )

    # ── deepseek_7a_rokowanie — TYLKO rokowanie ───────────────────────────────
    schema_7a_rok = json.dumps(
        {
            "rokowanie": sec_7.get("schema", {}).get(
                "rokowanie", "Min. 5-6 zdań. Bezlitosne. Nawiązuje do emaila."
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_7a_rok = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SCHEMAT JSON (TYLKO klucz rokowanie):\n{schema_7a_rok}\n\n"
        f"rokowanie: min. 5-6 zdań, bezlitosne, każde zdanie nawiązuje do konkretnego słowa z emaila. "
        f"Styl: formalny raport psychiatryczny + nihilizm Tylera Durdena.\n"
        f"Zwróć TYLKO czysty JSON."
    )
    raw_7a_rok = call_deepseek(_s(system_7), _u(user_7a_rok), MODEL_TYLER, max_tokens=1200)
    result_7a_rok = _parse_json_safe(raw_7a_rok, "zalecenia_7a_rokowanie")
    if not isinstance(result_7a_rok, dict):
        result_7a_rok = {}
    current_app.logger.info(
        "[psych-raport] 7a_rokowanie OK: %s", bool(result_7a_rok.get("rokowanie"))
    )

    # ── deepseek_7b — NOTATKI PIELĘGNIAREK ───────────────────────────────────
    schema_7b = json.dumps(
        {
            "notatki_pielegniarek": sec_7.get("schema", {}).get(
                "notatki_pielegniarek",
                "Lista MINIMUM 3 obiektów: {imie_pielegniarki, data, tresc}",
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_7b = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (TYLKO klucz notatki_pielegniarek):\n{schema_7b}\n\n"
        f"notatki_pielegniarek: DOKŁADNIE 3 obiekty. Każdy: imie_pielegniarki, data (DD.MM.YYYY), "
        f"tresc (3-4 zdania gwarą polską — śląska/mazurska/podlaska mieszanka, ciepła dosadna kobieta ze wsi, "
        f"nawiązuje do KONKRETNYCH zachowań pacjenta z emaila i zdarzenia z hospitalizacji).\n"
        f"KAŻDA notatka nawiązuje do INNEGO zdarzenia i INNEGO dnia.\n"
        f"LIMIT: pole tresc max 400 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_7b = call_deepseek(_s(system_7), _u(user_7b), MODEL_TYLER, max_tokens=2700)
    result_7b = _parse_json_safe(raw_7b, "zalecenia_7b")
    if not isinstance(result_7b, dict):
        result_7b = {}
    current_app.logger.info(
        "[psych-raport] 7b notatki_pielegniarek OK: %s",
        bool(result_7b.get("notatki_pielegniarek")),
    )

    # ── deepseek_7c_sprzataczka — TYLKO notatki_sprzataczki ──────────────────
    schema_7c_sp = json.dumps(
        {
            "notatki_sprzataczki": sec_7.get("schema", {}).get(
                "notatki_sprzataczki", "Lista 3 obiektów: {data, tresc}"
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_7c_sp = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"SCHEMAT JSON (TYLKO klucz notatki_sprzataczki):\n{schema_7c_sp}\n\n"
        f"notatki_sprzataczki: DOKŁADNIE 3 obiekty. Każdy: data (DD.MM.YYYY), "
        f"tresc (2-3 zdania — co znalazła sprzątając salę, gwarą polską, absurdalny humor biurokratycznej powagi, "
        f"nawiązuje do emaila pacjenta). KAŻDA notatka mówi o INNYM znalezisku.\n"
        f"LIMIT: pole tresc max 400 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_7c_sp = call_deepseek(_s(system_7), _u(user_7c_sp), MODEL_TYLER, max_tokens=1500)
    result_7c_sp = _parse_json_safe(raw_7c_sp, "zalecenia_7c_sprzataczka")
    if not isinstance(result_7c_sp, dict):
        result_7c_sp = {}
    current_app.logger.info(
        "[psych-raport] 7c_sprzataczka OK: %s",
        bool(result_7c_sp.get("notatki_sprzataczki")),
    )

    # ── deepseek_7c_incydenty — TYLKO incydenty_specjalne ────────────────────
    schema_7c_inc = json.dumps(
        {
            "incydenty_specjalne": sec_7.get("schema", {}).get(
                "incydenty_specjalne", "Lista 2 incydentów po 4-5 zdań"
            )
        },
        ensure_ascii=False,
        indent=2,
    )
    user_7c_inc = (
        f"EMAIL PACJENTA:\n{email_fragment}\n\n"
        f"KLUCZOWE ZDARZENIA Z HOSPITALIZACJI:\n{zdarzenia_str}\n\n"
        f"SCHEMAT JSON (TYLKO klucz incydenty_specjalne):\n{schema_7c_inc}\n\n"
        f"incydenty_specjalne: DOKŁADNIE 2 incydenty, każdy 4-5 zdań, NAPRAWDĘ absurdalnych i śmiesznych, "
        f"nawiązujących do emaila. KAŻDY incydent INNY motyw. "
        f"Format: 'Protokół Incydentu [nr]: [tytuł]. [4-5 zdań]'.\n"
        f"LIMIT: każdy incydent max 400 znaków. Zwróć TYLKO czysty JSON."
    )
    raw_7c_inc = call_deepseek(_s(system_7), _u(user_7c_inc), MODEL_TYLER, max_tokens=1500)
    result_7c_inc = _parse_json_safe(raw_7c_inc, "zalecenia_7c_incydenty")
    if not isinstance(result_7c_inc, dict):
        result_7c_inc = {}
    current_app.logger.info(
        "[psych-raport] 7c_incydenty OK: %s",
        bool(result_7c_inc.get("incydenty_specjalne")),
    )

    # ── Scal wyniki pięciu wywołań ────────────────────────────────────────────
    result = {}
    result.update(result_7a_zal)
    result.update(result_7a_rok)
    result.update(result_7b)
    result.update(result_7c_sp)
    result.update(result_7c_inc)

    if not result:
        return {
            "zalecenia_tylera": {
                "naglowek": "ZALECENIA TERAPEUTYCZNE — PROTOKÓŁ AWARYJNY",
                "zadanie_1": "Zidentyfikować i wyeliminować główne źródło złudzeń opisanych w wiadomości.",
                "podpis": "Dr. Tyler Durden, Ordynator Oddziału Beznadziei",
            },
            "rokowanie": "Trudne. Pacjent przejawia objawy niezdrowego optymizmu.",
            "notatki_pielegniarek": [],
            "notatki_sprzataczki": [],
            "incydenty_specjalne": [],
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK #8 — prompty FLUX
# ─────────────────────────────────────────────────────────────────────────────


def _sekcja_flux_prompty(
    cfg: dict,
    body: str,
    nouns_dict: dict,
    sender_name: str,
    gender: str,
    test_mode: bool = False,
) -> dict:
    sec = cfg.get("deepseek_8_flux_prompty", {})
    if test_mode:
        current_app.logger.info(
            "[psych-raport] test_mode — pomijam generowanie promptów FLUX"
        )
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
    raw = call_deepseek(_s(system), _u(user), MODEL_TYLER, max_tokens=2250)
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
            ),
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
    "cytaty_z_przyjecia",  # długie, DeepSeek generuje je już z właściwym stylem
    "wypis",  # też długie, styl już OK z DeepSeek
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
    current_app.logger.info(
        "[psych-raport] DeepSeek tone check START (slim=%d kluczy)", len(raport_slim)
    )
    raw = call_deepseek(_s(system), _u(user), MODEL_TYLER)
    if not raw:
        current_app.logger.warning(
            "[psych-raport] DeepSeek tone check → brak odpowiedzi, skip"
        )
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
    """
    Podzielone na 3 osobne wywołania DeepSeek:
      cc_1 — sprawdź dane_pacjenta + cytaty_z_przyjecia   (max_tokens: 2250)
      cc_2 — sprawdź diagnozy + depozyt + farmakologia    (max_tokens: 2250)
      cc_3 — sprawdź zalecenia_tylera + rokowanie + wypis (max_tokens: 2250)
    Każdy call dostaje tylko swój fragment raportu — nie cały.
    """
    sec = cfg.get("deepseek_2_completeness_check", {})
    system = sec.get("system", "")
    instrukcje = "\n".join(sec.get("instrukcje", []))
    email_fragment = body[:MAX_DLUGOSC_EMAIL]

    raport_slim = {k: v for k, v in raport.items() if k not in _DEEPSEEK_SKIP}

    # Podział raportu na 3 grupy kluczy
    keys_cc1 = {"dane_pacjenta", "numer_historii_choroby", "data_przyjecia", "powod_przyjecia"}
    keys_cc2 = {"diagnoza_wstepna", "diagnoza_dodatkowa", "objawy", "choroba_wspolistniejaca",
                "depozyt", "farmakologia"}
    keys_cc3 = {"zalecenia_tylera", "rokowanie", "wypis", "leczenie_specjalne"}

    def _run_cc(group_keys: set, label: str) -> dict:
        fragment = {k: v for k, v in raport_slim.items() if k in group_keys}
        if not fragment:
            return {}
        user = (
            f"ORYGINALNY EMAIL PACJENTA:\n{email_fragment}\n\n"
            f"FRAGMENT RAPORTU DO SPRAWDZENIA ({label}):\n"
            f"{json.dumps(fragment, ensure_ascii=False, indent=2)}\n\n"
            f"INSTRUKCJE:\n{instrukcje}\n\n"
            f"Sprawdź TYLKO klucze podane powyżej. Pola '__BRAK__' — zostaw bez zmian.\n"
            f"Zwróć TYLKO czysty JSON z poprawkami (ta sama struktura kluczy)."
        )
        current_app.logger.info(
            "[psych-raport] completeness_check_%s START (%d kluczy)", label, len(fragment)
        )
        raw = call_deepseek(system, _u(user), MODEL_TYLER, max_tokens=2250)
        if not raw:
            current_app.logger.warning(
                "[psych-raport] completeness_check_%s → brak odpowiedzi", label
            )
            return {}
        result = _parse_json_safe(raw, f"completeness_{label}")
        if not isinstance(result, dict):
            return {}
        current_app.logger.info(
            "[psych-raport] completeness_check_%s OK (%d kluczy)", label, len(result)
        )
        return result

    merged = dict(raport)
    for group_keys, label in [
        (keys_cc1, "dane_cytaty"),
        (keys_cc2, "diagnozy_depozyt"),
        (keys_cc3, "zalecenia_wypis"),
    ]:
        result = _run_cc(group_keys, label)
        if result:
            merged = _merge_dicts(merged, result)

    current_app.logger.info("[psych-raport] completeness_check DONE — 3 calle")
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
        current_app.logger.warning(
            "[psych-raport] Brak konfiguracji deepseek_3_relacje_swiadkow"
        )
        return {"relacje_swiadkow": []}

    system = sec.get("system", "")
    schema = json.dumps(sec.get("schema", {}), ensure_ascii=False, indent=2)
    instrukcje = "\n".join(sec.get("instrukcje", []))

    # Przygotuj kontekst z raportu: imię pacjenta, stan cywilny, kluczowe zachowania
    imie_pacjenta = raport.get("dane_pacjenta", {}).get("imie_nazwisko", "pacjent")
    stan_cywilny = raport.get("dane_pacjenta", {}).get("stan_cywilny", "")

    user = (
        f"EMAIL PACJENTA:\n{body[:MAX_DLUGOSC_EMAIL]}\n\n"
        f"INFORMACJE O PACJENCIE:\n"
        f"- Imię: {imie_pacjenta}\n"
        f"- Stan cywilny: {stan_cywilny if stan_cywilny else 'nieznany'}\n\n"
        f"SCHEMAT JSON:\n{schema}\n\n"
        + (f"INSTRUKCJE:\n{instrukcje}\n\n" if instrukcje else "")
        + "Wygeneruj DOKŁADNIE 6 relacji świadków (ksiądz, dostawca jedzenia, kurier, rodzina, hydraulik, serwisant automatu do kawy).\n"
        f"Zwróć TYLKO czysty JSON z listą relacji świadków."
    )

    # 6 świadków × 5 zdań + gwara + kontekst = potrzeba dużo tokenów
    raw = call_deepseek(_s(system), _u(user), MODEL_TYLER, max_tokens=4500)
    result = _parse_json_safe(raw, "relacje_swiadkow")

    if not result:
        current_app.logger.warning(
            "[psych-raport] _sekcja_relacje_swiadkow → brak wyników"
        )
        return {"relacje_swiadkow": []}

    # Obsłuż przypadek gdy DeepSeek zwróci listę zamiast dict
    if isinstance(result, list):
        current_app.logger.info(
            "[psych-raport] relacje_swiadkow: otrzymano listę (%d) — owijam w dict",
            len(result),
        )
        return {"relacje_swiadkow": result}

    if not isinstance(result, dict):
        current_app.logger.warning(
            "[psych-raport] _sekcja_relacje_swiadkow → nieoczekiwany typ: %s",
            type(result),
        )
        return {"relacje_swiadkow": []}

    # Jeśli dict nie ma klucza relacje_swiadkow ale ma listę na pierwszym kluczu
    if "relacje_swiadkow" not in result:
        for v in result.values():
            if isinstance(v, list) and len(v) > 0:
                current_app.logger.info(
                    "[psych-raport] relacje_swiadkow: wyciągnięto listę z klucza"
                )
                return {"relacje_swiadkow": v}
        current_app.logger.warning(
            "[psych-raport] relacje_swiadkow: brak klucza relacje_swiadkow w dict"
        )
        return {"relacje_swiadkow": []}

    return result


# ─────────────────────────────────────────────────────────────────────────────
# FLUX — generowanie zdjęć
# ─────────────────────────────────────────────────────────────────────────────


def _hf_credit_exhausted(resp: requests.Response) -> bool:
    if resp.status_code != 402:
        return False
    text = (resp.text or "").lower()
    return (
        "depleted your monthly included credits" in text
        or "purchase pre-paid credits" in text
    )


def _substitute_or_none(label: str) -> str | None:
    """
    Zwraca base64 obrazka zastępczego (zastepczy.jpg) z podpisem,
    lub None jeśli plik nie istnieje.
    Używane gdy tokeny HF są niedostępne, wyczerpane lub zwracają błędy.
    """
    substitute = _load_substitute_image()
    if not substitute:
        current_app.logger.warning(
            "[psych-flux] Brak zastepczy.jpg — fotografia pominięta (%s)", label
        )
        return None
    caption = "pacjent" if label == "photo_pacjent" else "przedmioty"
    substitute = _add_text_below_image(
        substitute,
        f"Zdjęcie zastępcze — {caption} (tokeny HF niedostępne)",
        0,
    )
    current_app.logger.info("[psych-flux] Użyto zastepczy.jpg dla %s", label)
    return substitute.get("base64")


def _generate_flux(
    prompt: str,
    label: str,
    steps: int = 28,
    guidance: float = 7.0,
    width: int = 1024,
    height: int = 1024,
    test_mode: bool = False,
) -> str | None:
    """Generuje obrazek FLUX. Zwraca base64 JPG lub None."""
    if os.getenv("HF_TOKENS_ACTIVE", "tak").strip().lower() == "nie":
        current_app.logger.info(
            "[psych-flux] HF_TOKENS_ACTIVE=nie — używam zastepczy.jpg (%s)", label
        )
        return _substitute_or_none(label)

    if test_mode:
        substitute = _load_substitute_image()
        if substitute:
            # Dodaj tekst na dole jak w obrazkach Flux
            caption = "pacjent" if label == "photo_pacjent" else "przedmioty"
            substitute = _add_text_below_image(
                substitute, f"Zdjęcie zastępcze - {caption}", 0
            )
            current_app.logger.info(
                "[psych-flux] test_mode — używam zastepczy.jpg dla %s", label
            )
            return substitute.get("base64")
        current_app.logger.warning(
            "[psych-flux] test_mode — brak zastepczy.jpg, pomijam %s", label
        )
        return None

    tokens = get_active_tokens()
    if not tokens:
        current_app.logger.error(
            "[psych-flux] Brak tokenów HF dla %s — używam zastepczy.jpg", label
        )
        return _substitute_or_none(label)

    current_app.logger.info("[psych-flux] %s — prompt %.120s...", label, prompt)

    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "width": width,
            "height": height,
            "seed": random.randint(0, 2**32 - 1),
        },
    }

    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(
                HF_API_URL, headers=headers, json=payload, timeout=HF_TIMEOUT
            )
            if resp.status_code == 200:
                current_app.logger.info(
                    "[psych-flux] %s OK token=%s (%dB)", label, name, len(resp.content)
                )
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
                mark_dead(name)
                current_app.logger.warning(
                    "[psych-flux] %s 402 token=%s — wyczerpane kredyty, dodano do czarnej listy",
                    label,
                    name,
                )
                if _hf_credit_exhausted(resp):
                    current_app.logger.warning(
                        "[psych-flux] %s 402 wskazuje na globalne wyczerpanie kredytów — kończę próby",
                        label,
                    )
                    break
            elif resp.status_code in (401, 403):
                mark_dead(name)
                current_app.logger.warning(
                    "[psych-flux] %s HTTP %d token=%s — nieważny, dodano do czarnej listy",
                    label,
                    resp.status_code,
                    name,
                )
            elif resp.status_code == 429:
                current_app.logger.warning(
                    "[psych-flux] %s 429 token=%s → następny", label, name
                )
            else:
                current_app.logger.warning(
                    "[psych-flux] %s HTTP %d token=%s", label, resp.status_code, name
                )
        except Exception as e:
            current_app.logger.warning(
                "[psych-flux] %s wyjątek token=%s: %s", label, name, e
            )

    current_app.logger.error(
        "[psych-flux] %s — wszystkie tokeny zawiodły — używam zastepczy.jpg", label
    )
    return _substitute_or_none(label)


def _generate_photos_parallel(
    prompt_pacjent: str, prompt_przedmioty: str, test_mode: bool = False
) -> tuple:
    """
    Generuje oba zdjęcia równolegle. Zwraca (photo_pacjent, photo_przedmioty).
    """
    from flask import current_app as flask_app

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    app_obj = flask_app._get_current_object()

    def gen_pacjent():
        with app_obj.app_context():
            return _generate_flux(
                prompt_pacjent,
                "photo_pacjent",
                steps=28,
                guidance=7,
                test_mode=test_mode,
            )

    def gen_przedmioty():
        with app_obj.app_context():
            return _generate_flux(
                prompt_przedmioty,
                "photo_przedmioty",
                steps=28,
                guidance=7,
                test_mode=test_mode,
            )

    b64_pacjent = None
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
                "base64": b64.get("base64"),
                "content_type": b64.get("content_type", "image/jpeg"),
                "filename": b64.get("filename", f"psych_{suffix}_{ts}.jpg"),
            }
        return {
            "base64": b64,
            "content_type": "image/jpeg",
            "filename": f"psych_{suffix}_{ts}.jpg",
        }

    return _wrap(b64_pacjent, "pacjent"), _wrap(b64_przedmioty, "przedmioty")


# ─────────────────────────────────────────────────────────────────────────────
# BUDOWANIE DOCX
# ─────────────────────────────────────────────────────────────────────────────


def _build_docx(
    raport: dict,
    photo_pacjent_b64: str | None,
    photo_przedmioty_b64: str | None,
    cfg: dict,
) -> str | None:
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
    doc = Document()

    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    # Dodaj stopkę z numerami stron (XML — python-docx nie ma add_field na Run)
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    section = doc.sections[0]
    footer = section.footer
    footer_paragraph = (
        footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    )
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_paragraph.clear()

    def _add_page_field(paragraph, field_name):
        """Wstawia pole Word (PAGE lub NUMPAGES) do akapitu przez XML."""
        run = paragraph.add_run()
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        run._r.append(fld)
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " " + field_name + " "
        run._r.append(instr)
        fld2 = OxmlElement("w:fldChar")
        fld2.set(qn("w:fldCharType"), "separate")
        run._r.append(fld2)
        fld3 = OxmlElement("w:fldChar")
        fld3.set(qn("w:fldCharType"), "end")
        run._r.append(fld3)

    _add_page_field(footer_paragraph, "PAGE")
    sep_run = footer_paragraph.add_run(" / ")
    sep_run.font.size = Pt(9)
    sep_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    _add_page_field(footer_paragraph, "NUMPAGES")

    RED = RGBColor(0x99, 0x1A, 0x1A)
    DARK = RGBColor(0x0D, 0x0D, 0x0D)
    GREY = RGBColor(0x66, 0x66, 0x66)
    LGREY = RGBColor(0x99, 0x99, 0x99)

    def heading(text, level=1, color=DARK, size=14):
        h = doc.add_heading(text, level=level)
        h.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for run in h.runs:
            run.font.size = Pt(size)
            run.font.color.rgb = color
        return h

    def para(text, bold=False, italic=False, color=DARK, size=10, align=None):
        p = doc.add_paragraph()
        if align:
            p.alignment = align
        r = p.add_run(str(text))
        r.bold = bold
        r.italic = italic
        r.font.size = Pt(size)
        r.font.color.rgb = color
        return p

    def field(label, value, label_color=DARK, val_color=DARK, size=10):
        if value in (None, "", [], {}, "__BRAK__"):
            value = "[brak danych]"
            val_color = LGREY
        p = doc.add_paragraph()
        rl = p.add_run(f"{label}: ")
        rl.bold = True
        rl.font.size = Pt(size)
        rl.font.color.rgb = label_color
        rv = p.add_run(str(value))
        rv.font.size = Pt(size)
        rv.font.color.rgb = val_color

    def separator():
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.space_before = Pt(2)
        r = p.add_run("─" * 72)
        r.font.size = Pt(7)
        r.font.color.rgb = LGREY

    def insert_photo(b64: str, caption: str, width_cm: float = 14.0):
        if not b64:
            return
        try:
            img_bytes = base64.b64decode(b64)
            stream = io.BytesIO(img_bytes)
            doc.add_picture(stream, width=Cm(width_cm))
            cap = doc.add_paragraph(caption)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in cap.runs:
                r.font.size = Pt(8)
                r.font.italic = True
                r.font.color.rgb = GREY
        except Exception as e:
            current_app.logger.warning("[psych-docx] Błąd wstawiania zdjęcia: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # NAGŁÓWEK SZPITALA
    # ══════════════════════════════════════════════════════════════════════════
    h1 = doc.add_heading(
        szpital.get("nazwa", "Szpital Psychiatryczny im. Tylera Durdena"), 1
    )
    h1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in h1.runs:
        r.font.size = Pt(14)
        r.font.color.rgb = DARK

    p_adr = doc.add_paragraph(szpital.get("adres", ""))
    p_adr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in p_adr.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = GREY

    p_odd = doc.add_paragraph(szpital.get("oddzial", ""))
    p_odd.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in p_odd.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = GREY

    doc.add_paragraph()

    tyt = doc.add_heading("HISTORIA CHOROBY — KARTA PRZYJĘCIA I HOSPITALIZACJI", 2)
    tyt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in tyt.runs:
        r.font.size = Pt(12)

    nr = raport.get("numer_historii_choroby", "NY-2026-00000")
    data_przyj = raport.get("data_przyjecia", datetime.now().strftime("%d.%m.%Y"))
    nr_p = doc.add_paragraph(
        f"Nr: {nr}  |  Data przyjęcia: {data_przyj}  |  "
        f"Lekarz prowadzący: {szpital.get('lekarz', 'Dr. T. Durden')}"
    )
    nr_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in nr_p.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = GREY

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 1 — DANE PACJENTA
    # ══════════════════════════════════════════════════════════════════════════
    heading("I. DANE PACJENTA", 2, RED, 11)
    dp = raport.get("dane_pacjenta", {})
    field("Imię i nazwisko", dp.get("imie_nazwisko", ""))
    field("Wiek", dp.get("wiek", ""))
    field("Adres", dp.get("adres", ""))
    field("Zawód", dp.get("zawod", ""))
    field("Stan cywilny", dp.get("stan_cywilny", ""))
    field("Nr ubezpieczenia", dp.get("numer_ubezpieczenia", ""))
    doc.add_paragraph()

    if photo_pacjent_b64:
        heading("DOKUMENTACJA FOTOGRAFICZNA — PRZYJĘCIE", 3, GREY, 9)
        insert_photo(
            photo_pacjent_b64,
            "Fot. 1 — Pacjent w kaftanie bezpieczeństwa. Oddział B. Materiał dowodowy.",
        )
        doc.add_paragraph()

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 2 — POWÓD PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("II. POWÓD PRZYJĘCIA", 2, RED, 11)
    powod = raport.get("powod_przyjecia") or ""
    if not powod or powod == "__BRAK__":
        para(
            "[brak danych — sekcja nie została wygenerowana]",
            italic=True,
            color=LGREY,
            size=9,
        )
    else:
        para(powod, size=10)
    doc.add_paragraph()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 3 — CYTATY Z PRZYJĘCIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("III. CYTATY Z IZBY PRZYJĘĆ", 2, RED, 11)
    cytaty = raport.get("cytaty_z_przyjecia")
    if not cytaty or cytaty == "__BRAK__":
        para(
            "[brak danych — cytaty nie zostały wygenerowane]",
            italic=True,
            color=LGREY,
            size=9,
        )
    elif isinstance(cytaty, list):
        for c in cytaty:
            if c and str(c).strip() not in ("", "__BRAK__"):
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
        if lista and lista != ["__BRAK__"]:
            for item in lista:
                if str(item).strip() in ("", "__BRAK__"):
                    continue
                p_item = doc.add_paragraph(style="List Bullet")
                r_item = p_item.add_run(str(item))
                r_item.font.size = Pt(10)
                r_item.font.color.rgb = DARK
        else:
            para(
                "[brak danych — lista przedmiotów nie została wygenerowana]",
                italic=True,
                color=LGREY,
                size=9,
            )
        if proto and proto != "__BRAK__":
            doc.add_paragraph()
            para(proto, italic=True, color=GREY, size=9)
    else:
        para(
            "[brak danych — sekcja depozytu nie została wygenerowana]",
            italic=True,
            color=LGREY,
            size=9,
        )
    doc.add_paragraph()

    if photo_przedmioty_b64:
        heading("DOKUMENTACJA FOTOGRAFICZNA — DOWODY RZECZOWE", 3, GREY, 9)
        insert_photo(
            photo_przedmioty_b64,
            "Fot. 2 — Przedmioty skonfiskowane przy przyjęciu. "
            "Protokół dowodów rzeczowych, Oddział B.",
        )
        doc.add_paragraph()

    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 5 — FARMAKOLOGIA
    # ══════════════════════════════════════════════════════════════════════════
    heading("V. FARMAKOLOGIA — PEŁNA LISTA LEKÓW ZASTOSOWANYCH", 2, RED, 11)
    farm = raport.get("farmakologia", {})
    leki_lista = farm.get("leki", []) if isinstance(farm, dict) else []

    # Filtruj leki z wartością __BRAK__
    leki_lista = [
        l
        for l in leki_lista
        if isinstance(l, dict) and l.get("nazwa", "") != "__BRAK__"
    ]

    if leki_lista:
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        hdr = t.rows[0].cells
        for i, label in enumerate(
            ["Nazwa leku", "Przedmioty odebrane", "Wskazanie", "Dawkowanie"]
        ):
            hdr[i].text = ""
            r = hdr[i].paragraphs[0].add_run(label)
            r.bold = True
            r.font.size = Pt(9)
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
    else:
        para(
            "[brak danych — lista leków nie została wygenerowana]",
            italic=True,
            color=LGREY,
            size=9,
        )
        doc.add_paragraph()

    nota_farm = farm.get("nota_farmaceutyczna", "") if isinstance(farm, dict) else ""
    if nota_farm:
        para(nota_farm, italic=True, color=GREY, size=9)

    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 6 — HOSPITALIZACJA (14 dni)
    # ══════════════════════════════════════════════════════════════════════════
    heading("VI. PRZEBIEG HOSPITALIZACJI — DNI 1, 3, 14, 30", 2, RED, 11)

    dni_all = (raport.get("hospitalizacja_tydzien_1", []) or []) + (
        raport.get("hospitalizacja_tydzien_2", []) or []
    )

    for d in dni_all:
        if not isinstance(d, dict):
            continue
        dzien = d.get("dzien", "")
        data = d.get("data", "")
        zdarz = d.get("zdarzenie", "")
        lek = d.get("lek", "")
        stan = d.get("stan_pacjenta", "")
        nota = d.get("nota_lekarska", "")

        p_day = doc.add_paragraph(style="List Bullet")
        r_day = p_day.add_run(f"Dzień {dzien}  [{data}]")
        r_day.bold = True
        r_day.font.size = Pt(10)
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
            r_line.font.size = Pt(9)
            r_line.font.color.rgb = DARK

        if nota and nota != "__BRAK__":
            p_nota = doc.add_paragraph()
            p_nota.paragraph_format.left_indent = Pt(24)
            r_nota = p_nota.add_run(f"↳ {nota}")
            r_nota.italic = True
            r_nota.font.size = Pt(8)
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
        r1.bold = True
        r1.font.size = Pt(11)
        r1.font.color.rgb = RED
        if dw.get("nazwa_polska"):
            r2 = p_diag.add_run(f" (pol. {dw['nazwa_polska']})")
            r2.font.size = Pt(10)
            r2.font.color.rgb = DARK
        if dw.get("kod_dsm"):
            field("Kod DSM", dw["kod_dsm"], size=9)
        if dw.get("opis_kliniczny"):
            para(dw["opis_kliniczny"], size=10)
    doc.add_paragraph()

    dd = raport.get("diagnoza_dodatkowa", {})
    if isinstance(dd, dict):
        heading("Diagnoza Dodatkowa:", 3, DARK, 10)
        p_dd = doc.add_paragraph()
        r1 = p_dd.add_run(dd.get("nazwa_lacinska", ""))
        r1.bold = True
        r1.font.size = Pt(11)
        r1.font.color.rgb = RED
        if dd.get("nazwa_polska"):
            r2 = p_dd.add_run(f" (pol. {dd['nazwa_polska']})")
            r2.font.size = Pt(10)
            r2.font.color.rgb = DARK
        if dd.get("opis_kliniczny"):
            para(dd["opis_kliniczny"], size=10)
    doc.add_paragraph()

    cw = raport.get("choroba_wspolistniejaca", {})
    if isinstance(cw, dict):
        heading("Choroba Współistniejąca:", 3, DARK, 10)
        p_cw = doc.add_paragraph()
        r1 = p_cw.add_run(cw.get("nazwa_lacinska", ""))
        r1.bold = True
        r1.font.size = Pt(11)
        r1.font.color.rgb = RED
        if cw.get("nazwa_polska"):
            r2 = p_cw.add_run(f" (pol. {cw['nazwa_polska']})")
            r2.font.size = Pt(10)
            r2.font.color.rgb = DARK
        if cw.get("opis_kliniczny"):
            para(cw["opis_kliniczny"], size=10)
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
        if zt.get("zadanie_1") and zt["zadanie_1"] != "__BRAK__":
            p_z = doc.add_paragraph(style="List Number")
            p_z.add_run(str(zt["zadanie_1"])).font.size = Pt(10)
        if zt.get("podpis"):
            doc.add_paragraph()
            para(zt["podpis"], italic=True, color=GREY, size=9)
    doc.add_paragraph()
    separator()

    # ══════════════════════════════════════════════════════════════════════════
    # SEKCJA 10 — ROKOWANIE
    # ══════════════════════════════════════════════════════════════════════════
    heading("X. ROKOWANIE", 2, RED, 11)
    rokowanie = (
        raport.get("rokowanie", "").strip()
        if isinstance(raport.get("rokowanie"), str)
        else ""
    )
    if not rokowanie or rokowanie == "__BRAK__":
        rokowanie = "---brak---"
    p_rok = doc.add_paragraph()
    r_rok = p_rok.add_run(rokowanie)
    r_rok.font.size = Pt(10)
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
                    imie = n.get("imie_pielegniarki", "")
                    data = n.get("data", "")
                    tresc = n.get("tresc", "")
                    if tresc == "__BRAK__":
                        continue
                    header_parts = [x for x in [imie, data] if x and x != "__BRAK__"]
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
                    data = n.get("data", "")
                    tresc = n.get("tresc", "")
                    if tresc == "__BRAK__":
                        continue
                    if data and data != "__BRAK__":
                        p_hdr = doc.add_paragraph()
                        p_hdr.paragraph_format.left_indent = Pt(12)
                        r_hdr = p_hdr.add_run(data)
                        r_hdr.bold = True
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


def build_raport(
    body: str,
    previous_body: str | None,
    res_text: str,
    nouns_dict: dict,
    sender_name: str = "",
    gender: str = "patient",
    test_mode: bool = False,
) -> dict:
    """
    Główna funkcja modułu.

    Równoległość DeepSeek:
      Runda 1 (niezależne): #1 pacjent | #2 depozyt+leki | #6 diagnozy | #8 flux_prompty
      Runda 2 (równolegle): #3 dni 1,3 | #4 dni 14,30 | #5 wypis
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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

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
            return _sekcja_flux_prompty(
                cfg, body, nouns_dict, sender_name, gender, test_mode=test_mode
            )

    sekcja_pacjent = {}
    sekcja_dep_leki = {}
    sekcja_diagnozy = {}
    sekcja_flux = {}

    for name, fn in [
        ("pacjent", _r1_pacjent),
        ("depozyt", _r1_depozyt),
        ("diagnozy", _r1_diagnozy),
        ("flux", _r1_flux),
    ]:
        try:
            result = fn()
            if name == "pacjent":
                sekcja_pacjent = result
            elif name == "depozyt":
                sekcja_dep_leki = result
            elif name == "diagnozy":
                sekcja_diagnozy = result
            elif name == "flux":
                sekcja_flux = result
            current_app.logger.info("[psych-raport] Runda1 %s OK", name)
        except Exception as e:
            current_app.logger.error("[psych-raport] Runda1 %s błąd: %s", name, e)

    data_przyjecia = sekcja_pacjent.get(
        "data_przyjecia", datetime.now().strftime("%d.%m.%Y")
    )
    leki_lista = sekcja_dep_leki.get("farmakologia", {}).get("leki", [])

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

    dni_1_7 = []
    dni_8_14 = []
    sekcja_wypis = {}

    for name, fn in [
        ("tydzien1", _r2_tydzien1),
        ("tydzien2", _r2_tydzien2),
        ("wypis", _r2_wypis),
    ]:
        try:
            result = fn()
            if name == "tydzien1":
                dni_1_7 = result
            elif name == "tydzien2":
                dni_8_14 = result
            elif name == "wypis":
                sekcja_wypis = result
            current_app.logger.info(
                "[psych-raport] Runda2 %s OK (%s elementów)",
                name,
                len(result) if isinstance(result, list) else "?",
            )
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
    raport["depozyt"] = sekcja_dep_leki.get("depozyt", {})
    raport["farmakologia"] = sekcja_dep_leki.get("farmakologia", {})
    raport["hospitalizacja_tydzien_1"] = dni_1_7
    raport["hospitalizacja_tydzien_2"] = dni_8_14
    raport.update(sekcja_wypis)
    raport.update(sekcja_diagnozy)
    raport.update(sekcja_zalecenia)

    current_app.logger.info(
        "[psych-raport] Scalono %d kluczy przed DeepSeek", len(raport)
    )

    # ── DeepSeek completeness check ──────────────────────────────────────────
    raport = _deepseek_completeness_check(cfg, raport, body)
    current_app.logger.info("[psych-raport] DeepSeek#1 completeness OK")

    # ── Relacje świadków ─────────────────────────────────────────────────────
    try:
        relacje_result = _sekcja_relacje_swiadkow(cfg, body, raport)
        if relacje_result and "relacje_swiadkow" in relacje_result:
            raport["relacje_swiadkow"] = relacje_result["relacje_swiadkow"]
            current_app.logger.info(
                "[psych-raport] Relacje świadków OK (%d relacji)",
                len(relacje_result["relacje_swiadkow"]),
            )
        else:
            raport["relacje_swiadkow"] = []
            current_app.logger.warning("[psych-raport] Brak relacji świadków")
    except Exception as e:
        current_app.logger.error(
            "[psych-raport] Błąd generowania relacji świadków: %s", e
        )
        raport["relacje_swiadkow"] = []

    # ── FLUX — oba zdjęcia równolegle ─────────────────────────────────────────
    prompt_pacjent = sekcja_flux.get("prompt_pacjent", "")
    prompt_przedmioty = sekcja_flux.get("prompt_przedmioty", "")
    photo_1, photo_2 = _generate_photos_parallel(
        prompt_pacjent, prompt_przedmioty, test_mode=test_mode
    )
    current_app.logger.info(
        "[psych-raport] FLUX photo1=%s photo2=%s", bool(photo_1), bool(photo_2)
    )

    # ── Buduj DOCX (zawsze — nawet bez zdjęć) ────────────────────────────────
    photo_1_b64 = photo_1["base64"] if photo_1 else None
    photo_2_b64 = photo_2["base64"] if photo_2 else None
    docx_b64 = _build_docx(raport, photo_1_b64, photo_2_b64, cfg)

    if not docx_b64:
        current_app.logger.error("[psych-raport] DOCX nie wygenerowany")
        return {"raport_pdf": None, "psych_photo_1": photo_1, "psych_photo_2": photo_2}

    imie = raport.get("dane_pacjenta", {}).get("imie_nazwisko", "pacjent")
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", imie)[:30]

    raport_pdf_dict = {
        "base64": docx_b64,
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "filename": f"raport_psychiatryczny_{safe}_{ts}.docx",
    }

    current_app.logger.info(
        "[psych-raport] DONE raport=%s photo1=%s photo2=%s",
        raport_pdf_dict["filename"],
        photo_1["filename"] if photo_1 else "brak",
        photo_2["filename"] if photo_2 else "brak",
    )

    return {
        "raport_pdf": raport_pdf_dict,
        "psych_photo_1": photo_1,
        "psych_photo_2": photo_2,
    }
