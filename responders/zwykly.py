"""
responders/zwykly.py
Responder emocjonalny — Tyler Durden + Sokrates.

ZMIANY W TEJ WERSJI:
  1. prompt.txt → prompt.json  (czysta struktura, render programowy)
  2. Groq PIERWSZY do generowania tekstu, DeepSeek jako fallback
  3. Brak ograniczeń długości tekstu
  4. Generowanie tryptyku FLUX (3 panele Fight Club)
     - styl z zwykly_obrazek_tyler.js
     - Groq generuje prompty dla każdego panelu, DeepSeek fallback
     - rotacja tokenów HF (HF_TOKEN, HF_TOKEN1...HF_TOKEN20)
     - jeśli tokeny wyczerpane → wysyłamy tyle ile wygenerowano
  5. Każdy panel PNG jest od razu konwertowany do JPG 95% (Pillow)
     - PNG FLUX ~2MB → JPG 95% ~300-500KB
     - nazwa: tyler_YYYYMMDD_HHMMSS_panel{N}.jpg
     - zwracany content_type: image/jpeg
  6. Nadawca dostaje: reply_html + emotka PNG + PDF emocji + tryptyk JPG
     (inline w mailu + załącznik JPG)
  7. Pole triptych_for_drive zawiera listę JPG do zapisu na Google Drive
     przez GAS (_saveTylerJpgsToDrive) — ta sama logika co smierc.py
"""

import os
import re
import io
import json
import random
import base64
import requests
from datetime import datetime
from flask import current_app

from core.ai_client    import call_deepseek, extract_clean_text, sanitize_model_output, MODEL_TYLER
from core.files        import read_file_base64
from core.html_builder import build_html_reply

# ─────────────────────────────────────────────────────────────────────────────
# ŚCIEŻKI
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOTKI_DIR  = os.path.join(BASE_DIR, "emotki")
PDF_DIR     = os.path.join(BASE_DIR, "pdf")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")

PROMPT_JSON_PATH   = os.path.join(PROMPTS_DIR, "prompt.json")
STYLE_JS_PATH      = os.path.join(PROMPTS_DIR, "zwykly_obrazek_tyler.js")

# ─────────────────────────────────────────────────────────────────────────────
# STAŁE API
# ─────────────────────────────────────────────────────────────────────────────
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

HF_API_URL   = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS     = 3
HF_GUIDANCE  = 3
HF_TIMEOUT   = 55
TYLER_JPG_QUALITY = 95   # Kompresja JPG dla paneli tryptyku (95% = minimalna strata)

# ─────────────────────────────────────────────────────────────────────────────
# MAPOWANIE EMOCJI → PLIKI
# ─────────────────────────────────────────────────────────────────────────────
EMOCJA_MAP = {
    "radosc": "twarz_radosc",
    "smutek": "twarz_smutek",
    "zlosc":  "twarz_zlosc",
    "lek":    "twarz_lek",
    "nuda":   "twarz_nuda",
    "spokoj": "twarz_spokoj",
}
FALLBACK_EMOT = "error"


# ═══════════════════════════════════════════════════════════════════════════════
# ŁADOWANIE prompt.json
# ═══════════════════════════════════════════════════════════════════════════════

def _load_prompt_json() -> dict:
    """
    Wczytuje prompt.json z katalogu prompts/.
    Fallback: minimalny słownik jeśli plik nie istnieje.
    """
    try:
        with open(PROMPT_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        current_app.logger.info("[zwykly] prompt.json wczytany OK")
        return data
    except FileNotFoundError:
        current_app.logger.error("[zwykly] Brak prompt.json: %s — używam fallbacku", PROMPT_JSON_PATH)
    except json.JSONDecodeError as e:
        current_app.logger.error("[zwykly] Błąd JSON w prompt.json: %s", e)
    return _fallback_prompt_dict()


def _fallback_prompt_dict() -> dict:
    """Minimalny fallback gdyby prompt.json był niedostępny."""
    return {
        "system": "Odpowiadaj WYŁĄCZNIE w formacie JSON bez żadnego tekstu poza klamrami {}.",
        "output_schema": {
            "odpowiedz_tekstowa": "...",
            "kategoria_pdf": "Manifest Wolności",
            "emocja": "radosc|smutek|zlosc|lek|nuda|spokoj"
        },
        "instrukcje": {
            "sokrates": "Odpowiedz mądrze, max 4 zdania, podpisz: Sokrates.",
            "tyler": "Styl nihilistyczny Fight Club. Podpisz: Tyler Durden.",
            "zasady_nota": "Dostosuj zasady twórczo do spraw nadawcy."
        },
        "zasady_tylera": [
            "Pierwsza zasada: Nie mówi się o tym.",
            "Druga zasada: Nie mówi się o tym.",
            "Trzecia zasada: Jeśli ktoś zawoła stop, walka się kończy.",
            "Czwarta zasada: Walczą tylko dwaj faceci.",
            "Piąta zasada: Jedna walka naraz.",
            "Szósta zasada: Żadnych koszul, żadnych butów.",
            "Siódma zasada: Walki trwają tak długo jak muszą.",
            "Ósma zasada: Jeśli to twoja pierwsza noc, musisz walczyć."
        ],
        "manifesty": [
            {"temat": "KONSUMPCJONIZM", "tresc": "Rzeczy, które posiadasz, w końcu zaczynają posiadać ciebie."},
            {"temat": "HISTORIA", "tresc": "Jesteśmy średnimi dziećmi historii."},
            {"temat": "SAMODOSKONALENIE", "tresc": "Samodoskonalenie to masturbacja."},
            {"temat": "TOŻSAMOŚĆ", "tresc": "Nie jesteś swoją pracą."},
            {"temat": "PROJEKT CHAOS", "tresc": "Pewnego dnia umrzesz. Jesteś trybem w maszynie."}
        ],
        "formatowanie_adresata": "Użyj formy: Drogi [Imię]-[Przymiotnik]-[Przydomek].",
        "user_text_placeholder": "{{USER_TEXT}}"
    }


def _render_prompt(data: dict, body: str) -> str:
    """
    Buduje pełny string promptu z danych prompt.json.
    Zwraca gotowy tekst do wysłania do modelu.
    """
    lines = []

    # Instrukcja systemu
    lines.append(data.get("system", ""))
    lines.append("")

    # Schemat wyjściowy
    schema = data.get("output_schema", {})
    if schema:
        lines.append("### SCHEMAT JSON DO WYPEŁNIENIA:")
        lines.append(json.dumps(schema, ensure_ascii=False, indent=2))
        lines.append("")

    # Tekst użytkownika
    placeholder = data.get("user_text_placeholder", "{{USER_TEXT}}")
    lines.append(f"Tekst użytkownika:\n{body}")
    lines.append("")

    # Zasady odpowiedzi
    inst = data.get("instrukcje", {})
    if inst:
        lines.append("### ZASADY ODPOWIEDZI:")
        if inst.get("sokrates"):
            lines.append(f"1. SOKRATES: {inst['sokrates']}")
        if inst.get("tyler"):
            lines.append(f"2. TYLER DURDEN: {inst['tyler']}")
        lines.append("")

    # Zasady Tylera (lista)
    zasady = data.get("zasady_tylera", [])
    nota = inst.get("zasady_nota", "")
    if zasady:
        lines.append("### ELEMENTY DLA TYLERA (Wpleć w wypowiedź):")
        if nota:
            lines.append(nota)
        for zasada in zasady:
            lines.append(f"- {zasada}")
        lines.append("")

    # Manifesty
    manifesty = data.get("manifesty", [])
    if manifesty:
        lines.append("### MANIFESTY TYLERA (Dostosuj i wygłoś każdy):")
        for i, m in enumerate(manifesty, 1):
            lines.append(f"{i}. O {m.get('temat', '???')}: {m.get('tresc', '')}")
        lines.append("")

    # Formatowanie adresata
    fmt = data.get("formatowanie_adresata", "")
    if fmt:
        lines.append("### FORMATOWANIE ADRESATA:")
        lines.append(fmt)
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# GROQ — główny model (szybszy), DeepSeek — fallback
# ═══════════════════════════════════════════════════════════════════════════════

def _call_groq(system: str, user: str, max_tokens: int = 4000) -> str | None:
    """
    Wywołuje Groq API (llama-3.3-70b-versatile).
    Zwraca tekst odpowiedzi lub None przy błędzie.
    """
    api_key = os.getenv("API_KEY_GROQ", "").strip()
    if not api_key:
        current_app.logger.warning("[groq] Brak API_KEY_GROQ w env")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        "max_tokens":  max_tokens,
        "temperature": 0.9,
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            current_app.logger.info("[groq] OK (%d znaków)", len(result))
            return result
        current_app.logger.warning("[groq] HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        current_app.logger.warning("[groq] Wyjątek: %s", str(e)[:120])
    return None


def _call_ai_with_fallback(system: str, user: str, max_tokens: int = 4000) -> tuple[str | None, str]:
    """
    Groq PIERWSZY → DeepSeek FALLBACK.
    Zwraca (tekst_odpowiedzi, nazwa_providera).
    """
    result = _call_groq(system, user, max_tokens=max_tokens)
    if result:
        return result, "groq"

    current_app.logger.warning("[zwykly] Groq zawiódł → próbuję DeepSeek")
    result = call_deepseek(system, user, MODEL_TYLER)
    if result:
        return result, "deepseek"

    current_app.logger.error("[zwykly] Oba modele zawiodły!")
    return None, "none"


# ═══════════════════════════════════════════════════════════════════════════════
# PARSOWANIE ODPOWIEDZI MODELU
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_response(raw: str) -> tuple[str, str]:
    """
    Parsuje odpowiedź modelu (oczekujemy JSON).
    Zwraca (tekst_odpowiedzi, emotion_key).
    """
    if not raw:
        return "", FALLBACK_EMOT

    json_str = raw.strip()
    # Wytnij blok JSON jeśli model owinął w ```json ... ```
    m = re.search(r'\{.*\}', json_str, re.DOTALL)
    if m:
        json_str = m.group(0)

    try:
        data   = json.loads(json_str)
        tekst  = data.get("odpowiedz_tekstowa", "").strip()
        emocja = data.get("emocja", "").strip().lower()

        # Usuń ewentualne znaki | jeśli model zwrócił schemat zamiast wartości
        if "|" in emocja:
            emocja = emocja.split("|")[0].strip()

        emotion_key = EMOCJA_MAP.get(emocja, FALLBACK_EMOT)

        if not tekst:
            tekst = sanitize_model_output(raw)

        current_app.logger.info("[zwykly] emocja=%s → plik=%s", emocja, emotion_key)
        return tekst, emotion_key

    except Exception as e:
        current_app.logger.warning("[zwykly] Błąd parsowania JSON: %s | raw=%.200s", e, raw)
        return sanitize_model_output(raw), FALLBACK_EMOT


# ═══════════════════════════════════════════════════════════════════════════════
# EMOTKA + PDF
# ═══════════════════════════════════════════════════════════════════════════════

def _get_emoticon_and_pdf(emotion_key: str) -> tuple:
    """Zwraca (png_b64, pdf_b64) dla danej emocji z fallbackiem na error."""
    png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{emotion_key}.png"))
    pdf_b64 = read_file_base64(os.path.join(PDF_DIR,    f"{emotion_key}.pdf"))

    if not png_b64:
        current_app.logger.warning("[zwykly] Brak PNG dla %s, używam error.png", emotion_key)
        png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{FALLBACK_EMOT}.png"))
    if not pdf_b64:
        current_app.logger.warning("[zwykly] Brak PDF dla %s, używam error.pdf", emotion_key)
        pdf_b64 = read_file_base64(os.path.join(PDF_DIR, f"{FALLBACK_EMOT}.pdf"))

    return png_b64, pdf_b64


# ═══════════════════════════════════════════════════════════════════════════════
# ŁADOWANIE WYTYCZNYCH STYLU (zwykly_obrazek_tyler.js)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_style_config() -> dict:
    """
    Wczytuje STYLE_CONFIG z pliku zwykly_obrazek_tyler.js.
    Wyciąga blok JSON między znacznikami // <STYLE_CONFIG> i // </STYLE_CONFIG>.
    """
    try:
        with open(STYLE_JS_PATH, encoding="utf-8") as f:
            content = f.read()

        m = re.search(r'//\s*<STYLE_CONFIG>(.*?)//\s*</STYLE_CONFIG>', content, re.DOTALL)
        if not m:
            current_app.logger.warning("[zwykly-img] Brak bloku STYLE_CONFIG w %s", STYLE_JS_PATH)
            return {}

        config = json.loads(m.group(1).strip())
        current_app.logger.info("[zwykly-img] Wczytano STYLE_CONFIG OK")
        return config

    except FileNotFoundError:
        current_app.logger.warning("[zwykly-img] Brak pliku %s", STYLE_JS_PATH)
    except json.JSONDecodeError as e:
        current_app.logger.error("[zwykly-img] Błąd parsowania STYLE_CONFIG: %s", e)
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# GENEROWANIE PROMPTÓW DLA TRYPTYKU (Groq → DeepSeek fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_panel_prompt(
    panel_index: int,
    panel_config: dict,
    style_config: dict,
    response_text: str,
    prompt_data: dict,
    body: str
) -> str:
    """
    Generuje angielski prompt FLUX dla jednego panelu tryptyku.
    Używa Groq (szybszy) → DeepSeek fallback.

    panel_index: 1, 2 lub 3
    response_text: pełna odpowiedź Tylera/Sokratesa do adresata
    body: oryginalny email nadawcy
    """
    actor       = style_config.get("actor", "Brad Pitt")
    character   = style_config.get("character", "Tyler Durden")
    base_style  = style_config.get("base_style", "cinematic film still, Fight Club aesthetic")
    quality     = style_config.get("quality_tags", "masterpiece, best quality")
    neg_prompt  = style_config.get("negative_prompt", "anime, cartoon, blurry")
    bubble_style = style_config.get("speech_bubble_style", "hand-drawn speech bubble")
    layout      = panel_config.get("layout", "")

    zasady   = prompt_data.get("zasady_tylera", [])
    manifesty = prompt_data.get("manifesty", [])

    # Wybierz treść chmurki
    if panel_index == 1:
        # Losowa zasada
        if zasady:
            zasada_raw = random.choice(zasady)
            # Skróć do rozsądnej długości dla chmurki
            bubble_text = zasada_raw[:120]
        else:
            bubble_text = "Nie mówi się o tym."
        panel_purpose = "Tyler confronts the viewer with one of his rules"

    elif panel_index == 2:
        # Losowy manifest
        if manifesty:
            manifest = random.choice(manifesty)
            bubble_text = f"{manifest.get('temat', '')}: {manifest.get('tresc', '')}"[:140]
        else:
            bubble_text = "You are not your job."
        panel_purpose = "Tyler delivers a nihilistic manifesto speech"

    else:
        # Panel 3 — rzeczy nadawcy wyrzucane do śmietnika
        # Wyciągamy kluczowe słowa z treści emaila
        bubble_text = f"All of this? In the trash. You don't need any of it."
        panel_purpose = "Tyler throws away objects representing the sender's concerns"

    # System prompt dla modelu generującego prompt FLUX
    system_for_flux = (
        "You are a cinematic visual prompt engineer for FLUX image generation. "
        "You create precise, vivid English prompts for photorealistic movie stills. "
        "Always describe: actor name, character name, exact pose, lighting, background, "
        "speech bubble content and placement. "
        "Output: ONE paragraph, max 120 words, no bullet points, no explanations. "
        "Only the prompt text."
    )

    user_for_flux = (
        f"Create a FLUX image generation prompt for panel {panel_index} of 3 in a triptych.\n\n"
        f"Actor: {actor} as {character} from Fight Club (1999)\n"
        f"Panel purpose: {panel_purpose}\n"
        f"Layout description: {layout}\n"
        f"Speech bubble text (in Polish): \"{bubble_text}\"\n"
        f"Speech bubble visual style: {bubble_style}\n"
        f"Base visual style: {base_style}\n"
        f"Quality tags: {quality}\n"
        f"Negative prompt (do NOT include these): {neg_prompt}\n\n"
        f"Original email context (Polish, for reference only, do NOT translate):\n{body[:500]}\n\n"
        "Write the complete FLUX prompt now:"
    )

    flux_prompt, provider = _call_ai_with_fallback(system_for_flux, user_for_flux, max_tokens=300)

    if not flux_prompt:
        # Hardcoded fallback prompt
        flux_prompt = (
            f"{actor} as {character}, {layout}, "
            f"speech bubble saying '{bubble_text}', "
            f"{base_style}, {quality}"
        )
        provider = "fallback"

    current_app.logger.info("[zwykly-img] Panel %d prompt (%s): %.120s", panel_index, provider, flux_prompt)
    return flux_prompt


# ═══════════════════════════════════════════════════════════════════════════════
# ROTACJA TOKENÓW HF — wzorowane na smierc.py
# ═══════════════════════════════════════════════════════════════════════════════

def _get_hf_tokens() -> list:
    """Pobiera listę tokenów HF (HF_TOKEN, HF_TOKEN1...HF_TOKEN20)."""
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(21)]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


def _png_to_jpg(image_obj: dict, panel_index: int) -> dict:
    """
    Konwertuje PNG (base64) do JPG 95% jakości.
    Nazwa wynikowa: tyler_YYYYMMDD_HHMMSS_panel{N}.jpg
    Zwraca nowy dict z zaktualizowanymi polami base64 / content_type / filename.
    Przy błędzie zwraca oryginał (PNG) żeby nie tracić obrazka.
    """
    try:
        from PIL import Image

        raw_bytes = base64.b64decode(image_obj["base64"])
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=TYLER_JPG_QUALITY, optimize=True)
        jpg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"tyler_{ts}_panel{panel_index}.jpg"

        size_png_kb = len(raw_bytes) // 1024
        size_jpg_kb = len(buf.getvalue()) // 1024

        current_app.logger.info(
            "[tyler-jpg] Panel %d: %dKB PNG → %dKB JPG (jakość=%d%%)",
            panel_index, size_png_kb, size_jpg_kb, TYLER_JPG_QUALITY
        )

        result = {
            "base64":       jpg_b64,
            "content_type": "image/jpeg",
            "filename":     filename,
            "size_jpg":     f"{size_jpg_kb}KB",
            "size_png_orig": f"{size_png_kb}KB",
        }
        # Zachowaj metadata z oryginału
        for key in ("seed", "token_name", "remaining_requests"):
            if key in image_obj:
                result[key] = image_obj[key]
        return result

    except ImportError:
        current_app.logger.error("[tyler-jpg] Pillow niedostępny — zwracam PNG")
        return image_obj
    except Exception as e:
        current_app.logger.warning("[tyler-jpg] Błąd konwersji: %s — zwracam PNG", e)
        return image_obj


def _generate_flux_image(prompt: str, panel_index: int = 0) -> dict | None:
    """
    Generuje jeden obrazek FLUX z losowym seed.
    Próbuje każdy token HF po kolei.
    Zwraca dict z base64 lub None.
    """
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[flux-tyler] Brak tokenów HF!")
        return None

    seed = random.randint(0, 2 ** 32 - 1)
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
            "seed":                seed,
        }
    }

    current_app.logger.info("[flux-tyler] Panel %d — %d tokenów dostępnych, seed=%d",
                            panel_index, len(tokens), seed)

    for name, token in tokens:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept":        "image/png"
        }
        try:
            current_app.logger.info("[flux-tyler] Próbuję token: %s", name)
            resp = requests.post(HF_API_URL, headers=headers, json=payload, timeout=HF_TIMEOUT)

            remaining = resp.headers.get("X-Remaining-Requests")

            if resp.status_code == 200:
                current_app.logger.info(
                    "[flux-tyler] ✓ Token %s: sukces (PNG %d B, pozostało: %s)",
                    name, len(resp.content), remaining or "?"
                )
                return {
                    "base64":       base64.b64encode(resp.content).decode("ascii"),
                    "content_type": "image/png",
                    "filename":     f"tyler_panel{panel_index}_seed{seed}.png",
                    "seed":         seed,
                    "token_name":   name,
                    "remaining_requests": int(remaining) if remaining else None,
                }

            elif resp.status_code in (401, 403):
                current_app.logger.warning("[flux-tyler] ✗ Token %s: nieważny (HTTP %d)",
                                           name, resp.status_code)
            elif resp.status_code in (503, 529):
                current_app.logger.warning("[flux-tyler] ⚠ Token %s: przeciążony (HTTP %d)",
                                           name, resp.status_code)
            else:
                current_app.logger.warning("[flux-tyler] ✗ Token %s: HTTP %d: %s",
                                           name, resp.status_code, resp.text[:100])

        except requests.exceptions.Timeout:
            current_app.logger.warning("[flux-tyler] ⏱ Token %s: timeout (%ds)", name, HF_TIMEOUT)
        except requests.exceptions.ConnectionError as e:
            current_app.logger.warning("[flux-tyler] 🔌 Token %s: connection error: %s", name, str(e)[:80])
        except Exception as e:
            current_app.logger.warning("[flux-tyler] ❌ Token %s: wyjątek: %s", name, str(e)[:80])

    current_app.logger.error("[flux-tyler] Wszystkie tokeny HF zawiodły dla panelu %d", panel_index)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# GENEROWANIE TRYPTYKU
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_triptych(
    response_text: str,
    prompt_data: dict,
    body: str
) -> list:
    """
    Generuje listę obrazków PNG tryptyku (max 3 panele).
    Jeśli tokeny HF wyczerpią się przed końcem — zwraca tyle ile wygenerowano.
    Zwraca listę dict [{base64, content_type, filename, ...}, ...]
    """
    style_config = _load_style_config()
    if not style_config:
        current_app.logger.warning("[zwykly-img] Brak STYLE_CONFIG — pomijam generowanie tryptyku")
        return []

    panels_config = style_config.get("triptych", {}).get("panels", [])
    if not panels_config:
        current_app.logger.warning("[zwykly-img] Brak konfiguracji paneli w STYLE_CONFIG")
        return []

    images = []
    for panel in panels_config:
        idx = panel.get("index", len(images) + 1)

        # Generuj prompt dla panelu
        flux_prompt = _generate_panel_prompt(
            panel_index=idx,
            panel_config=panel,
            style_config=style_config,
            response_text=response_text,
            prompt_data=prompt_data,
            body=body
        )

        # Generuj obrazek
        image = _generate_flux_image(flux_prompt, panel_index=idx)

        if image:
            image = _png_to_jpg(image, panel_index=idx)   # PNG → JPG 95%
            images.append(image)
            current_app.logger.info("[zwykly-img] Panel %d/%d: OK (%s)",
                                    idx, len(panels_config), image.get("filename", "?"))
        else:
            current_app.logger.warning(
                "[zwykly-img] Panel %d/%d: brak — tokeny wyczerpane lub błąd. "
                "Zwracam %d wygenerowanych paneli.",
                idx, len(panels_config), len(images)
            )
            # Jeśli brak tokenów → przerywamy, nie próbujemy kolejnych paneli
            break

    current_app.logger.info("[zwykly-img] Tryptyk: wygenerowano %d/%d paneli",
                            len(images), len(panels_config))
    return images


# ═══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJA
# ═══════════════════════════════════════════════════════════════════════════════

def build_zwykly_section(body: str) -> dict:
    """
    Buduje sekcję 'zwykly' odpowiedzi:

    1. Wczytuje prompt.json i renderuje prompt programowo
    2. Groq PIERWSZY → DeepSeek FALLBACK — generuje odpowiedź Tyler+Sokrates
    3. Parsuje JSON z odpowiedzi: tekst + emocja
    4. Dobiera emotkę PNG i PDF do emocji
    5. Generuje tryptyk PNG (3 panele FLUX Fight Club) — opcjonalnie
    6. Zwraca dict ze wszystkimi elementami

    Nadawca dostaje:
      - reply_html  (pastelowy HTML z odpowiedzią)
      - emoticon    (PNG emocji inline)
      - pdf         (PDF emocji jako załącznik)
      - triptych    (lista max 3 PNG — jeśli tokeny HF dostępne)
    """
    # ── 1. Załaduj i zrenderuj prompt ────────────────────────────────────────
    prompt_data   = _load_prompt_json()
    prompt_str    = _render_prompt(prompt_data, body)

    # System i user dla modelu — cały prompt idzie jako user (jak w oryginale)
    system_msg = prompt_data.get("system", "Odpowiadaj wyłącznie w formacie JSON.")
    user_msg   = prompt_str

    # ── 2. Wywołaj model (Groq → DeepSeek) ───────────────────────────────────
    res_raw, provider = _call_ai_with_fallback(system_msg, user_msg)

    current_app.logger.info("[zwykly] Provider użyty: %s", provider)

    # ── 3. Parsuj odpowiedź ───────────────────────────────────────────────────
    res_text, emotion_key = _parse_response(res_raw)

    if not res_text:
        res_text = (
            "### SOKRATES\n\nPrzepraszam, tym razem słowa do mnie nie przyszły.\n\n"
            "--- Sokrates\n\n---\n\n### TYLER DURDEN\n\n"
            "System zawiódł. Ale to i tak lepiej — maszyny nie powinny za nas myśleć.\n\n"
            "--- Tyler Durden"
        )

    # ── 4. Emotka + PDF ───────────────────────────────────────────────────────
    png_b64, pdf_b64 = _get_emoticon_and_pdf(emotion_key)

    # ── 5. Buduj HTML reply ───────────────────────────────────────────────────
    reply_html = build_html_reply(res_text)

    # ── 6. Tryptyk FLUX ───────────────────────────────────────────────────────
    triptych_images = _generate_triptych(res_text, prompt_data, body)

    current_app.logger.info(
        "[zwykly] OK provider=%s emotion=%s png=%s pdf=%s triptych=%d paneli",
        provider, emotion_key, bool(png_b64), bool(pdf_b64), len(triptych_images)
    )

    # ── 7. Zwróć wszystko ─────────────────────────────────────────────────────
    return {
        "reply_html": reply_html,
        "emoticon": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     f"{emotion_key}.png",
        },
        "pdf": {
            "base64":   pdf_b64,
            "filename": f"{emotion_key}.pdf",
        },
        "detected_emotion":   emotion_key,
        "provider":           provider,
        "triptych":           triptych_images,      # JPG — do emaila (inline + załącznik)
        "triptych_for_drive": triptych_images,      # Ten sam JPG — GAS zapisuje na Drive
    }
