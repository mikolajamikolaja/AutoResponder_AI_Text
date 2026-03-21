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

# reportlab — budowanie PDF CV
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

# ─────────────────────────────────────────────────────────────────────────────
# ŚCIEŻKI
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOTKI_DIR  = os.path.join(BASE_DIR, "emotki")
PDF_DIR     = os.path.join(BASE_DIR, "pdf")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")

PROMPT_JSON_PATH      = os.path.join(PROMPTS_DIR, "zwykly_prompt.json")
CV_CONTENT_JSON_PATH  = os.path.join(PROMPTS_DIR, "zwykly_cv_content.json")
CV_PHOTO_FLUX_PATH    = os.path.join(PROMPTS_DIR, "zwykly_cv_photo_flux.json")
ICON_FLUX_JSON_PATH   = os.path.join(PROMPTS_DIR, "zwykly_icon_flux.json")
STYLE_JS_PATH         = os.path.join(PROMPTS_DIR, "zwykly_obrazek_tyler.js")

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

def _render_prompt(data: dict, body: str, previous_body: str = None) -> str:
    """
    Buduje pełny string promptu z danych prompt.json.
    Obsługuje zarówno stary format (instrukcje/zasady_tylera/manifesty)
    jak i nowy (tyler_zasady_OBOWIAZKOWE / tyler_manifesty_OBOWIAZKOWE).
    Obsługuje previous_body — poprzednią wiadomość od nadawcy.
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

    # ── Poprzednia wiadomość (jeśli dostępna) ─────────────────────────────────
    if previous_body and previous_body.strip():
        lines.append("### POPRZEDNIA WIADOMOŚĆ OD TEJ OSOBY (Tyler i Sokrates MUSZĄ do niej nawiązać):")
        lines.append(previous_body[:500])
        lines.append("")
        # Instrukcja nawiązania z prompt.json
        poprzednia_instr = data.get("tyler_poprzednia_wiadomosc", "")
        if poprzednia_instr:
            lines.append("### INSTRUKCJA NAWIĄZANIA DO POPRZEDNIEJ WIADOMOŚCI:")
            lines.append(poprzednia_instr)
            lines.append("")

    # ── Tekst użytkownika ─────────────────────────────────────────────────────
    lines.append("### OBECNA WIADOMOŚĆ OD NADAWCY (na jej podstawie generuj WSZYSTKO):")
    lines.append(body)
    lines.append("")

    # ── Hard constraints ──────────────────────────────────────────────────────
    hard = data.get("hard_constraints", [])
    if hard:
        lines.append("### BEZWZGLĘDNE ZAKAZY I WYMOGI (naruszenie = błędna odpowiedź):")
        for h in hard:
            lines.append(f"- {h}")
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
        lines.append(f"WYMÓG ZASADA 1=2: {zasady_obj.get('zasada_1_2_identyczne', '')}")
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
        lines.append("### FORMATOWANIE ADRESATA (OBOWIĄZKOWE):")
        lines.append(fmt)
        lines.append("")

    # ── Końcowe przypomnienie ─────────────────────────────────────────────────
    lines.append("### PRZYPOMNIENIE PRZED GENEROWANIEM:")
    lines.append("Każde zdanie Tylera MUSI nawiązywać do konkretnych słów z wiadomości nadawcy.")
    lines.append("ZAKAZ ogólnych rad, coachingu, pozytywnego myślenia, pocieszania.")
    lines.append("ZASADA 1 I ZASADA 2 MUSZĄ BYĆ IDENTYCZNE SŁOWO W SŁOWO.")
    lines.append("ADRESAT: ZAKAZ 'Drogi/Droga' — tylko forma wołacza jak w instrukcji.")
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
def _clean_manifest_labels(text: str) -> str:
    """
    Usuwa etykiety manifestów które model wypisuje mimo zakazu.
    np. "KONSUMPCJONIZM: treść" → "treść"
    """
    if not text:
        return text
    labels = [
        "KONSUMPCJONIZM", "DNO", r"DNO \(Rock Bottom\)",
        r"BÓG/RELIGIA", "BÓG", "RELIGIA",
        "KLASA ROBOTNICZA", r"ŚMIERTELNOŚĆ",
        r"ODPUSZCZENIE \(Let Go\)", "ODPUSZCZENIE",
        "AUTENTYCZNOŚĆ", "ILUZJA BEZPIECZEŃSTWA",
        "HISTORIA", "SAMODOSKONALENIE", "TOŻSAMOŚĆ",
        "RYZYKO", "BUNT", "KONTROLA",
    ]
    pattern = r'^(?:' + '|'.join(labels) + r')\s*:\s*'
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        cleaned.append(re.sub(pattern, '', line, flags=re.IGNORECASE))
    return '\n'.join(cleaned)

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
        tekst  = _clean_manifest_labels(data.get("odpowiedz_tekstowa", "").strip())
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
    "Brad Pitt as Tyler Durden POST-CAR CRASH — blood smeared on forehead, glazed thousand-yard stare, unwashed, split lip, bruised cheekbone, dried blood under nose, greasy matted hair stuck to face, shirtless with soap-burn scars and chemical burns on forearms, cigarette ash on fingers, dark circles under bloodshot eyes, looks like he just walked away from a burning car wreck. NOT handsome. NOT groomed. Raw damage only.",
    "Edward Norton as the Narrator — black eye, torn dirty suit, hollow exhausted gaze, pale skin, hasn't slept in days, blood on shirt collar, hands trembling",
    "Helena Bonham Carter as Marla Singer — dark smoky eyes, tattered vintage dress covered in ash, cigarette burning down to fingers, nihilistic smirk, blood on teeth, unwashed hair matted to face",
    "Meat Loaf as Bob — enormous man with gynecomastia, tearful desperate hollow eyes, oversized stained sweater, sitting on dirty floor surrounded by wreckage",
    "Jared Leto as Angel Face — once-beautiful face now destroyed, blood on perfect teeth, swollen eye, angelic features beaten into ruin",
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
    "releasing steering wheel of speeding car, smiling, oncoming headlights reflected in glazed eyes, motion blur",
    "standing before a burning field, arms spread wide, face turned upward toward black smoke, liberated and destroyed",
    "crouching over terrified ordinary person pinned against dumpster in dark alley, forcing them to confront their meaningless life, steam from pipes, rats visible",
    "laughing maniacally with blood on teeth, surrounded by burning consumer goods",
    "sitting at the bottom of a dark pit, looking up at a tiny square of grey sky, rock bottom",
    "screaming at the camera with veins on neck, face inches from lens, rage and contempt",
    "writing manifesto on wall with bloody knuckles, chemical smoke in background",
    "standing over pile of burning objects from sender's life, pointing finger at camera",
    "walking away from car wreck in slow motion, fire behind, not looking back",
    "reading from burning book in empty church, congregation of rats",
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
    Priorytetyzuje najbardziej agresywne zdania — o Bogu, śmierci, dnie.
    Zwraca dict:
      panel1 — pierwsza zasada (identyczna 1=2)
      panel2 — manifest DNO/BÓG/ŚMIERTELNOŚĆ (priorytet nihilistyczny)
      panel3 — okrzyk końcowy lub ostatnie zdanie Tylera
    """
    if not response_text:
        return {
            "panel1": "Nie mówi się o tym.",
            "panel2": "Bóg cię nie lubi. Prawdopodobnie cię nienawidzi.",
            "panel3": "Puść kierownicę. Pozwól sobie na wypadek.",
        }

    # Wytnij sekcję Tylera
    tyler_section = response_text
    if "### TYLER DURDEN" in response_text:
        tyler_section = response_text.split("### TYLER DURDEN", 1)[1]

    lines = [l.strip() for l in tyler_section.splitlines() if l.strip()]

    # Panel 1 — pierwsza zasada w stylu Fight Club
    panel1 = None
    ordinal_re = re.compile(
        r'^(pierwsza|druga|trzecia|czwarta|pi[aą]ta|sz[oó]sta|si[oó]dma|[oó]sma)\s+zasada',
        re.IGNORECASE
    )
    for line in lines:
        if ordinal_re.match(line):
            panel1 = line[:120]
            break
    if not panel1:
        for line in lines:
            if re.match(r'^[1-8][.)]', line):
                panel1 = re.sub(r'^[1-8][.)]\s*', '', line)[:120]
                break
    if not panel1:
        panel1 = "Pierwsza zasada: nie mówi się o tym."

    # Panel 2 — priorytet: DNO, BÓG, ŚMIERTELNOŚĆ, ODPUSZCZENIE (nihilistyczne)
    panel2 = None
    nihilist_priority = ["DNO", "BÓG", "ŚMIERTELNOŚĆ", "ODPUSZCZENIE", "AUTENTYCZNOŚĆ", "ILUZJA"]
    for priority_word in nihilist_priority:
        for line in lines:
            if line.upper().startswith(priority_word):
                panel2 = line[:140]
                break
        if panel2:
            break
    # fallback: pierwsza linia z CAPS tematem manifestu
    if not panel2:
        for line in lines:
            if re.match(r'^[A-ZŻŹĆĄŚĘÓŁŃ]{4,}[\s:]', line):
                panel2 = line[:140]
                break
    if not panel2:
        for line in lines:
            if line.startswith("- ") and len(line) > 15:
                panel2 = line[2:][:140]
                break
    if not panel2:
        panel2 = "Bóg cię nie lubi. Jesteś niechcianym produktem historii."

    # Panel 3 — okrzyk końcowy lub ostatnie zdanie
    panel3 = None
    for line in lines:
        if "okrzyk" in line.lower():
            panel3 = re.sub(r'^okrzyk[^:]*:\s*', '', line, flags=re.IGNORECASE).strip()[:120]
            break
    if not panel3 and lines:
        for line in reversed(lines):
            if line and not line.startswith("---") and not line.startswith("###") and len(line) > 15:
                panel3 = line[:120]
                break
    if not panel3:
        panel3 = "Puść kierownicę. Pozwól sobie na wypadek."

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

def _generate_icon_flux(body: str, emotion_key: str) -> str | None:
    """
    Generuje emotkę PNG przez FLUX na podstawie treści emaila.
    Używa Groq do wygenerowania promptu, potem FLUX do obrazka.
    Zwraca base64 PNG lub None przy błędzie.
    """
    try:
        with open(ICON_FLUX_JSON_PATH, encoding="utf-8") as f:
            icon_cfg = json.load(f)
    except Exception as e:
        current_app.logger.warning("[icon-flux] Brak zwykly_icon_flux.json: %s", e)
        icon_cfg = {}

    style_base   = icon_cfg.get("style_base", "minimalist black ink sketch, Fight Club zine style")
    neg_prompt   = icon_cfg.get("negative_prompt", "clean, polished, colorful, beautiful, anime")
    system_groq  = icon_cfg.get("system_for_groq", "Generate a short FLUX image prompt based on this email.")
    fallbacks    = icon_cfg.get("fallback_prompts", {})

    icon_prompt = None
    try:
        groq_key = os.getenv("API_KEY_GROQ", "")
        if groq_key:
            headers = {
                "Authorization": f"Bearer {groq_key}",
                "Content-Type":  "application/json",
            }
            payload = {
                "model":    GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_groq},
                    {"role": "user",   "content": f"Email:\n{body[:800]}\nEmocja: {emotion_key}"},
                ],
                "max_tokens":  150,
                "temperature": 0.7,
            }
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=20)
            if resp.status_code == 200:
                icon_prompt = resp.json()["choices"][0]["message"]["content"].strip()
                current_app.logger.info("[icon-flux] Groq prompt: %.100s", icon_prompt)
    except Exception as e:
        current_app.logger.warning("[icon-flux] Groq błąd: %s", e)

    if not icon_prompt:
        icon_prompt = call_deepseek(
            system_groq,
            f"Email:\n{body[:800]}\nEmocja: {emotion_key}",
            MODEL_TYLER,
            timeout=20,
        )

    if not icon_prompt or len(icon_prompt.strip()) < 10:
        icon_prompt = fallbacks.get(emotion_key, fallbacks.get("zlosc", style_base))
        current_app.logger.warning("[icon-flux] Używam fallback promptu dla emocji: %s", emotion_key)

    full_prompt = f"{icon_prompt.strip()}, {style_base}"
    current_app.logger.info("[icon-flux] Pełny prompt: %.150s", full_prompt)

    img = _generate_flux_image(full_prompt, panel_index=99)
    if img and img.get("base64"):
        try:
            from PIL import Image as PILImage
            raw = base64.b64decode(img["base64"])
            pil = PILImage.open(io.BytesIO(raw)).convert("RGB")
            pil = pil.resize((512, 512), PILImage.LANCZOS)
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            current_app.logger.warning("[icon-flux] Błąd resize: %s — zwracam oryginał", e)
            return img["base64"]
    return None


def _generate_cv_content(body: str, previous_body: str | None, sender_email: str) -> dict | None:
    """
    Generuje treść CV w stylu Tylera przez AI (Groq → DeepSeek fallback).
    Zwraca dict z polami CV lub None przy błędzie.
    """
    try:
        with open(CV_CONTENT_JSON_PATH, encoding="utf-8") as f:
            cv_cfg = json.load(f)
    except Exception as e:
        current_app.logger.warning("[cv] Brak zwykly_cv_content.json: %s", e)
        cv_cfg = {}

    system_msg = cv_cfg.get("system", "Generuj prześmiewcze CV w stylu Tylera Durdena. Zwróć TYLKO JSON.")
    schema     = cv_cfg.get("output_schema", {})
    instrukcje = cv_cfg.get("instrukcje_dodatkowe", [])

    context_parts = [f"EMAIL:\n{body[:1500]}"]
    if previous_body and previous_body.strip():
        context_parts.append(f"\nPOPRZEDNIA WIADOMOŚĆ:\n{previous_body[:500]}")
    if sender_email:
        context_parts.append(f"\nEMAIL NADAWCY: {sender_email}")
    context_parts.append(f"\nSCHEMAT JSON DO WYPEŁNIENIA:\n{json.dumps(schema, ensure_ascii=False, indent=2)}")
    if instrukcje:
        context_parts.append(f"\nINSTRUKCJE:\n" + "\n".join(f"- {i}" for i in instrukcje))
    context_parts.append("\nZwróć TYLKO czysty JSON bez żadnego tekstu poza klamrami.")

    user_msg = "\n".join(context_parts)

    raw, _ = _call_ai_with_fallback(system_msg, user_msg, max_tokens=2000)

    if not raw:
        current_app.logger.warning("[cv] Brak odpowiedzi od AI")
        return None

    try:
        clean = raw.strip()
        clean = re.sub(r'^```[a-z]*', '', clean, flags=re.M)
        clean = re.sub(r'```\s*$', '', clean, flags=re.M)
        cv_data = json.loads(clean.strip())
        current_app.logger.info("[cv] CV wygenerowane OK: %s", cv_data.get("imie_nazwisko", "?"))
        return cv_data
    except json.JSONDecodeError as e:
        current_app.logger.warning("[cv] Błąd JSON: %s | raw: %.200s", e, raw)
        return None


def _generate_cv_photo(body: str, cv_data: dict) -> str | None:
    """
    Generuje zdjęcie profilowe do CV przez FLUX.
    Używa Groq do promptu, FLUX do obrazka.
    Zwraca base64 PNG lub None.
    """
    try:
        with open(CV_PHOTO_FLUX_PATH, encoding="utf-8") as f:
            photo_cfg = json.load(f)
    except Exception as e:
        current_app.logger.warning("[cv-photo] Brak zwykly_cv_photo_flux.json: %s", e)
        photo_cfg = {}

    system_groq = photo_cfg.get("system_for_groq", "Generate a FLUX portrait prompt for a CV photo.")
    style_base  = photo_cfg.get("style_base", "professional CV headshot portrait, sharp focus")
    neg_prompt  = photo_cfg.get("negative_prompt", "cartoon, anime, blur, dark")

    imie     = cv_data.get("imie_nazwisko", "unknown person") if cv_data else "unknown person"
    tytul    = cv_data.get("tytul_zawodowy", "") if cv_data else ""
    user_msg = (
        f"Person: {imie}\n"
        f"Job title: {tytul}\n"
        f"Email content (for context on objects to include):\n{body[:600]}"
    )

    photo_prompt = None
    try:
        groq_key = os.getenv("API_KEY_GROQ", "")
        if groq_key:
            headers = {
                "Authorization": f"Bearer {groq_key}",
                "Content-Type":  "application/json",
            }
            payload = {
                "model":    GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_groq},
                    {"role": "user",   "content": user_msg},
                ],
                "max_tokens":  120,
                "temperature": 0.7,
            }
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=20)
            if resp.status_code == 200:
                photo_prompt = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        current_app.logger.warning("[cv-photo] Groq błąd: %s", e)

    if not photo_prompt:
        photo_prompt = call_deepseek(system_groq, user_msg, MODEL_TYLER, timeout=20)

    if not photo_prompt:
        photo_prompt = f"Professional CV headshot portrait, {style_base}"

    full_prompt = f"{photo_prompt.strip()}, {style_base}"
    current_app.logger.info("[cv-photo] Prompt: %.150s", full_prompt)

    img = _generate_flux_image(full_prompt, panel_index=98)
    if img and img.get("base64"):
        try:
            from PIL import Image as PILImage
            raw = base64.b64decode(img["base64"])
            pil = PILImage.open(io.BytesIO(raw)).convert("RGB")
            w, h = pil.size
            side = min(w, h)
            left = (w - side) // 2
            top  = (h - side) // 2
            pil  = pil.crop((left, top, left + side, top + side))
            pil  = pil.resize((300, 300), PILImage.LANCZOS)
            buf  = io.BytesIO()
            pil.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            current_app.logger.warning("[cv-photo] Błąd resize: %s", e)
            return img["base64"]
    return None


def _build_cv_pdf(cv_data: dict, photo_b64: str | None) -> str | None:
    """
    Buduje PDF CV z reportlab z polskimi znakami (UTF-8).
    Zdjęcie w prawym górnym rogu.
    Zwraca base64 PDF lub None przy błędzie.
    """
    try:
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.utils import ImageReader
    except ImportError as e:
        current_app.logger.error("[cv-pdf] Brak reportlab: %s", e)
        return None

    FONT_DIR = os.path.join(BASE_DIR, "fonts")
    FN = "Helvetica"
    FB = "Helvetica-Bold"
    try:
        np_ = os.path.join(FONT_DIR, "DejaVuSans.ttf")
        bp_ = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
        if os.path.exists(np_):
            pdfmetrics.registerFont(TTFont("DejaVuSans", np_))
            FN = "DejaVuSans"
        if os.path.exists(bp_):
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bp_))
            FB = "DejaVuSans-Bold"
    except Exception as e:
        current_app.logger.warning("[cv-pdf] Czcionki: %s — używam Helvetica", e)

    buf = io.BytesIO()
    W, H = A4
    c = rl_canvas.Canvas(buf, pagesize=A4)

    BLACK  = (0.05, 0.05, 0.05)
    DARK   = (0.15, 0.15, 0.15)
    GRAY   = (0.45, 0.45, 0.45)
    LGRAY  = (0.85, 0.85, 0.85)
    RED    = (0.7,  0.1,  0.1)
    WHITE  = (1.0,  1.0,  1.0)

    def set_color(rgb):
        c.setFillColorRGB(*rgb)

    def draw_text(txt, x, y, font=FN, size=10, color=BLACK, max_width=None):
        set_color(color)
        c.setFont(font, size)
        if max_width:
            words = str(txt).split()
            line  = ""
            lines = []
            for w in words:
                test = (line + " " + w).strip()
                if c.stringWidth(test, font, size) <= max_width:
                    line = test
                else:
                    if line:
                        lines.append(line)
                    line = w
            if line:
                lines.append(line)
            for i, ln in enumerate(lines):
                c.drawString(x, y - i * (size + 2), ln)
            return len(lines) * (size + 2)
        else:
            c.drawString(x, y, str(txt))
            return size + 2

    c.setFillColorRGB(*BLACK)
    c.rect(0, H - 45*mm, W, 45*mm, fill=1, stroke=0)

    imie = cv_data.get("imie_nazwisko", "Anonim Bezdomny")
    set_color(WHITE)
    c.setFont(FB, 22)
    c.drawString(15*mm, H - 20*mm, imie)

    tytul = cv_data.get("tytul_zawodowy", "")
    set_color((0.8, 0.8, 0.8))
    c.setFont(FN, 11)
    c.drawString(15*mm, H - 30*mm, tytul)

    email_str = cv_data.get("email", "")
    tel_str   = cv_data.get("telefon", "")
    miasto    = cv_data.get("miasto", "")
    kontakt   = " | ".join(filter(None, [email_str, tel_str, miasto]))
    set_color((0.65, 0.65, 0.65))
    c.setFont(FN, 9)
    c.drawString(15*mm, H - 39*mm, kontakt)

    if photo_b64:
        try:
            photo_bytes = base64.b64decode(photo_b64)
            photo_reader = ImageReader(io.BytesIO(photo_bytes))
            photo_size = 38*mm
            c.drawImage(
                photo_reader,
                W - photo_size - 10*mm,
                H - photo_size - 3.5*mm,
                width=photo_size,
                height=photo_size,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception as e:
            current_app.logger.warning("[cv-pdf] Błąd wklejania zdjęcia: %s", e)

    c.setStrokeColorRGB(*RED)
    c.setLineWidth(2)
    c.line(15*mm, H - 48*mm, W - 15*mm, H - 48*mm)

    y = H - 58*mm
    left_margin  = 15*mm
    right_margin = W - 15*mm
    col_width    = right_margin - left_margin

    def section_header(title, ypos):
        c.setFont(FB, 11)
        c.setFillColorRGB(*RED)
        c.drawString(left_margin, ypos, title.upper())
        c.setStrokeColorRGB(*RED)
        c.setLineWidth(0.5)
        c.line(left_margin, ypos - 2, right_margin, ypos - 2)
        return ypos - 8*mm

    def check_page_break(ypos, needed=20*mm):
        if ypos < needed:
            c.showPage()
            return H - 20*mm
        return ypos

    podsumowanie = cv_data.get("podsumowanie", "")
    if podsumowanie:
        y = section_header("Podsumowanie zawodowe", y)
        c.setFont(FN, 10)
        c.setFillColorRGB(*DARK)
        words = podsumowanie.split()
        line  = ""
        for w in words:
            test = (line + " " + w).strip()
            if c.stringWidth(test, FN, 10) <= col_width:
                line = test
            else:
                c.drawString(left_margin, y, line)
                y -= 5*mm
                line = w
                y = check_page_break(y)
        if line:
            c.drawString(left_margin, y, line)
            y -= 5*mm
        y -= 3*mm

    doswiadczenie = cv_data.get("doswiadczenie", [])
    if doswiadczenie:
        y = check_page_break(y, 40*mm)
        y = section_header("Doświadczenie zawodowe", y)
        for job in doswiadczenie:
            y = check_page_break(y, 30*mm)
            firma      = job.get("firma", "")
            stanowisko = job.get("stanowisko", "")
            okres      = job.get("okres", "")
            obowiazki  = job.get("obowiazki", [])

            c.setFont(FB, 10)
            c.setFillColorRGB(*BLACK)
            c.drawString(left_margin, y, firma)
            c.setFont(FN, 10)
            c.setFillColorRGB(*GRAY)
            c.drawRightString(right_margin, y, okres)
            y -= 5*mm

            c.setFont(FN, 10)
            c.setFillColorRGB(*DARK)
            c.drawString(left_margin + 2*mm, y, stanowisko)
            y -= 5*mm

            c.setFont(FN, 9)
            c.setFillColorRGB(*DARK)
            for ob in obowiazki:
                y = check_page_break(y)
                c.drawString(left_margin + 4*mm, y, f"• {ob}")
                y -= 4.5*mm
            y -= 3*mm

    wyksztalcenie = cv_data.get("wyksztalcenie", [])
    if wyksztalcenie:
        y = check_page_break(y, 25*mm)
        y = section_header("Wykształcenie", y)
        for edu in wyksztalcenie:
            uczelnia = edu.get("uczelnia", "")
            kierunek = edu.get("kierunek", "")
            rok      = edu.get("rok", "")
            c.setFont(FB, 10)
            c.setFillColorRGB(*BLACK)
            c.drawString(left_margin, y, uczelnia)
            c.setFont(FN, 9)
            c.setFillColorRGB(*GRAY)
            c.drawRightString(right_margin, y, str(rok))
            y -= 5*mm
            c.setFont(FN, 10)
            c.setFillColorRGB(*DARK)
            c.drawString(left_margin + 2*mm, y, kierunek)
            y -= 7*mm

    umiejetnosci = cv_data.get("umiejetnosci", [])
    if umiejetnosci:
        y = check_page_break(y, 20*mm)
        y = section_header("Umiejętności", y)
        half = len(umiejetnosci) // 2 + len(umiejetnosci) % 2
        col1 = umiejetnosci[:half]
        col2 = umiejetnosci[half:]
        col_w2 = col_width / 2
        y_start = y
        c.setFont(FN, 9)
        c.setFillColorRGB(*DARK)
        for i, um in enumerate(col1):
            c.drawString(left_margin, y_start - i * 5*mm, f"• {um}")
        for i, um in enumerate(col2):
            c.drawString(left_margin + col_w2, y_start - i * 5*mm, f"• {um}")
        y = y_start - max(len(col1), len(col2)) * 5*mm - 3*mm

    jezyki = cv_data.get("jezyki", [])
    if jezyki:
        y = check_page_break(y, 15*mm)
        y = section_header("Języki", y)
        c.setFont(FN, 9)
        c.setFillColorRGB(*DARK)
        for j in jezyki:
            c.drawString(left_margin, y, f"• {j}")
            y -= 4.5*mm
        y -= 3*mm

    zainteresowania = cv_data.get("zainteresowania", [])
    if zainteresowania:
        y = check_page_break(y, 15*mm)
        y = section_header("Zainteresowania", y)
        c.setFont(FN, 9)
        c.setFillColorRGB(*DARK)
        line_z = " | ".join(zainteresowania)
        c.drawString(left_margin, y, line_z)
        y -= 8*mm

    cytat = cv_data.get("cytat_tylera", "")
    if cytat:
        y = check_page_break(y, 20*mm)
        c.setStrokeColorRGB(*LGRAY)
        c.setLineWidth(0.5)
        c.line(left_margin, y + 3*mm, right_margin, y + 3*mm)
        y -= 3*mm
        c.setFont(FN, 8)
        c.setFillColorRGB(*RED)
        words = cytat.split()
        line  = ""
        for w in words:
            test = (line + " " + w).strip()
            if c.stringWidth(test, FN, 8) <= col_width:
                line = test
            else:
                c.drawString(left_margin, y, f"— {line}")
                y -= 4*mm
                line = w
        if line:
            c.drawString(left_margin, y, f"— {line}")

    c.save()
    pdf_bytes = buf.getvalue()
    current_app.logger.info("[cv-pdf] PDF wygenerowany: %d B", len(pdf_bytes))
    return base64.b64encode(pdf_bytes).decode("ascii")


def build_zwykly_section(body: str, previous_body: str = None, sender_email: str = "") -> dict:
    """
    Buduje sekcję 'zwykly' odpowiedzi:

    1. Wczytuje zwykly_prompt.json i renderuje prompt programowo
    2. Groq PIERWSZY → DeepSeek FALLBACK — generuje odpowiedź Tyler+Sokrates
    3. Parsuje JSON z odpowiedzi: tekst + emocja
    4. Generuje emotkę PNG przez FLUX (zastępuje pliki z dysku)
    5. Generuje CV PDF z zdjęciem FLUX
    6. Generuje tryptyk PNG (3 panele FLUX Fight Club)
    7. Zwraca dict ze wszystkimi elementami

    Nadawca dostaje:
      - reply_html  (HTML z odpowiedzią)
      - emoticon    (PNG emotki generowanej przez FLUX — inline)
      - cv_pdf      (PDF CV w stylu Tylera — załącznik)
      - triptych    (lista max 3 JPG — jeśli tokeny HF dostępne)
    """
    # ── 1. Załaduj i zrenderuj prompt ────────────────────────────────────────
    prompt_data = _load_prompt_json()
    prompt_str  = _render_prompt(prompt_data, body, previous_body)

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
            "— Sokrates\n\n---\n\n### TYLER DURDEN\n\n"
            "System zawiódł. Ale to i tak lepiej — maszyny nie powinny za nas myśleć.\n\n"
            "— Tyler Durden"
        )

    # ── 4. Emotka FLUX (zastępuje pliki z dysku) ──────────────────────────────
    # PDF emocji wyłączony — zastąpiony przez cv_pdf generowany dynamicznie
    png_b64 = _generate_icon_flux(body, emotion_key)
    if not png_b64:
        current_app.logger.warning("[zwykly] FLUX emotka zawiodła — fallback na plik")
        png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{emotion_key}.png"))
        if not png_b64:
            png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{FALLBACK_EMOT}.png"))

    # ── 5. Buduj HTML reply ───────────────────────────────────────────────────
    reply_html = build_html_reply(res_text)

    # ── 6. Tryptyk FLUX ───────────────────────────────────────────────────────
    triptych_images, panel_prompts = _generate_triptych(res_text, prompt_data, body)

    # ── 7. Generuj CV (treść + zdjęcie + PDF) ─────────────────────────────────
    cv_pdf_b64   = None
    cv_data      = None
    cv_photo_b64 = None

    try:
        from concurrent.futures import ThreadPoolExecutor
        from flask import current_app as flask_app
        app_obj = flask_app._get_current_object()

        def gen_cv_content():
            with app_obj.app_context():
                return _generate_cv_content(body, previous_body, sender_email)

        def gen_cv_photo(cv_d):
            with app_obj.app_context():
                return _generate_cv_photo(body, cv_d)

        with ThreadPoolExecutor(max_workers=1) as ex:
            f = ex.submit(gen_cv_content)
            cv_data = f.result(timeout=45)

        if cv_data:
            with ThreadPoolExecutor(max_workers=1) as ex:
                f = ex.submit(gen_cv_photo, cv_data)
                cv_photo_b64 = f.result(timeout=55)

        if cv_data:
            cv_pdf_b64 = _build_cv_pdf(cv_data, cv_photo_b64)

    except Exception as e:
        current_app.logger.error("[zwykly] Błąd generowania CV: %s", e)

    current_app.logger.info(
        "[zwykly] OK provider=%s emotion=%s png=%s cv_pdf=%s tryptyk=%d paneli",
        provider, emotion_key, bool(png_b64), bool(cv_pdf_b64), len(triptych_images)
    )

    # ── 8. Debug TXT do Google Drive ─────────────────────────────────────────
    debug_txt = _build_debug_txt(
        body=body,
        provider=provider,
        emotion_key=emotion_key,
        res_raw=res_raw or "",
        res_text=res_text,
        triptych_images=triptych_images,
        panel_prompts=panel_prompts,
    )

    # ── 9. Zwróć wszystko ─────────────────────────────────────────────────────
    imie_nazwisko = (cv_data.get("imie_nazwisko", "CV") if cv_data else "CV")
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', imie_nazwisko)[:30]

    return {
        "reply_html": reply_html,
        "emoticon": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     f"emotka_{emotion_key}.png",
        },
        "cv_pdf": {
            "base64":       cv_pdf_b64,
            "content_type": "application/pdf",
            "filename":     f"CV_{safe_name}_Tyler.pdf",
        },
        "detected_emotion":   emotion_key,
        "provider":           provider,
        "triptych":           triptych_images,
        "triptych_for_drive": triptych_images,
        "debug_txt":          debug_txt,
    }