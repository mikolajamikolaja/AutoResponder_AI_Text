"""
responders/biznes.py
Responder biznesowy — Notariusz.
Wykrywa temat notarialny, generuje odpowiedź, dołącza właściwy PDF.
"""
import os
from flask import current_app

from core.ai_client    import call_deepseek, extract_clean_text, sanitize_model_output, MODEL_BIZ
from core.files        import read_file_base64, load_prompt
from core.html_builder import build_html_reply

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR  = os.path.join(BASE_DIR, "pdf_biznes")

# Mapowanie słów kluczowych → nazwa pliku PDF
TOPIC_MAP = {
    "darowizna_mieszkania_lub_domu_obowiazki_podatkowe_i_formalne":  ["darowiz"],
    "dzial_spadku_umowny_krok_po_kroku_z_notariuszem":              ["spad"],
    "intercyza_umowa_majatkowa_malzenska_wyjasnienie_i_koszty":     ["intercyz"],
    "kontakt_godziny_pracy_notariusza_podstawowe_informacje":       ["kontakt", "godzin"],
    "sprzedaz_nieruchomosci_mieszkanie_procedura_koszty_wymagane_dokumenty": ["sprzed", "nieruchom"],
}
FALLBACK_PDF = "kontakt_godziny_pracy_notariusza_podstawowe_informacje"


def detect_topic(body_text: str) -> str:
    """Pyta model o temat notarialny, zwraca klucz z TOPIC_MAP lub 'UNKNOWN'."""
    topics_list = "\n".join(f"- {k}" for k in TOPIC_MAP)
    prompt = (
        "Przeczytaj tekst klienta i rozpoznaj, który z poniższych tematów notarialnych "
        "jest najbardziej odpowiedni. Jeśli nie możesz jednoznacznie przypisać, odpowiedz: UNKNOWN.\n\n"
        f"Tematy:\n{topics_list}\n\n"
        f"Tekst:\n{body_text}\n\nOdpowiedź (jedna etykieta lub UNKNOWN):"
    )
    res = call_deepseek("Detektor tematu notarialnego (jedna etykieta lub UNKNOWN)", prompt, MODEL_BIZ)
    if not res:
        return "UNKNOWN"
    token = res.strip().lower()
    for topic_key, keywords in TOPIC_MAP.items():
        if any(kw in token for kw in keywords):
            return topic_key
    return "UNKNOWN"


def _get_pdf(topic_key: str):
    """Zwraca (pdf_b64, filename) dla tematu, z fallbackiem na kontakt."""
    # UNKNOWN od razu kieruje do pliku kontaktowego
    if topic_key == "UNKNOWN":
        topic_key = FALLBACK_PDF

    pdf_path = os.path.join(PDF_DIR, f"{topic_key}.pdf")
    pdf_b64  = read_file_base64(pdf_path)
    filename = f"{topic_key}.pdf"

    if not pdf_b64:
        current_app.logger.warning("Brak PDF dla %s, próbuję fallback", topic_key)
        pdf_path = os.path.join(PDF_DIR, f"{FALLBACK_PDF}.pdf")
        pdf_b64  = read_file_base64(pdf_path)
        filename = f"{FALLBACK_PDF}.pdf" if pdf_b64 else filename

    return pdf_b64, filename


def build_biznes_section(body: str) -> dict:
    """
    Buduje sekcję 'biznes' odpowiedzi:
    - generuje odpowiedź przez model (prompt_biznesowy.txt)
    - wykrywa temat i dołącza właściwy PDF
    """
    prompt_template = load_prompt(
        "prompt_biznesowy.txt",
        fallback="Jesteś uprzejmym Notariuszem. Przygotuj profesjonalną odpowiedź: {{USER_TEXT}}"
    )
    prompt_for_model = prompt_template.replace("{{USER_TEXT}}", body[:3000])

    res_raw   = call_deepseek(prompt_for_model, "", MODEL_BIZ)
    res_clean = sanitize_model_output(res_raw) if res_raw else ""
    res_text  = extract_clean_text(res_clean)
    if not res_text:
        res_text = "Przepraszam, wystąpił problem z generowaniem odpowiedzi biznesowej."

    topic_key      = detect_topic(body)
    pdf_b64, fname = _get_pdf(topic_key)

    section = {
        "reply_html": build_html_reply(res_text),
        "pdf": {
            "base64":   pdf_b64,
            "filename": fname,
        },
        "topic": topic_key if pdf_b64 else "UNKNOWN",
    }
    if not pdf_b64:
        section["notes"] = "Brak pliku PDF na serwerze; proszę o kontakt."

    return section
