"""
responders/emocje.py
Responder EMOCJE — empatyczny pocieszyciel.

Generuje odpowiedzi WSZYSTKIMI 8 metodami pocieszenia naraz.
Odbiorca wybiera właściwą.

Strategia AI:
  1 zapytanie do DeepSeek → tablica 8 JSON-ów (jedna na metodę)
  Fallback: 8 osobnych zapytań jeśli parsowanie tablicy się nie uda.

Zmiany v2:
  - wszystkie 8 metod generowane zawsze
  - nagłówek emaila: TYLKO jedna linia tekstu w kolorze (bez "Jestem tutaj" h1)
  - brak zbędnego miejsca w body
"""

import re
import os
import json
import logging
from flask import current_app

from core.ai_client import call_deepseek, extract_clean_text, MODEL_TYLER

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
PROMPT_JSON = os.path.join(PROMPTS_DIR, "emocje.json")

# Wszystkie 8 metod w ustalonej kolejności
ALL_METODY = [
    "walidacja_emocji",
    "obecnosc",
    "normalizacja",
    "odzwierciedlenie",
    "przestrzen_na_cisze",
    "docenienie_odwagi",
    "bez_srebrnych_podszewek",
    "cieplo_przez_konkret",
]


# ── Ładowanie promptu ─────────────────────────────────────────────────────────


def _load_prompt() -> dict:
    try:
        with open(PROMPT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[emocje] Brak emocje.json: %s — używam fallbacku", e)
        return _fallback_prompt()


def _fallback_prompt() -> dict:
    return {
        "system": (
            "Jesteś empatycznym towarzyszem. Twoje jedyne zadanie to pocieszyć osobę "
            "która napisała wiadomość. NIE dajesz rad. NIE proponujesz rozwiązań. "
            "Odpowiadasz WYŁĄCZNIE w formacie JSON bez żadnego tekstu poza nawiasami."
        ),
        "user_template": (
            "Przeczytaj poniższą wiadomość i wygeneruj ciepłe, empatyczne odpowiedzi pocieszenia "
            "WSZYSTKIMI 8 metodami.\n\n"
            "### WIADOMOŚĆ:\n{{MAIL}}\n\n"
            "### IMIĘ NADAWCY:\n{{SENDER_NAME}}\n\n"
            "### WYMAGANIA:\n"
            "- Dla każdej metody: 4-6 akapitów HTML (<p>...</p>)\n"
            "- NIE dawaj rad ani rozwiązań\n"
            "- Odwołuj się do konkretnych słów z wiadomości\n"
            "- Nastrój: smutek|lęk|frustracja|ból|neutralna|złość|samotność\n"
            "- Intensywność 0-10\n\n"
            "### FORMAT — zwróć TYLKO tablicę JSON z 8 obiektami:\n"
            "[\n"
            '  {"metoda": "walidacja_emocji", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "obecnosc", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "normalizacja", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "odzwierciedlenie", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "przestrzen_na_cisze", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "docenienie_odwagi", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "bez_srebrnych_podszewek", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7},\n'
            '  {"metoda": "cieplo_przez_konkret", "pocieszenie": "<p>...</p>", "nastroj": "smutek", "intensywnosc": 7}\n'
            "]"
        ),
        "fallback_pocieszenie": (
            "<p>Dostałem/am Twoją wiadomość i jestem tutaj.</p>"
            "<p>To co czujesz ma sens. Nie musisz teraz nic robić — wystarczy że jesteś.</p>"
            "<p>Jestem z Tobą w tym.</p>"
        ),
    }


# ── Call AI — jedno zbiorcze zapytanie ───────────────────────────────────────


def _generuj_wszystkie_metody(
    body: str, sender_name: str, prompt_data: dict
) -> list[dict] | None:
    """
    Jedno wywołanie DeepSeek → zwraca listę 8 dict-ów (jedna per metoda).
    Fallback: None jeśli nie uda się sparsować.
    """
    template = prompt_data.get("user_template", _fallback_prompt()["user_template"])
    system_msg = prompt_data.get("system", "Odpowiadaj WYŁĄCZNIE w JSON.")

    user_msg = template.replace("{{MAIL}}", body[:4000]).replace(
        "{{SENDER_NAME}}", sender_name or "nieznane"
    )

    # Dołącz opisy metod jeśli są w JSON
    metody_def = prompt_data.get("metody_pocieszenia", [])
    if metody_def:
        metody_txt = "\n### OPISY METOD:\n"
        for m in metody_def:
            metody_txt += (
                f"- [{m.get('id', '?')}] {m.get('nazwa', '')} ({m.get('id_key', m.get('id', ''))}): "
                f"{m.get('opis', '')} "
                f"(przykład: \"{m.get('przyklad', '')}\")\n"
            )
        user_msg += metody_txt

    zasady = prompt_data.get("zasady_odpowiedzi", [])
    if zasady:
        user_msg += "\n### ZASADY:\n" + "\n".join(f"- {z}" for z in zasady)

    raw = call_deepseek(system_msg, user_msg, MODEL_TYLER)
    if not raw:
        logger.error("[emocje] DeepSeek nie odpowiedział")
        return None

    clean = extract_clean_text(raw) if callable(extract_clean_text) else raw
    clean = re.sub(r"```json\s*", "", clean)
    clean = re.sub(r"```\s*", "", clean)
    clean = clean.strip()

    # Naprawa nawiasów
    if clean.count("[") > clean.count("]"):
        clean += "]"
    if clean.count("{") > clean.count("}"):
        clean += "}"

    try:
        decoder = json.JSONDecoder()
        # Szukaj pierwszego '[' — powinniśmy dostać tablicę
        for match in re.finditer(r"\[", clean):
            start = match.start()
            try:
                obj, _ = decoder.raw_decode(clean[start:])
                if isinstance(obj, list) and len(obj) > 0:
                    return obj
            except json.JSONDecodeError:
                continue
        # Może AI zwróciło pojedynczy obiekt zamiast tablicy
        for match in re.finditer(r"\{", clean):
            start = match.start()
            try:
                obj, _ = decoder.raw_decode(clean[start:])
                if isinstance(obj, dict):
                    return [obj]
            except json.JSONDecodeError:
                continue
        raise json.JSONDecodeError("Brak tablicy JSON", clean, 0)
    except json.JSONDecodeError:
        logger.error("[emocje] Nie można sparsować JSON: %s...", clean[:300])
        return None


def _generuj_jedna_metoda(
    body: str, sender_name: str, metoda_key: str, prompt_data: dict
) -> dict | None:
    """
    Fallback: osobne wywołanie dla jednej metody (używane gdy zbiorcze się nie uda).
    """
    metody_def = prompt_data.get("metody_pocieszenia", [])
    opis_metody = ""
    for m in metody_def:
        if (
            m.get("id_key", m.get("id", "")) == metoda_key
            or m.get("nazwa", "").lower().replace(" ", "_") == metoda_key
        ):
            opis_metody = f"{m.get('nazwa', metoda_key)}: {m.get('opis', '')} (przykład: \"{m.get('przyklad', '')}\")"
            break
    if not opis_metody:
        opis_metody = metoda_key.replace("_", " ")

    system_msg = prompt_data.get("system", "Odpowiadaj WYŁĄCZNIE w JSON.")
    user_msg = (
        f"Przeczytaj wiadomość i wygeneruj pocieszenie METODĄ: {opis_metody}\n\n"
        f"### WIADOMOŚĆ:\n{body[:4000]}\n\n"
        f"### IMIĘ:\n{sender_name or 'nieznane'}\n\n"
        f"### FORMAT JSON:\n"
        f'{{"metoda": "{metoda_key}", "pocieszenie": "<p>...</p><p>...</p>", '
        f'"nastroj": "smutek|lęk|frustracja|ból|neutralna|złość|samotność", "intensywnosc": 0}}'
    )

    zasady = prompt_data.get("zasady_odpowiedzi", [])
    if zasady:
        user_msg += "\n### ZASADY:\n" + "\n".join(f"- {z}" for z in zasady)

    raw = call_deepseek(system_msg, user_msg, MODEL_TYLER)
    if not raw:
        return None

    clean = extract_clean_text(raw) if callable(extract_clean_text) else raw
    clean = re.sub(r"```json\s*", "", clean).replace("```", "").strip()
    if clean.count("{") > clean.count("}"):
        clean += "}"

    try:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", clean):
            try:
                obj, _ = decoder.raw_decode(clean[match.start() :])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _wyciagnij_imie(sender_name: str, sender_email: str = "") -> str:
    name = (sender_name or "").strip()
    if not name or "@" in name:
        if sender_email:
            local = sender_email.split("@")[0]
            local = re.sub(r"[._+\-]", " ", local).strip()
            local = re.split(r"\s+", local)[0]
            local = re.sub(r"\d+", "", local).strip()
            if local:
                return local.capitalize()
        return ""
    return name


def _nastroj_do_koloru(nastroj: str) -> dict:
    palety = {
        "smutek": {
            "bg": "#eeedfe",
            "border": "#afa9ec",
            "ink": "#534ab7",
            "accent": "#534ab7",
        },
        "ból": {
            "bg": "#eeedfe",
            "border": "#afa9ec",
            "ink": "#534ab7",
            "accent": "#534ab7",
        },
        "lęk": {
            "bg": "#faeeda",
            "border": "#fac775",
            "ink": "#854f0b",
            "accent": "#854f0b",
        },
        "frustracja": {
            "bg": "#fcebeb",
            "border": "#f09595",
            "ink": "#a32d2d",
            "accent": "#a32d2d",
        },
        "złość": {
            "bg": "#fcebeb",
            "border": "#f09595",
            "ink": "#a32d2d",
            "accent": "#a32d2d",
        },
        "samotność": {
            "bg": "#fbeaf0",
            "border": "#f0a8c4",
            "ink": "#993556",
            "accent": "#993556",
        },
        "neutralna": {
            "bg": "#d4f0e8",
            "border": "#7ecab8",
            "ink": "#1d8a6e",
            "accent": "#1d8a6e",
        },
    }
    return palety.get(nastroj, palety["neutralna"])


def _metoda_do_tagu(metoda: str) -> str:
    mapy = {
        "walidacja_emocji": "metoda 01 · walidacja emocji",
        "obecnosc": "metoda 02 · obecność",
        "normalizacja": "metoda 03 · normalizacja",
        "odzwierciedlenie": "metoda 04 · odzwierciedlenie",
        "przestrzen_na_cisze": "metoda 05 · przestrzeń na ciszę",
        "docenienie_odwagi": "metoda 06 · docenienie odwagi",
        "bez_srebrnych_podszewek": "metoda 07 · bez srebrnych podszewek",
        "cieplo_przez_konkret": "metoda 08 · ciepło przez konkret",
    }
    return mapy.get(metoda, f"metoda · {metoda.replace('_', ' ')}")


# ── Budowanie HTML bloku jednej metody ────────────────────────────────────────


def _buduj_html_blok_metody(
    pocieszenie_html: str,
    sender_name: str,
    metoda: str,
    nastroj: str,
) -> str:
    """
    Zwraca HTML dla JEDNEJ metody.
    Nagłówek: tylko jedna linia tekstu w kolorze atramentu — bez h1, bez dużych tytułów.
    Minimalny footprint w body.
    """
    kolory = _nastroj_do_koloru(nastroj)
    tag = _metoda_do_tagu(metoda)
    imie = sender_name or ""
    powitanie = f"<p>Drogi/a {imie},</p>" if imie else ""

    return f"""<div style="border-left:3px solid {kolory['border']};padding:10px 14px 10px 14px;margin-bottom:18px;background:{kolory['bg']};border-radius:0 10px 10px 0;">
  <p style="font-family:'DM Mono',monospace;font-size:10px;color:{kolory['ink']};margin:0 0 8px 0;letter-spacing:0.04em;">{tag}</p>
  <div style="font-family:'DM Mono',monospace;font-size:13px;color:#2a1f14;line-height:1.75;">
    {powitanie}
    {pocieszenie_html}
  </div>
</div>"""


def _buduj_html_email_multi(
    metody_results: list[dict],
    sender_name: str,
    nastroj_dominujacy: str,
) -> str:
    """
    Składa cały reply_html ze wszystkich metod jako lista bloków.
    """
    kolory = _nastroj_do_koloru(nastroj_dominujacy)
    imie = sender_name or "Nadawca"

    bloki = []
    for r in metody_results:
        blok = _buduj_html_blok_metody(
            r.get("pocieszenie", "<p>Jestem tutaj.</p>"),
            sender_name,
            r.get("metoda", "obecnosc"),
            r.get("nastroj", nastroj_dominujacy),
        )
        bloki.append(blok)

    bloki_html = "\n".join(bloki)

    return f"""<div style="font-family:'DM Mono',monospace;color:#2a1f14;max-width:560px;margin:0 auto;padding:16px 14px;">
  <p style="font-size:10px;color:{kolory['ink']};margin:0 0 16px 0;opacity:0.7;letter-spacing:0.05em;">
    pocieszenie dla {imie} · {len(metody_results)} metod
  </p>
  {bloki_html}
  <div style="margin-top:16px;padding-top:10px;border-top:1px solid #d3cfc8;font-size:10px;color:#8a7a6a;text-align:center;">
    <em>nastrój: {nastroj_dominujacy}</em>
  </div>
</div>"""


# ── Główna funkcja responderu ─────────────────────────────────────────────────


def build_emocje_section(
    body: str,
    sender_name: str = "",
    sender_email: str = "",
    attachments: list = None,
    test_mode: bool = False,
) -> dict:
    """
    Emocje responder — generuje odpowiedzi WSZYSTKIMI 8 metodami.

    Strategia AI:
      - 1 zapytanie zbiorcze → lista 8 dict-ów
      - jeśli parsowanie tablicy nie wyjdzie → 8 osobnych zapytań (fallback)

    Zwraca dict z:
      reply_html  – HTML z blokami wszystkich 8 metod
      images      – []
      docs        – ZIP z pełnym HTML i SVG pierwszej metody
    """
    prompt_data = _load_prompt()
    mail_text = (body or "").strip()
    if not mail_text:
        fallback = prompt_data.get(
            "fallback_pocieszenie",
            "<p>Dostałem/am Twoją wiadomość i jestem tutaj.</p>",
        )
        return {"reply_html": fallback, "images": [], "docs": []}

    imie = _wyciagnij_imie(sender_name, sender_email)

    # ── Strategia: jedno zbiorcze zapytanie ──────────────────────────────────

    metody_results = _generuj_wszystkie_metody(mail_text, imie, prompt_data)

    # ── Fallback: 8 osobnych zapytań jeśli nie dostaliśmy tablicy ────────────

    if not metody_results:
        logger.warning(
            "[emocje] Zbiorcze zapytanie nie zadziałało — fallback: 8 osobnych"
        )
        metody_results = []
        for metoda_key in ALL_METODY:
            r = _generuj_jedna_metoda(mail_text, imie, metoda_key, prompt_data)
            if r:
                metody_results.append(r)
            else:
                # Absolutny fallback dla tej metody
                metody_results.append(
                    {
                        "metoda": metoda_key,
                        "pocieszenie": prompt_data.get(
                            "fallback_pocieszenie", "<p>Jestem tutaj z Tobą.</p>"
                        ),
                        "nastroj": "neutralna",
                        "intensywnosc": 5,
                    }
                )

    # Upewnij się że mamy wszystkie 8 — uzupełnij brakujące fallbackiem
    metody_keys_got = {r.get("metoda") for r in metody_results}
    for metoda_key in ALL_METODY:
        if metoda_key not in metody_keys_got:
            metody_results.append(
                {
                    "metoda": metoda_key,
                    "pocieszenie": prompt_data.get(
                        "fallback_pocieszenie", "<p>Jestem tutaj.</p>"
                    ),
                    "nastroj": "neutralna",
                    "intensywnosc": 5,
                }
            )

    # Sortuj wg kolejności z ALL_METODY
    order = {k: i for i, k in enumerate(ALL_METODY)}
    metody_results.sort(key=lambda r: order.get(r.get("metoda", ""), 99))

    # Dominujący nastrój — bierzemy z pierwszego wyniku
    nastroj_dominujacy = metody_results[0].get("nastroj", "neutralna")

    # ── reply_html — wszystkie metody ────────────────────────────────────────

    reply_html = _buduj_html_email_multi(metody_results, imie, nastroj_dominujacy)

    logger.info(
        "[emocje] metod=%d | nastrój=%s",
        len(metody_results),
        nastroj_dominujacy,
    )

    return {
        "reply_html": reply_html,
        "images": [],
        "docs": [],
    }
