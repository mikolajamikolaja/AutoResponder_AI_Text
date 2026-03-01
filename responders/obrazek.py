"""
responders/obrazek.py
Responder OBRAZEK — generuje 4-ujęciowy komiks AI z treści maila.

Przepływ:
  1. Treść maila (X) → DeepSeek z instrukcją z pliku
     prompts/1_przygotowanie_opisu_scen_obrazka.txt
     → powstaje scenariusz 4 scen komiksowych (Y)
  2. Scenariusz (Y) + styl z pliku
     prompts/2_prompt_obrazek_styl.txt
     → HF FLUX.1-schnell generuje obrazek PNG
  3. Mail zwrotny zawiera treść Y i obrazek PNG w załączniku

Tokeny HF w Render: HF_TOKEN, HF_TOKEN1, HF_TOKEN2, HF_TOKEN3, HF_TOKEN4
Klucz DeepSeek w Render: API_KEY_DEEPSEEK
"""

import os
import re
import base64
import requests
from flask import current_app

from core.ai_client import call_groq as call_deepseek, MODEL_TYLER

# ── Stałe ─────────────────────────────────────────────────────────────────────
HF_API_URLS = [
    "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
    "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-3-medium",
]
HF_STEPS    = 30
HF_GUIDANCE = 3.5
TIMEOUT_SEC = 60

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR    = os.path.join(BASE_DIR, "prompts")
SCENE_FILE     = os.path.join(PROMPTS_DIR, "1_przygotowanie_opisu_scen_obrazka.txt")
STYLE_FILE     = os.path.join(PROMPTS_DIR, "2_prompt_obrazek_styl.txt")


# ── Wczytaj plik promptu ──────────────────────────────────────────────────────
def _load_file(path: str, fallback: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except Exception as e:
        current_app.logger.warning("Nie można wczytać %s: %s", path, e)
    return fallback


# ── KROK 1: Mail → DeepSeek → scenariusz 4 scen (Y) ──────────────────────────
def _build_scene_prompt(body: str) -> str:
    """
    Wysyła treść maila do DeepSeek z instrukcją z pliku
    1_przygotowanie_opisu_scen_obrazka.txt.
    Zwraca scenariusz 4 scen komiksowych po angielsku (treść Y).
    """
    instruction_template = _load_file(
        SCENE_FILE,
        fallback=(
            "Create a 4-panel comic strip script based on this email. "
            "Output only visual scene descriptions in English.\n\n"
            "EMAIL CONTENT:\n[PASTE EMAIL HERE]"
        )
    )

    # Podmień placeholder na treść maila
    instruction = instruction_template.replace("[PASTE EMAIL HERE]", body[:2000])

    current_app.logger.info("DeepSeek — generuję scenariusz 4 scen...")

    result = call_deepseek(instruction, "", MODEL_TYLER)

    if result and result.strip():
        # Usuń ewentualne cudzysłowy i nadmiarowe nowe linie
        scene_text = re.sub(r'"{3,}', '', result.strip())
        scene_text = re.sub(r'\n{3,}', '\n\n', scene_text)
        current_app.logger.info(
            "DeepSeek scenariusz (%.200s...)", scene_text
        )
        return scene_text

    # Fallback jeśli DeepSeek zawiedzie
    current_app.logger.warning("DeepSeek nie zwrócił scenariusza — używam fallback")
    return "Two people arguing about emotions. One dramatic, one eating a sandwich."


# ── KROK 2: Scenariusz (Y) + styl → pełny prompt do HF ───────────────────────
def _build_hf_prompt(scene_text: str) -> str:
    """
    Łączy scenariusz scen (Y) ze stylem komiksowym z pliku
    2_prompt_obrazek_styl.txt.
    """
    style = _load_file(
        STYLE_FILE,
        fallback=(
            "4-panel comic strip, black and white, thick ink lines, "
            "oversized heads, exaggerated expressions, no text outside bubbles."
        )
    )
    return f"{scene_text}\n\n{style}"


# ── Zbierz tokeny HF ──────────────────────────────────────────────────────────
def _get_hf_tokens() -> list:
    names  = ["HF_TOKEN", "HF_TOKEN1", "HF_TOKEN2", "HF_TOKEN3", "HF_TOKEN4",
              "HF_TOKEN5", "HF_TOKEN6", "HF_TOKEN7"]
    tokens = []
    for name in names:
        val = os.getenv(name, "").strip()
        if val:
            tokens.append((name, val))
    return tokens


# ── KROK 3: Prompt → HF (FLUX / SD3) → PNG ──────────────────────────────────
def _generate_image_hf(full_prompt: str) -> bytes:
    """
    Wysyła pełny prompt do HF.
    Próbuje modeli w kolejności: FLUX.1-schnell → Stable Diffusion 3.
    Dla każdego modelu próbuje tokeny po kolei.
    Zwraca bytes PNG lub b'' przy błędzie.
    """
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("Brak HF_TOKEN w zmiennych środowiskowych!")
        return b""

    payload = {
        "inputs": full_prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
        },
    }

    current_app.logger.info(
        "HF — tokeny: %s | prompt: %.150s",
        [n for n, _ in tokens], full_prompt,
    )

    # Próbuj każdy model
    for model_url in HF_API_URLS:
        model_name = model_url.split("/")[-1]
        current_app.logger.info("Próbuję model: %s", model_name)
        
        for name, token in tokens:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept":        "image/png",
            }
            try:
                resp = requests.post(
                    model_url, headers=headers, json=payload, timeout=TIMEOUT_SEC
                )
                if resp.status_code == 200:
                    current_app.logger.info(
                        "✓ Sukces! Model=%s | token=%s | PNG %d B", 
                        model_name, name, len(resp.content)
                    )
                    return resp.content
                elif resp.status_code in (401, 403):
                    current_app.logger.warning(
                        "Model=%s token %s nieważny — następny token",
                        model_name, name
                    )
                elif resp.status_code in (503, 529):
                    current_app.logger.warning(
                        "Model=%s token %s przeciążony — następny token",
                        model_name, name
                    )
                else:
                    current_app.logger.warning(
                        "Model=%s token %s błąd %s — następny token",
                        model_name, name, resp.status_code
                    )
            except requests.exceptions.Timeout:
                current_app.logger.warning(
                    "Model=%s token %s timeout — następny token",
                    model_name, name
                )
            except Exception as e:
                current_app.logger.warning(
                    "Model=%s token %s błąd: %s — następny token",
                    model_name, name, str(e)[:50]
                )
        
        current_app.logger.info(
            "Model %s zawiódł ze wszystkimi tokenami — próbuję następny model",
            model_name
        )

    current_app.logger.error(
        "Wszystkie modele i tokeny zawiodły!"
    )
    return b""


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_obrazek_section(body: str) -> dict:
    """
    Buduje sekcję 'obrazek':
      Krok 1 — treść maila (X) → DeepSeek → scenariusz 4 scen (Y)
      Krok 2 — scenariusz (Y) + styl → pełny prompt HF
      Krok 3 — HF FLUX.1-schnell (fallback: SD3) → PNG
      Krok 4 — mail zwrotny z treścią Y i obrazkiem PNG
    """
    if not body or not body.strip():
        return {
            "reply_html": "<p>Brak treści do wygenerowania obrazka.</p>",
            "image": {
                "base64":       None,
                "content_type": "image/png",
                "filename":     "komiks_ai.png",
            },
            "prompt_used": "",
        }

    # Krok 1 — DeepSeek generuje scenariusz Y
    scene_text = _build_scene_prompt(body)

    # Krok 2 — łączymy Y ze stylem
    full_prompt = _build_hf_prompt(scene_text)
    current_app.logger.info("Pełny prompt HF: %.300s", full_prompt)

    # Krok 3 — HF generuje PNG
    png_bytes = _generate_image_hf(full_prompt)
    png_b64   = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None

    # Krok 4 — treść maila zwrotnego
    # Pokazujemy nadawcy scenariusz Y (bez części stylistycznej)
    scene_html = scene_text.replace("\n", "<br>")

    if png_b64:
        reply_html = (
            "<p>Na podstawie Twojej treści automatycznie utworzyłem prompt "
            "do obrazka, który załączam:</p>"
            f"<blockquote>{scene_html}</blockquote>"
        )
    else:
        reply_html = (
            "<p>Na podstawie Twojej treści automatycznie utworzyłem prompt "
            "do obrazka:</p>"
            f"<blockquote>{scene_html}</blockquote>"
            "<p>Jednak wystąpił błąd podczas generowania obrazka po stronie "
            "serwisu AI. Spróbuj ponownie za chwilę.</p>"
        )

    current_app.logger.info(
        "Obrazek AI: sukces=%s | PNG=%d B", bool(png_b64), len(png_bytes)
    )

    return {
        "reply_html": reply_html,
        "image": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     "komiks_ai.png",
        },
        "prompt_used": scene_text,
    }
