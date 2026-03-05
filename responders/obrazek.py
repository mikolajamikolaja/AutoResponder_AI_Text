"""
responders/obrazek.py
Responder OBRAZEK — generuje 2 wersje 4-ujęciowego komiksu AI z treści maila.

Przepływ:
  1. Treść maila (X) → DeepSeek z instrukcją z pliku
     prompts/1_przygotowanie_opisu_scen_obrazka.txt
     → powstaje scenariusz 4 scen komiksowych (Y)
  2. Scenariusz (Y) + styl z pliku prompts/2_prompt_obrazek_styl.txt
     → HF FLUX.1-schnell generuje obrazek PNG #1
  3. Scenariusz (Y) + styl z pliku prompts/3_prompt_obrazek_styl.txt
     → HF FLUX.1-schnell generuje obrazek PNG #2
  Kroki 2 i 3 wykonywane ASYNCHRONICZNIE (ThreadPoolExecutor).
  Style pobierane WYŁĄCZNIE z plików 2_ i 3_ — brak fallbacków stylu w kodzie.

Tokeny HF w Render: HF_TOKEN, HF_TOKEN1 ... HF_TOKEN20
Klucz DeepSeek w Render: API_KEY_DEEPSEEK
"""

import os
import re
import base64
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

# ── Stałe ─────────────────────────────────────────────────────────────────────
HF_API_URLS = [
    "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
]
HF_STEPS    = 5
HF_GUIDANCE = 5    # zakres 0-5 dla FLUX.1-schnell
TIMEOUT_SEC = 55   # nieco poniżej 60s aby nie kolidować z timeoutem Render

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
SCENE_FILE  = os.path.join(PROMPTS_DIR, "1_przygotowanie_opisu_scen_obrazka.txt")
STYLE1_FILE = os.path.join(PROMPTS_DIR, "2_prompt_obrazek_styl.txt")
STYLE2_FILE = os.path.join(PROMPTS_DIR, "3_prompt_obrazek_styl.txt")


# ── Wczytaj plik ──────────────────────────────────────────────────────────────
def _load_file(path: str, fallback: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except Exception as e:
        current_app.logger.warning("Nie można wczytać %s: %s", path, e)
    return fallback


# ── KROK 1: Mail → DeepSeek → scenariusz 4 scen (Y) ──────────────────────────
def _build_scene_prompt(body: str):
    """Zwraca scenariusz (str) lub None gdy DeepSeek nie odpowie."""
    instruction_template = _load_file(
        SCENE_FILE,
        fallback=(
            "Create a 4-panel comic strip script based on this email. "
            "Output only visual scene descriptions in English.\n\n"
            "EMAIL CONTENT:\n[PASTE EMAIL HERE]"
        )
    )
    instruction = instruction_template.replace("[PASTE EMAIL HERE]", body[:2000])
    current_app.logger.info("DeepSeek — generuję scenariusz 4 scen...")

    result = call_deepseek(instruction, "", MODEL_TYLER)

    if result and result.strip():
        scene_text = re.sub(r'"{3,}', '', result.strip())
        scene_text = re.sub(r'\n{3,}', '\n\n', scene_text)
        current_app.logger.info("DeepSeek scenariusz (%.200s...)", scene_text)
        return scene_text

    current_app.logger.warning("DeepSeek nie zwrócił scenariusza — przerywam generowanie")
    return None


# ── KROK 2: Scenariusz (Y) + plik stylu → pełny prompt HF ───────────────────
def _build_hf_prompt(scene_text: str, style_file: str) -> str:
    """Łączy scenariusz ze stylem z pliku. Zwraca pusty string gdy brak pliku."""
    style = _load_file(style_file)
    if not style:
        current_app.logger.error("Brak pliku stylu: %s — przerywam", style_file)
        return ""
    current_app.logger.info("STYL UŻYTY dla %s: %.200s", style_file, style)
    return f"{scene_text}\n\n{style}"


# ── Zbierz tokeny HF ──────────────────────────────────────────────────────────
def _get_hf_tokens() -> list:
    names = [
        "HF_TOKEN",  "HF_TOKEN1",  "HF_TOKEN2",  "HF_TOKEN3",  "HF_TOKEN4",
        "HF_TOKEN5", "HF_TOKEN6",  "HF_TOKEN7",  "HF_TOKEN8",  "HF_TOKEN9",
        "HF_TOKEN10","HF_TOKEN11", "HF_TOKEN12", "HF_TOKEN13", "HF_TOKEN14",
        "HF_TOKEN15","HF_TOKEN16", "HF_TOKEN17", "HF_TOKEN18", "HF_TOKEN19",
        "HF_TOKEN20",
    ]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


# ── Generowanie pojedynczego obrazka przez HF ────────────────────────────────
def _generate_image_hf(full_prompt: str, label: str) -> bytes:
    """
    Wysyła pełny prompt do HF FLUX.1-schnell.
    Dla każdego modelu próbuje tokeny po kolei.
    Zwraca bytes PNG lub b'' przy błędzie.
    """
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[%s] Brak HF_TOKEN w zmiennych środowiskowych!", label)
        return b""

    payload = {
        "inputs": full_prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
        },
    }

    current_app.logger.info(
        "[%s] HF — tokeny: %s | prompt: %.150s",
        label, [n for n, _ in tokens], full_prompt,
    )

    for model_url in HF_API_URLS:
        model_name = model_url.split("/")[-1]
        current_app.logger.info("[%s] Próbuję model: %s", label, model_name)

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
                        "[%s] ✓ Sukces! Model=%s | token=%s | PNG %d B",
                        label, model_name, name, len(resp.content)
                    )
                    return resp.content
                elif resp.status_code in (401, 403):
                    current_app.logger.warning(
                        "[%s] Model=%s token %s nieważny — następny token",
                        label, model_name, name
                    )
                elif resp.status_code in (503, 529):
                    current_app.logger.warning(
                        "[%s] Model=%s token %s przeciążony — następny token",
                        label, model_name, name
                    )
                else:
                    current_app.logger.warning(
                        "[%s] Model=%s token %s błąd %s — następny token",
                        label, model_name, name, resp.status_code
                    )
            except requests.exceptions.Timeout:
                current_app.logger.warning(
                    "[%s] Model=%s token %s timeout — następny token",
                    label, model_name, name
                )
            except Exception as e:
                current_app.logger.warning(
                    "[%s] Model=%s token %s błąd: %s — następny token",
                    label, model_name, name, str(e)[:50]
                )

        current_app.logger.info(
            "[%s] Model %s zawiódł — próbuję następny model", label, model_name
        )

    current_app.logger.error("[%s] Wszystkie modele i tokeny zawiodły!", label)
    return b""


# ── HTML ze scenariusza ───────────────────────────────────────────────────────
def _scene_to_html(text: str) -> str:
    html = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r'\*?\*?Panel\s+\d+', line, re.IGNORECASE):
            clean = re.sub(r'\*+', '', line).strip()
            html.append(f'<h3 style="color:#333;margin-top:16px">{clean}</h3>')
        elif 'Visual Description' in line:
            clean = re.sub(r'\*+', '', line).replace('Visual Description:', '').strip()
            html.append(f'<p><strong>🎨 Scena:</strong> {clean}</p>')
        elif re.match(r'\*?\*?(Rob|Paul|Speech Bubble)', line, re.IGNORECASE):
            clean = re.sub(r'\*+', '', line).strip()
            html.append(f'<p style="margin-left:16px">💬 {clean}</p>')
        elif re.sub(r'[\*\s]', '', line):
            clean = re.sub(r'\*+', '', line).strip()
            html.append(f'<p style="margin-left:16px">{clean}</p>')
    return '\n'.join(html)


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_obrazek_section(body: str) -> dict:
    """
    Buduje sekcję 'obrazek':
      Krok 1 — treść maila → DeepSeek → scenariusz 4 scen
      Krok 2 — scenariusz + styl z pliku → dwa prompty HF
      Krok 3 — oba obrazki generowane ASYNCHRONICZNIE
      Krok 4 — mail zwrotny ze scenariuszem i obrazkami PNG
    """
    empty_result = {
        "reply_html":  "",
        "image":       {"base64": None, "content_type": "image/png", "filename": "komiks_ai.png"},
        "image2":      {"base64": None, "content_type": "image/png", "filename": "komiks_ai_retro.png"},
        "prompt_used": "",
    }

    if not body or not body.strip():
        empty_result["reply_html"] = "<p>Brak treści do wygenerowania obrazka.</p>"
        return empty_result

    # Krok 1 — DeepSeek generuje scenariusz
    scene_text = _build_scene_prompt(body)
    if not scene_text:
        empty_result["reply_html"] = "<p>Nie udało się wygenerować scenariusza — spróbuj ponownie.</p>"
        return empty_result

    # Krok 2 — budujemy dwa prompty ze stylów z plików
    prompt1 = _build_hf_prompt(scene_text, STYLE1_FILE)
    prompt2 = _build_hf_prompt(scene_text, STYLE2_FILE)

    if not prompt1 or not prompt2:
        empty_result["reply_html"] = "<p>Brak pliku stylu — sprawdź pliki prompts/2_ i 3_.</p>"
        return empty_result

    current_app.logger.info("Pełny prompt HF #1: %.200s", prompt1)
    current_app.logger.info("Pełny prompt HF #2: %.200s", prompt2)

    # Krok 3 — generujemy oba obrazki równolegle
    from flask import current_app as flask_app
    app = flask_app._get_current_object()

    def gen1():
        with app.app_context():
            return _generate_image_hf(prompt1, "obrazek_1")

    def gen2():
        with app.app_context():
            return _generate_image_hf(prompt2, "obrazek_2")

    with ThreadPoolExecutor(max_workers=2) as executor:
        future1 = executor.submit(gen1)
        future2 = executor.submit(gen2)
        png1 = future1.result()
        png2 = future2.result()

    png1_b64 = base64.b64encode(png1).decode("ascii") if png1 else None
    png2_b64 = base64.b64encode(png2).decode("ascii") if png2 else None

    # Krok 4 — treść maila zwrotnego
    scene_html = _scene_to_html(scene_text)

    status_parts = []
    if png1_b64:
        status_parts.append("komiks czarno-biały")
    if png2_b64:
        status_parts.append("komiks retro-pop")

    if status_parts:
        reply_html = (
            "<p>Na podstawie Twojej treści automatycznie utworzyłem prompt "
            f"do obrazków, które załączam ({' i '.join(status_parts)}):</p>"
            f"<blockquote>{scene_html}</blockquote>"
        )
    else:
        reply_html = (
            "<p>Na podstawie Twojej treści automatycznie utworzyłem prompt "
            "do obrazków:</p>"
            f"<blockquote>{scene_html}</blockquote>"
            "<p>Wystąpił błąd podczas generowania obrazków. Spróbuj ponownie za chwilę.</p>"
        )

    current_app.logger.info(
        "Obrazki AI: #1 sukces=%s (%d B) | #2 sukces=%s (%d B)",
        bool(png1_b64), len(png1), bool(png2_b64), len(png2)
    )

    # Krok 5 — kodujemy prompty jako base64 text/plain do załączników
    prompt1_b64 = base64.b64encode(prompt1.encode("utf-8")).decode("ascii")
    prompt2_b64 = base64.b64encode(prompt2.encode("utf-8")).decode("ascii")

    return {
        "reply_html":  reply_html,
        "image": {
            "base64":       png1_b64,
            "content_type": "image/png",
            "filename":     "komiks_ai.png",
        },
        "image2": {
            "base64":       png2_b64,
            "content_type": "image/png",
            "filename":     "komiks_ai_retro.png",
        },
        "prompt1_txt": {
            "base64":       prompt1_b64,
            "content_type": "text/plain",
            "filename":     "2222.txt",
        },
        "prompt2_txt": {
            "base64":       prompt2_b64,
            "content_type": "text/plain",
            "filename":     "3333.txt",
        },
        "prompt_used": scene_text,
    }
