"""
responders/obrazek.py
Responder OBRAZEK — generuje obrazek AI z treści maila.

Używa Hugging Face Inference API z modelem FLUX.1-schnell.
Token HF ustawiasz w Render jako zmienną środowiskową: HF_TOKEN
Styl obrazka pochodzi z pliku: prompts/prompt_obrazek.txt

Parametry generowania (identyczne jak w wersji lokalnej):
  - num_inference_steps: 30
  - guidance_scale:      3.5

Dodanie do app.py (już gotowe):
    from responders.obrazek import build_obrazek_section
    if data.get("wants_obrazek"):
        response_data["obrazek"] = build_obrazek_section(body)
"""

import os
import re
import base64
import requests
from flask import current_app

from core.ai_client import call_groq, MODEL_TYLER

# ── Stałe ─────────────────────────────────────────────────────────────────────
HF_API_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "black-forest-labs/FLUX.1-schnell"
)
HF_STEPS    = 30     # num_inference_steps  (z programu lokalnego)
HF_GUIDANCE = 3.5    # guidance_scale       (z programu lokalnego)
TIMEOUT_SEC = 60     # HF bywa wolne — 60 sek
MAX_PROMPT  = 400    # limit znaków promptu tematycznego

# Ścieżka do pliku stylu
BASE_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_STYLE_FILE = os.path.join(BASE_DIR, "prompts", "prompt_obrazek.txt")


# ── Wczytaj styl z pliku ──────────────────────────────────────────────────────
def _load_style() -> str:
    """
    Wczytuje styl (suffix promptu) z pliku prompts/prompt_obrazek.txt.
    Fallback = domyślny styl komiksowy z programu 3_dziala_huggins_tokeny.py.
    """
    try:
        with open(PROMPT_STYLE_FILE, encoding="utf-8") as f:
            style = f.read().strip()
            if style:
                return style
    except Exception as e:
        current_app.logger.warning("Nie można wczytać prompt_obrazek.txt: %s", e)

    return (
        "black and white comic style, thick bold ink lines, high contrast, "
        "simplified shapes, no text, no speech bubbles, hand-drawn look"
    )


# ── Skróć treść maila do promptu obrazkowego ─────────────────────────────────
def _build_image_prompt(body: str, style: str) -> str:
    """
    Używa Groq żeby zamienić treść maila na krótki prompt wizualny po angielsku.
    Fallback: pierwsze zdanie maila.
    Styl z pliku dołączany jako suffix — tak jak w programie lokalnym.
    """
    try:
        res = call_groq(
            "Zamień poniższy tekst na krótki prompt obrazkowy po angielsku "
            "(max 20 słów, konkretny, wizualny, bez cudzysłowów, bez wyjaśnień):\n\n"
            + body[:500],
            "",
            MODEL_TYLER,
        )
        if res and res.strip():
            prompt = re.sub(r'["\'\n]', ' ', res.strip())
            prompt = prompt[:MAX_PROMPT]
        else:
            raise ValueError("Pusta odpowiedź Groq")
    except Exception as e:
        current_app.logger.warning("Groq prompt generation failed: %s", e)
        first  = re.split(r'[.!?\n]', body.strip())[0].strip()
        prompt = first[:MAX_PROMPT] if first else "abstract colorful art"

    # Suffix stylu — identycznie jak w programie lokalnym
    if style.strip():
        full_prompt = f"{prompt}\nStyle: {style.strip()}"
    else:
        full_prompt = prompt

    return full_prompt


# ── Wywołaj HF API i pobierz PNG ─────────────────────────────────────────────
def _generate_image_hf(full_prompt: str) -> bytes:
    """
    Wysyła prompt do Hugging Face FLUX.1-schnell.
    Zwraca bytes PNG lub b'' przy błędzie.
    """
    hf_token = os.getenv("HF_TOKEN", "")
    if not hf_token:
        current_app.logger.error("Brak HF_TOKEN w zmiennych środowiskowych Render!")
        return b""

    headers = {
        "Authorization": f"Bearer {hf_token}",
        "Accept":        "image/png",
    }
    payload = {
        "inputs": full_prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
        },
    }

    current_app.logger.info("HF FLUX prompt: %.120s", full_prompt)

    try:
        resp = requests.post(
            HF_API_URL,
            headers=headers,
            json=payload,
            timeout=TIMEOUT_SEC,
        )
        if resp.status_code == 200:
            current_app.logger.info(
                "HF FLUX sukces — PNG %d B", len(resp.content)
            )
            return resp.content
        else:
            current_app.logger.error(
                "HF FLUX błąd %s: %s", resp.status_code, resp.text[:300]
            )
    except requests.exceptions.Timeout:
        current_app.logger.error("HF FLUX: timeout po %d sek", TIMEOUT_SEC)
    except Exception as e:
        current_app.logger.error("HF FLUX: nieoczekiwany błąd: %s", e)

    return b""


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_obrazek_section(body: str) -> dict:
    """
    Buduje sekcję 'obrazek':
      1. Wczytuje styl z prompts/prompt_obrazek.txt
      2. Tłumaczy treść maila na prompt wizualny (Groq)
      3. Generuje obrazek PNG przez HF FLUX.1-schnell
      4. Zwraca base64 PNG + HTML z wiadomością dla nadawcy

    Zwracany dict:
    {
        "reply_html":  str,
        "image": {
            "base64":       str | None,
            "content_type": "image/png",
            "filename":     "obrazek_ai.png",
        },
        "prompt_used": str,
    }
    """
    if not body or not body.strip():
        return {
            "reply_html": "<p>Brak treści do wygenerowania obrazka.</p>",
            "image": {
                "base64":       None,
                "content_type": "image/png",
                "filename":     "obrazek_ai.png",
            },
            "prompt_used": "",
        }

    # 1. Wczytaj styl
    style = _load_style()

    # 2. Zbuduj prompt
    full_prompt = _build_image_prompt(body, style)
    current_app.logger.info("Pełny prompt obrazka: %.200s", full_prompt)

    # 3. Generuj obrazek przez HF
    png_bytes = _generate_image_hf(full_prompt)
    png_b64   = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None

    # 4. Treść HTML odpowiedzi
    if png_b64:
        reply_html = (
            "<p>Na podstawie Twojej treści wygenerowałem następujący obrazek, "
            "który załączam.</p>"
        )
    else:
        reply_html = (
            "<p>Na podstawie Twojej treści próbowałem wygenerować obrazek, "
            "jednak wystąpił błąd po stronie serwisu AI. "
            "Spróbuj ponownie za chwilę.</p>"
        )

    current_app.logger.info(
        "Obrazek AI: sukces=%s | rozmiar=%d B",
        bool(png_b64), len(png_bytes)
    )

    return {
        "reply_html": reply_html,
        "image": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     "obrazek_ai.png",
        },
        "prompt_used": full_prompt,
    }
