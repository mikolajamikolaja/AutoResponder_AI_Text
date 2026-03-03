"""
responders/nawiazanie.py
Responder NAWIĄZANIE — analizuje historię konwersacji z nadawcą.

Przepływ:
  1. Apps Script wysyła poprzednią wiadomość (previous_body, previous_subject)
     razem z webhookiem — jeśli była w Google Sheets
  2. Jeśli previous_body istnieje → wysyłamy do DeepSeek:
     obecna wiadomość + poprzednia + instrukcja z prompt_nawiazanie.txt
  3. DeepSeek zwraca osobistą analizę nawiązującą do poprzedniej rozmowy
  4. Render zwraca sekcję 'nawiazanie' → Apps Script wysyła osobny email

Wymagane pola w webhooku (z Apps Script):
  previous_body    — treść ostatniej wiadomości od tego nadawcy (lub null)
  previous_subject — temat ostatniej wiadomości (lub null)
  sender           — adres email nadawcy
"""

import os
import re
from flask import current_app

from core.ai_client import call_groq as call_deepseek, MODEL_TYLER

# ── Ścieżki ───────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR   = os.path.join(BASE_DIR, "prompts")
PROMPT_FILE   = os.path.join(PROMPTS_DIR, "prompt_nawiazanie.txt")


# ── Wczytaj plik promptu ──────────────────────────────────────────────────────
def _load_prompt(fallback: str) -> str:
    try:
        with open(PROMPT_FILE, encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except Exception as e:
        current_app.logger.warning("Nie można wczytać %s: %s", PROMPT_FILE, e)
    return fallback


# ── Buduj instrukcję dla DeepSeek ────────────────────────────────────────────
def _build_instruction(
    current_body: str,
    previous_body: str,
    previous_subject: str,
) -> str:
    template = _load_prompt(
        fallback=(
            "Porównaj dwie wiadomości od tej samej osoby.\n\n"
            "POPRZEDNIA WIADOMOŚĆ (temat: [PREVIOUS_SUBJECT]):\n[PREVIOUS_BODY]\n\n"
            "OBECNA WIADOMOŚĆ:\n[CURRENT_BODY]\n\n"
            "Nawiąż do poprzedniej wiadomości, zwróć się po imieniu, "
            "opisz potrzeby tej osoby nawet te niewypowiedziane wprost."
        )
    )

    instruction = template.replace("[PREVIOUS_SUBJECT]", previous_subject or "brak tematu")
    instruction = instruction.replace("[PREVIOUS_BODY]",   previous_body[:1500])
    instruction = instruction.replace("[CURRENT_BODY]",    current_body[:1500])
    return instruction


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_nawiazanie_section(
    body: str,
    previous_body: str | None,
    previous_subject: str | None,
    sender: str = "",
) -> dict:
    """
    Buduje sekcję 'nawiazanie'.

    Jeśli brak previous_body — zwraca has_history=False i pusty reply_html.
    Jeśli jest historia — wywołuje DeepSeek i zwraca analizę.

    Parametry:
      body             — treść obecnej wiadomości
      previous_body    — treść ostatniej wiadomości od tego nadawcy (lub None)
      previous_subject — temat ostatniej wiadomości (lub None)
      sender           — adres email nadawcy (do logów)
    """

    # Brak historii — cicho, nic nie robimy
    if not previous_body or not previous_body.strip():
        current_app.logger.info(
            "Nawiązanie: brak historii dla nadawcy %s — pomijam", sender
        )
        return {
            "has_history": False,
            "reply_html":  "",
            "analysis":    "",
        }

    current_app.logger.info(
        "Nawiązanie: znaleziono historię dla %s | poprzedni temat: %s",
        sender, previous_subject
    )

    # Buduj instrukcję i wywołaj DeepSeek
    instruction = _build_instruction(body, previous_body, previous_subject or "")

    result = call_deepseek(instruction, "", MODEL_TYLER)

    if not result or not result.strip():
        current_app.logger.warning(
            "Nawiązanie: DeepSeek nie zwrócił analizy dla %s", sender
        )
        return {
            "has_history": True,
            "reply_html":  "<p>Nie udało się wygenerować analizy nawiązania.</p>",
            "analysis":    "",
        }

    # Wyczyść wynik
    analysis = re.sub(r'\n{3,}', '\n\n', result.strip())
    analysis_html = analysis.replace("\n", "<br>")

    current_app.logger.info(
        "Nawiązanie: analiza gotowa dla %s (%.150s...)", sender, analysis
    )

    reply_html = (
        "<p><strong>Nawiązanie do poprzedniej rozmowy:</strong></p>"
        f"<p>{analysis_html}</p>"
    )

    return {
        "has_history": True,
        "reply_html":  reply_html,
        "analysis":    analysis,
    }
