"""
responders/zwykly.py
Responder emocjonalny — Tyler Durden.
Wykrywa emocję z JSON zwróconego przez model, generuje odpowiedź tekstową,
dołącza emotkę PNG i PDF.
"""
import os
import re
import json
from flask import current_app

from core.ai_client    import call_deepseek, extract_clean_text, sanitize_model_output, MODEL_TYLER
from core.files        import read_file_base64, load_prompt
from core.html_builder import build_html_reply

# Katalog z emotkami i PDF-ami emocji
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOTKI_DIR = os.path.join(BASE_DIR, "emotki")
PDF_DIR    = os.path.join(BASE_DIR, "pdf")

# Mapowanie: wartości z prompt.txt → nazwy plików w emotki/
EMOCJA_MAP = {
    "radosc":  "twarz_radosc",
    "smutek":  "twarz_smutek",
    "zlosc":   "twarz_zlosc",
    "lek":     "twarz_lek",
    "nuda":    "twarz_nuda",
    "spokoj":  "twarz_spokoj",
}
FALLBACK_EMOT = "error"


def _parse_response(raw: str) -> tuple:
    """
    Parsuje odpowiedź modelu.
    Zwraca (tekst_odpowiedzi, emotion_key).
    Model zwraca JSON z polami: odpowiedz_tekstowa, emocja.
    """
    if not raw:
        return "", FALLBACK_EMOT

    # Wyciągnij blok JSON (czasem model opakowuje w ```json ... ```)
    json_str = raw.strip()
    m = re.search(r'\{.*\}', json_str, re.DOTALL)
    if m:
        json_str = m.group(0)

    try:
        data = json.loads(json_str)
        tekst  = data.get("odpowiedz_tekstowa", "").strip()
        emocja = data.get("emocja", "").strip().lower()

        # Mapuj emocję z prompt.txt na nazwę pliku
        emotion_key = EMOCJA_MAP.get(emocja, FALLBACK_EMOT)

        if not tekst:
            # Fallback: zwróć cały raw jako tekst
            tekst = sanitize_model_output(raw)

        current_app.logger.info("[zwykly] emocja=%s → plik=%s", emocja, emotion_key)
        return tekst, emotion_key

    except Exception as e:
        current_app.logger.warning("[zwykly] Błąd parsowania JSON: %s | raw=%.200s", e, raw)
        # Fallback: cały tekst, domyślna emocja
        return sanitize_model_output(raw), FALLBACK_EMOT


def _get_emoticon_and_pdf(emotion_key: str):
    """Zwraca (png_b64, pdf_b64) dla danej emocji, z fallbackiem na error."""
    png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{emotion_key}.png"))
    pdf_b64 = read_file_base64(os.path.join(PDF_DIR,    f"{emotion_key}.pdf"))

    if not png_b64:
        current_app.logger.warning("[zwykly] Brak PNG dla %s, używam error.png", emotion_key)
        png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{FALLBACK_EMOT}.png"))
    if not pdf_b64:
        current_app.logger.warning("[zwykly] Brak PDF dla %s", emotion_key)
        pdf_b64 = read_file_base64(os.path.join(PDF_DIR, f"{FALLBACK_EMOT}.pdf"))

    return png_b64, pdf_b64


def build_zwykly_section(body: str) -> dict:
    """
    Buduje sekcję 'zwykly' odpowiedzi:
    - generuje odpowiedź przez model (prompt.txt) — model sam wykrywa emocję w JSON
    - parsuje JSON z odpowiedzi: odpowiedz_tekstowa + emocja
    - dołącza emotkę i PDF dopasowany do emocji
    """
    prompt_template = load_prompt(
        "prompt.txt",
        fallback=(
            'Odpowiedz krótko i empatycznie na poniższy tekst.\n'
            'Zwróć JSON: {"odpowiedz_tekstowa": "...", "kategoria_pdf": "...", "emocja": "radosc"}\n'
            'Pole emocja: radosc|smutek|zlosc|lek|nuda|spokoj\n\n'
            'Tekst: {{USER_TEXT}}'
        )
    )
    prompt_for_model = prompt_template.replace("{{USER_TEXT}}", body[:6000])

    res_raw = call_deepseek(prompt_for_model, "", MODEL_TYLER)

    res_text, emotion_key = _parse_response(res_raw)

    if not res_text:
        res_text = "Przepraszam, wystąpił problem z generowaniem odpowiedzi."

    png_b64, pdf_b64 = _get_emoticon_and_pdf(emotion_key)

    current_app.logger.info("[zwykly] OK emotion=%s png=%s pdf=%s",
                            emotion_key, bool(png_b64), bool(pdf_b64))

    return {
        "reply_html": build_html_reply(res_text),
        "emoticon": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     f"{emotion_key}.png",
        },
        "pdf": {
            "base64":   pdf_b64,
            "filename": f"{emotion_key}.pdf",
        },
        "detected_emotion": emotion_key,
    }
