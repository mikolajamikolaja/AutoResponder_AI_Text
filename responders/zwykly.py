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
HF_STEPS     = 2
HF_GUIDANCE  = 2
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
    Obsługuje zarówno stary format (instrukcje/zasady_tylera/manifesty)
    jak i nowy (tyler_zasady_OBOWIAZKOWE / tyler_manifesty_OBOWIAZKOWE).
    """
    lines = []

    # ── System ───────────────────────────────────────────────────────────────
    lines.append(data.get("system", ""))
    lines.append("")

    # ── Schemat wyjściowy ─────────────────────────────────────────────────────
    schema = data.get("output_schema", {})
    if schema:
        lines.append("### SCHEMAT JSON DO WYPEŁNIENIA:")
        lines.append(json.dumps(schema, ensure_ascii=False, indent=2))
        lines.append("")

    # ── Tekst użytkownika ─────────────────────────────────────────────────────
    lines.append("### WIADOMOŚĆ OD NADAWCY (na jej podstawie generuj WSZYSTKO):")
    lines.append(body)
    lines.append("")

    # ── Sokrates ──────────────────────────────────────────────────────────────
    sokrates = (
        data.get("sokrates_instrukcja")
        or data.get("instrukcje_person", {}).get("sokrates")
        or data.get("instrukcje", {}).get("sokrates")
    )
    if sokrates:
        lines.append("### SOKRATES — INSTRUKCJA:")
        lines.append(sokrates)
        lines.append("")

    # ── Tyler — odmowa rekrutacji ─────────────────────────────────────────────
    odmowa = (
        data.get("tyler_odmowa_rekrutacji")
        or data.get("instrukcje_person", {}).get("tyler", {}).get("zasada_rekrutacji")
    )
    if odmowa:
        lines.append("### TYLER — ODMOWA REKRUTACJI (OBOWIĄZKOWE):")
        lines.append(odmowa)
        lines.append("")

    # ── Tyler — zasady (nowy format) ──────────────────────────────────────────
    zasady_obj = data.get("tyler_zasady_OBOWIAZKOWE", {})
    if zasady_obj:
        lines.append("### TYLER — 8 PUNKTÓW/DOGMATÓW (OBOWIĄZKOWE, KONKRETNE):")
        lines.append(zasady_obj.get("opis", ""))
        lines.append(f"FORMAT: {zasady_obj.get('format', '')}")
        lines.append(f"PRZYKŁAD ZŁY:   {zasady_obj.get('przyklad_zly', '')}")
        lines.append(f"PRZYKŁAD DOBRY: {zasady_obj.get('przyklad_dobry', '')}")
        lines.append("")
    else:
        # stary format
        zasady = data.get("zasady_tylera", [])
        inst   = data.get("instrukcje", {})
        nota   = inst.get("zasady_nota", "")
        if zasady:
            lines.append("### ELEMENTY DLA TYLERA (Wpleć w wypowiedź):")
            if nota:
                lines.append(nota)
            for z in zasady:
                lines.append(f"- {z}")
            lines.append("")

    # ── Tyler — manifesty (nowy format) ───────────────────────────────────────
    manifesty_obj = data.get("tyler_manifesty_OBOWIAZKOWE", {})
    if manifesty_obj:
        lines.append("### TYLER — 8 MANIFESTÓW (OBOWIĄZKOWE, KONKRETNE):")
        lines.append(manifesty_obj.get("opis", ""))
        for t in manifesty_obj.get("tematy", []):
            lines.append(f"- {t}")
        lines.append("")
    else:
        # stary format
        manifesty = data.get("manifesty", [])
        if manifesty:
            lines.append("### MANIFESTY TYLERA (Dostosuj i wygłoś każdy):")
            for i, m in enumerate(manifesty, 1):
                lines.append(f"{i}. O {m.get('temat', '???')}: {m.get('tresc', '')}")
            lines.append("")

    # ── Formatowanie adresata ─────────────────────────────────────────────────
    fmt = data.get("formatowanie_adresata", "")
    if fmt:
        lines.append("### FORMATOWANIE ADRESATA:")
        lines.append(fmt)
        lines.append("")

    # ── Końcowe przypomnienie ─────────────────────────────────────────────────
    lines.append("### PRZYPOMNIENIE PRZED GENEROWANIEM:")
    lines.append("Każde zdanie Tylera MUSI nawiązywać do konkretnych słów z wiadomości nadawcy.")
    lines.append("ZAKAZ ogólnych rad, coachingu, pozytywnego myślenia.")
    lines.append("Zwróć WYŁĄCZNIE poprawny JSON bez żadnego tekstu poza klamrami.")
    lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# GROQ — główny model (szybszy), DeepSeek — fallback
# ═══════════════════════════════════════════════════════════════════════════════

def _call_groq(system: str, user: str, max_tokens: int = 6000) -> str | None:
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


def _call_ai_with_fallback(system: str, user: str, max_tokens: int = 6000) -> tuple[str | None, str]:
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

        # Sprawdź czy odpowiedź zawiera sekcję Tylera — jeśli nie, JSON jest niekompletny
        if tekst and "### TYLER DURDEN" not in tekst:
            current_app.logger.warning(
                "[zwykly] Brak sekcji TYLER DURDEN — odpowiedź niekompletna (%.80s)", tekst
            )
            tekst = ""  # wymusi fallback poniżej

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
# KONFIGURACJA POSTACI, STYLÓW, AKCJI
# ═══════════════════════════════════════════════════════════════════════════════

FIGHT_CLUB_CHARACTERS = [
    "Brad Pitt as Tyler Durden — unwashed, split lip, bruised face, greasy matted hair, shirtless with soap-burn scars",
    "Edward Norton as the Narrator — disheveled office worker, black eye, torn suit, exhausted hollow gaze",
    "Helena Bonham Carter as Marla Singer — dark smoky eyes, vintage thrift-store dress, cigarette always in hand, nihilistic smirk",
    "Meat Loaf as Bob — enormous man with gynecomastia, tearful desperate eyes, oversized sweater",
    "Jared Leto as Angel Face — beautiful but battered face, blood on perfect teeth, angelic features destroyed",
]

PANEL_STYLES = [
    "35mm film grain, high contrast, sickly green and amber tones, Fincher cinematography",
    "raw gritty street photography, harsh fluorescent light, 1990s documentary style",
    "extreme chiaroscuro, single bare bulb lighting, deep shadows, industrial decay",
    "handheld camera blur, motion, chaotic energy, smoke and sweat",
    "desaturated noir, cold blue shadows, cracked concrete textures",
    "overexposed bleach bypass, washed out whites, dark crushed blacks",
]

PANEL_ACTIONS = [
    "screaming at the camera with veins on neck",
    "throwing objects violently across the room",
    "laughing maniacally with blood on teeth",
    "grabbing someone by the collar",
    "smoking aggressively, ash falling on clothes",
    "pointing finger directly at viewer with intense rage",
    "sitting on floor surrounded by wreckage, staring into nothing",
    "writing on a wall with bloody knuckles",
    "running through a dark corridor",
    "standing over a pile of burning objects",
]


def _extract_nouns_from_body(body: str) -> list:
    """
    Wyciąga rzeczowniki/konkretne obiekty z treści emaila.
    Szuka słów pisanych z wielkiej litery (imiona, miejsca) oraz
    typowych rzeczowników codziennych.
    Zwraca listę max 6 słów.
    """
    # Słowa które zawsze wyrzucamy (stopwords)
    stopwords = {
        "się", "nie", "jak", "ale", "czy", "też", "już", "aby", "żeby",
        "tego", "tej", "ten", "tak", "jest", "był", "być", "mam", "mieć",
        "to", "i", "w", "z", "na", "do", "po", "za", "od", "przez",
        "że", "co", "gdy", "więc", "bo", "dla", "przy", "nad", "pod",
        "mój", "moja", "moje", "jego", "jej", "ich", "swój", "twój",
        "wszystko", "tylko", "jeszcze", "bardzo", "bardziej", "może",
        "chcę", "musi", "można", "który", "która", "które",
    }
    words = re.findall(r'[A-Za-zżźćńółęąśŻŹĆŃÓŁĘĄŚ]{4,}', body)
    seen = set()
    nouns = []
    for w in words:
        wl = w.lower()
        if wl not in stopwords and wl not in seen:
            seen.add(wl)
            nouns.append(w)
        if len(nouns) >= 6:
            break
    return nouns


def _detect_sender_name(body: str) -> str | None:
    """
    Próbuje wykryć imię nadawcy z treści emaila.
    Szuka podpisu na końcu lub zwrotu do siebie w pierwszej osobie.
    Zwraca imię lub None.
    """
    # Szukaj podpisu: linia z jednym słowem zaczynającym się wielką literą
    # na końcu wiadomości
    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    for line in reversed(lines[-5:]):
        m = re.match(r'^([A-ZŁŻŹĆŃÓĘĄŚ][a-złżźćńóęąś]{2,12})$', line)
        if m:
            return m.group(1)

    # Szukaj "Pozdrawiam, Imię" lub "— Imię"
    m = re.search(
        r'(?:pozdrawiam|pozdrowienia|z poważaniem|regards)[,\s]+([A-ZŁŻŹĆŃÓĘĄŚ][a-złżźćńóęąś]{2,12})',
        body, re.IGNORECASE
    )
    if m:
        return m.group(1)

    m = re.search(r'(?:^|\n)[—–-]\s*([A-ZŁŻŹĆŃÓĘĄŚ][a-złżźćńóęąś]{2,12})', body)
    if m:
        return m.group(1)

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
        new_img.save(buf, format="JPEG", quality=TYLER_JPG_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"tyler_{ts}_panel{panel_index}_txt.jpg"

        result = dict(image_obj)
        result["base64"]   = b64
        result["filename"] = filename
        result["size_jpg"] = f"{len(buf.getvalue()) // 1024}KB"
        result["caption"]  = text
        return result

    except Exception as e:
        current_app.logger.warning("[tyler-txt] Błąd dopisywania tekstu: %s", e)
        return image_obj

# ═══════════════════════════════════════════════════════════════════════════════
# GENEROWANIE PROMPTÓW DLA TRYPTYKU (Groq → DeepSeek fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_tyler_sentences(response_text: str) -> dict:
    """
    Wyciąga gotowe zdania z odpowiedzi Tylera do użycia w dymkach tryptyku.
    Zwraca dict:
      panel1 — pierwsze zdanie z sekcji zasad (1. / dogmat)
      panel2 — pierwsze zdanie z sekcji manifestów (CAPS: ...)
      panel3 — okrzyk końcowy lub ostatnie zdanie Tylera
    Fallbacki są po polsku.
    """
    if not response_text:
        return {
            "panel1": "Nie mówi się o tym.",
            "panel2": "Nie jesteś swoją pracą.",
            "panel3": "To wszystko? Na śmietnik.",
        }

    # Wytnij sekcję Tylera
    tyler_section = response_text
    if "### TYLER DURDEN" in response_text:
        tyler_section = response_text.split("### TYLER DURDEN", 1)[1]

    lines = [l.strip() for l in tyler_section.splitlines() if l.strip()]

    # Panel 1 — pierwsza zasada w stylu Fight Club ("Pierwsza zasada Projektu X: ...")
    panel1 = None
    ordinal_re = re.compile(
        r'^(pierwsza|druga|trzecia|czwarta|pi[aą]ta|sz[oó]sta|si[oó]dma|[oó]sma)\s+zasada',
        re.IGNORECASE
    )
    for line in lines:
        if ordinal_re.match(line):
            panel1 = line[:120]
            break
    # fallback: linia z cyfrą (stary format)
    if not panel1:
        for line in lines:
            if re.match(r'^[1-8][.)]', line):
                panel1 = re.sub(r'^[1-8][.)]\s*', '', line)[:120]
                break
    if not panel1:
        panel1 = "Pierwsza zasada: nie mówi się o tym."

    # Panel 2 — pierwsza linia z CAPS tematem manifestu (np. "KONSUMPCJONIZM:")
    panel2 = None
    for line in lines:
        if re.match(r'^[A-ZŻŹĆĄŚĘÓŁŃ]{4,}[\s:]', line):
            panel2 = line[:140]
            break
    if not panel2:
        # fallback: szukaj linii z myślnikiem (manifest bez CAPS)
        for line in lines:
            if line.startswith("- ") and len(line) > 15:
                panel2 = line[2:][:140]
                break
    if not panel2:
        panel2 = "Nie jesteś swoją pracą."

    # Panel 3 — okrzyk końcowy (szuka "Okrzyk" najpierw, potem ostatnie zdanie)
    panel3 = None
    for line in lines:
        if "okrzyk" in line.lower():
            panel3 = re.sub(r'^okrzyk[^:]*:\s*', '', line, flags=re.IGNORECASE).strip()[:120]
            break
    if not panel3 and lines:
        # Ostatnie niepuste zdanie z sekcji Tylera (nie nagłówek, nie podpis)
        for line in reversed(lines):
            if line and not line.startswith("---") and not line.startswith("###") and len(line) > 15:
                panel3 = line[:120]
                break
    if not panel3:
        panel3 = "To wszystko? Na śmietnik."

    return {"panel1": panel1, "panel2": panel2, "panel3": panel3}


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
    - Losuje postać Fight Club per panel
    - Wykrywa imię nadawcy i wplata jako postać drugoplanową
    - Wyciąga rzeczowniki z emaila jako obiekty w scenie
    - Losuje styl wizualny i akcję
    - Bez dymków — tekst dopisuje Pillow osobno
    """
    base_style   = style_config.get("base_style", "cinematic film still, Fight Club 1999 aesthetic")
    quality      = style_config.get("quality_tags", "photorealistic, raw, gritty")
    neg_prompt   = style_config.get("negative_prompt",
                       "clean, polished, glamorous, beautiful, anime, cartoon, blurry, text, watermark")

    # ── Losuj postać, styl, akcję ─────────────────────────────────────────────
    character    = random.choice(FIGHT_CLUB_CHARACTERS)
    panel_style  = random.choice(PANEL_STYLES)
    action       = random.choice(PANEL_ACTIONS)

    # ── Rzeczowniki z emaila ──────────────────────────────────────────────────
    nouns = _extract_nouns_from_body(body)
    nouns_str = ", ".join(nouns[:4]) if nouns else "debris, trash, broken furniture"

    # ── Imię nadawcy → postać drugoplanowa ───────────────────────────────────
    sender_name = _detect_sender_name(body)
    if sender_name:
        sender_char = (
            f"A Polish woman named {sender_name} is also in the scene — "
            f"ordinary clothes, overwhelmed expression, reacting to the chaos."
        )
    else:
        sender_char = ""

    # ── Cytat Tylera (bez dymka — tylko jako kontekst dla sceny) ─────────────
    tyler_sentences = _extract_tyler_sentences(response_text)
    quote_map = {"1": "panel1", "2": "panel2", "3": "panel3"}
    caption = tyler_sentences.get(quote_map.get(str(panel_index), "panel1"), "")

    system_for_flux = (
        "You are a cinematic visual prompt engineer for FLUX image generation. "
        "Create a raw, gritty, photorealistic movie still. "
        "No speech bubbles, no text in the image — text will be added separately. "
        "Describe: character physical appearance, specific action, environment, objects, lighting. "
        "The character must look damaged, tired, unwashed — NOT clean or handsome. "
        "Output: ONE paragraph, max 120 words, no bullet points. Only the prompt."
    )

    user_for_flux = (
        f"Panel {panel_index} of 3. Fight Club 1999 aesthetic.\n\n"
        f"Main character: {character}\n"
        f"Action: {action}\n"
        f"Objects in scene (from sender email context): {nouns_str}\n"
        f"Visual style: {panel_style}, {base_style}\n"
        f"{sender_char}\n"
        f"Negative: {neg_prompt}\n\n"
        f"The scene should evoke the mood of this quote (do NOT render as text): '{caption}'\n\n"
        "Write the FLUX prompt now:"
    )

    flux_prompt, provider = _call_ai_with_fallback(system_for_flux, user_for_flux, max_tokens=300)

    if not flux_prompt:
        flux_prompt = (
            f"{character}, {action}, surrounded by {nouns_str}, "
            f"{panel_style}, {base_style}, {quality}"
        )

    current_app.logger.info("[zwykly-img] Panel %d prompt (%s): %.120s",
                            panel_index, provider, flux_prompt)
    return flux_prompt, caption


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
        return [], []

    panels_config = style_config.get("triptych", {}).get("panels", [])
    if not panels_config:
        current_app.logger.warning("[zwykly-img] Brak konfiguracji paneli w STYLE_CONFIG")
        return [], []

    images        = []
    panel_prompts = []
    for panel in panels_config:
        idx = panel.get("index", len(images) + 1)

        # Generuj prompt dla panelu (zwraca tuple: prompt + cytat)
        flux_prompt, caption = _generate_panel_prompt(
            panel_index=idx,
            panel_config=panel,
            style_config=style_config,
            response_text=response_text,
            prompt_data=prompt_data,
            body=body
        )
        panel_prompts.append(flux_prompt)

        # Generuj obrazek
        image = _generate_flux_image(flux_prompt, panel_index=idx)

        if image:
            image = _png_to_jpg(image, panel_index=idx)          # PNG → JPG 95%
            image = _add_text_below_image(image, caption, idx)   # dopisz tekst pod obrazkiem
            images.append(image)
            current_app.logger.info("[zwykly-img] Panel %d/%d: OK (%s)",
                                    idx, len(panels_config), image.get("filename", "?"))
        else:
            current_app.logger.warning(
                "[zwykly-img] Panel %d/%d: brak — tokeny wyczerpane lub błąd. "
                "Zwracam %d wygenerowanych paneli.",
                idx, len(panels_config), len(images)
            )
            break

    current_app.logger.info("[zwykly-img] Tryptyk: wygenerowano %d/%d paneli",
                            len(images), len(panels_config))
    return images, panel_prompts


# ═══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJA
# ═══════════════════════════════════════════════════════════════════════════════

def _build_debug_txt(
    body: str,
    provider: str,
    emotion_key: str,
    res_raw: str,
    res_text: str,
    triptych_images: list,
    panel_prompts: list,
) -> dict:
    """
    Buduje plik debug_txt (base64 TXT) do zapisu na Google Drive.
    Zawiera: timestamp, provider, emocja, email nadawcy (fragment),
             surowa odpowiedź modelu, tekstowa odpowiedź, prompty paneli.
    Zwraca dict zgodny z _saveTylerDebugTxt() w GAS:
      {"base64": ..., "content_type": "text/plain", "filename": "..."}
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    lines = [
        f"=== ZWYKLY DEBUG {ts} ===",
        f"provider:   {provider}",
        f"emocja:     {emotion_key}",
        f"panele:     {len(triptych_images)}",
        "",
        "--- BODY (pierwsze 500 znaków) ---",
        (body or "")[:500],
        "",
        "--- RAW MODEL OUTPUT ---",
        (res_raw or "(brak)")[:3000],
        "",
        "--- ODPOWIEDZ TEKSTOWA ---",
        (res_text or "(brak)")[:2000],
        "",
        "--- PROMPTY PANELI ---",
    ]
    for i, p in enumerate(panel_prompts, 1):
        lines.append(f"Panel {i}: {p[:300]}")
    lines.append("")
    lines.append("=== KONIEC ===")

    content = "\n".join(lines)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return {
        "base64":       b64,
        "content_type": "text/plain",
        "filename":     f"zwykly_debug_{ts}.txt",
    }


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
    res_raw, provider = _call_ai_with_fallback(system_msg, user_msg, max_tokens=6000)

    current_app.logger.info("[zwykly] Provider użyty: %s", provider)

    # ── 3. Parsuj odpowiedź ───────────────────────────────────────────────────
    res_text, emotion_key = _parse_response(res_raw)

    # ── 3b. Retry z DeepSeek jeśli odpowiedź niekompletna ────────────────────
    if not res_text and provider == "groq":
        current_app.logger.warning("[zwykly] Groq zwrócił niekompletną odpowiedź — retry DeepSeek")
        res_raw_retry = call_deepseek(system_msg, user_msg, MODEL_TYLER)
        if res_raw_retry:
            res_text, emotion_key = _parse_response(res_raw_retry)
            if res_text:
                provider = "deepseek-retry"
                res_raw  = res_raw_retry
                current_app.logger.info("[zwykly] DeepSeek retry OK")

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
    triptych_images, panel_prompts = _generate_triptych(res_text, prompt_data, body)

    current_app.logger.info(
        "[zwykly] OK provider=%s emotion=%s png=%s pdf=%s triptych=%d paneli",
        provider, emotion_key, bool(png_b64), bool(pdf_b64), len(triptych_images)
    )

    # ── 7. Debug TXT do Google Drive ─────────────────────────────────────────
    debug_txt = _build_debug_txt(
        body=body,
        provider=provider,
        emotion_key=emotion_key,
        res_raw=res_raw or "",
        res_text=res_text,
        triptych_images=triptych_images,
        panel_prompts=panel_prompts,
    )

    # ── 8. Zwróć wszystko ─────────────────────────────────────────────────────
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
        "triptych":           triptych_images,
        "triptych_for_drive": triptych_images,
        "debug_txt":          debug_txt,
    }
