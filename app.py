#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.

Respondery uruchamiane w DWÓCH FALACH równolegle:
  Fala 1 (lekkie — tekst AI): zwykly, biznes, scrabble, nawiazanie
  Fala 2 (ciężkie — obrazy/pliki): obrazek, emocje, analiza, generator_pdf, smierc

Respondery:
  - generator_pdf: Aktywowany przez flagę wants_generator_pdf
  - smierc: Pośmiertny autoresponder — aktywowany przez wants_smierc
    * Wymaga dodatkowych pól: etap, data_smierci, historia
    * Generuje odpowiedzi z zaświatów z progressją etapów
    * Etap 8+: Wysłannik generujący obrazki FLUX

    ZWRACA:
      {
        "reply_html": string,
        "nowy_etap": int,
        "images": [{"base64": ..., "content_type": "image/png", "filename": "..."}],
        "videos": [...],
        "debug_txt": {"base64": ..., "content_type": "text/plain", "filename": "_.txt"}
      }
"""
import os
import base64
import html
import io
import json
import re
import traceback
from flask import Flask, request, jsonify
import requests

from drive_utils import upload_file_to_drive, update_sheet_with_data, save_to_history_sheet
from core.logging_reporter import init_logger, get_logger


from responders.zwykly        import build_zwykly_section
from responders.biznes        import build_biznes_section
from responders.scrabble      import build_scrabble_section
from responders.analiza       import build_analiza_section
from responders.emocje        import build_emocje_section
from responders.nawiazanie    import build_nawiazanie_section
from responders.gif_maker     import make_gif
from responders.generator_pdf import build_generator_pdf_section
from responders.smierc        import build_smierc_section
from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

app = Flask(__name__)


def _run_parallel(tasks: dict, flask_app) -> dict:
    """Uruchamia słownik {klucz: lambda} sekwencyjnie, zwraca {klucz: wynik}."""
    results = {}
    for key, fn in tasks.items():
        try:
            results[key] = fn()
        except Exception as e:
            flask_app.logger.error(
                "Błąd responderu '%s': %s\n%s", key, e, traceback.format_exc()
            )
            results[key] = {}
    return results


def _strip_html_to_text(html_value: str) -> str:
    if not html_value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_value)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _upload_drive_item(item: dict, folder_id: str) -> bool:
    if not isinstance(item, dict) or not item.get("base64") or not item.get("filename"):
        return False
    upload_result = upload_file_to_drive(
        item["base64"],
        item["filename"],
        item.get("content_type", "application/octet-stream"),
        folder_id
    )
    if not upload_result:
        return False
    item["drive_url"] = upload_result["url"]
    item.pop("base64", None)
    return True


def _upload_drive_section_files(section_data: dict, folder_id: str) -> list:
    uploads = []
    if not isinstance(section_data, dict):
        return uploads

    single_fields = [
        "pdf", "emoticon", "cv_pdf", "log_psych", "ankieta_html", "ankieta_pdf",
        "horoskop_pdf", "karta_rpg_pdf", "raport_pdf", "debug_txt", "explanation_txt",
        "plakat_svg", "gra_html", "image", "image2", "prompt1_txt", "prompt2_txt",
    ]
    list_fields = ["triptych", "images", "videos", "docs", "docx_list"]

    for field in single_fields:
        item = section_data.get(field)
        if _upload_drive_item(item, folder_id):
            uploads.append(f"{field}/{item.get('filename')}")

    for field in list_fields:
        arr = section_data.get(field)
        if isinstance(arr, list):
            for item in arr:
                if _upload_drive_item(item, folder_id):
                    uploads.append(f"{field}/{item.get('filename')}")

    return uploads


def _format_log_entry_data(data: object) -> list:
    if data is None:
        return ["  (brak danych)"]
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False, indent=2)}")
            else:
                lines.append(f"  {key}: {value}")
        return lines
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"  - {item}")
        return lines
    return [f"  {data}"]


def _build_log_svg_content(logger) -> str:
    """
    Rozszerzony diagram SVG przebiegu autorespondera.
    Pokazuje: INPUT → DECISIONS → TIMELINE → API CALLS → SECTIONS → OUTPUT
    """
    entries = logger.entries
    if not entries:
        return '''<svg width="1000" height="300" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 300">
  <text x="10" y="150" font-size="16" font-family="Arial">Brak danych logowania</text>
</svg>'''

    # Zbierz informacje z logów
    input_data = next((e for e in entries if e['type'] == 'INPUT'), None)
    api_calls = [e for e in entries if e['type'] == 'API_CALL']
    section_results = [e for e in entries if e['type'] == 'SECTION_RESULT']
    decisions = [e for e in entries if e['type'] == 'DECISION']
    
    # Zlicz API calls
    groq_all = [e for e in api_calls if e['data'].get('api') == 'groq']
    groq_success = sum(1 for e in groq_all if e['data'].get('success'))
    groq_fail = len(groq_all) - groq_success
    
    deepseek_all = [e for e in api_calls if e['data'].get('api') == 'deepseek']
    deepseek_success = sum(1 for e in deepseek_all if e['data'].get('success'))
    deepseek_fail = len(deepseek_all) - deepseek_success
    
    sections_ok = sum(1 for e in section_results if e['data'].get('success'))
    sections_fail = len(section_results) - sections_ok
    
    # Harmonogram czasowy
    first_ts = entries[0].get('timestamp', 0)
    last_ts = entries[-1].get('timestamp', 0)
    total_time = last_ts - first_ts
    
    def escape_xml(text):
        return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;').replace("'", '&apos;').replace('&nbsp;', '&#160;'))
    
    # Wymiary SVG
    num_timeline_items = min(len(entries), 15)
    height = 200 + len(section_results) * 30 + num_timeline_items * 20 + 200
    width = 1200
    
    svg = f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
  <defs>
    <style>
      .box {{ fill: #e8f4f8; stroke: #0066cc; stroke-width: 2; }}
      .success {{ fill: #d4edda; stroke: #28a745; stroke-width: 2; }}
      .error {{ fill: #f8d7da; stroke: #dc3545; stroke-width: 2; }}
      .warning {{ fill: #fff3cd; stroke: #ffc107; stroke-width: 2; }}
      .info {{ fill: #d1ecf1; stroke: #17a2b8; stroke-width: 2; }}
      .text {{ font-family: 'Courier New', monospace; font-size: 12px; }}
      .title {{ font-weight: bold; font-size: 16px; }}
      .subtitle {{ font-size: 11px; fill: #666; }}
      .metric {{ font-size: 11px; font-weight: bold; }}
    </style>
  </defs>
  
  <!-- BACKGROUND -->
  <rect width="{width}" height="{height}" fill="#fafafa" stroke="#ddd" stroke-width="1"/>
  
  <!-- NAGŁÓWEK -->
  <rect x="0" y="0" width="{width}" height="40" fill="#1a1a2e" stroke="none"/>
  <text x="20" y="26" class="title" fill="#e8d5b0">Diagram Przebiegu AutoRespondera</text>
  <text x="{width-400}" y="26" class="subtitle" fill="#aaa">Czas: {total_time:.2f}s | Wpisy: {len(entries)} | Sekcje: {sections_ok}✓ {sections_fail}✗</text>
'''
    
    y_pos = 60
    
    # SEKCJA 1: INPUT
    sender = input_data['data'].get('sender', '?') if input_data else "?"
    subject = input_data['data'].get('subject', '?') if input_data else "?"
    body_preview = (input_data['data'].get('body_preview', '')[:35] + "...") if input_data else ""
    
    svg += f'''  <!-- SEKCJA 1: WEJŚCIE -->
  <rect x="20" y="{y_pos}" width="340" height="110" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📧 WEJŚCIE</text>
  <text x="30" y="{y_pos+42}" class="text">Nadawca: {escape_xml(sender)}</text>
  <text x="30" y="{y_pos+60}" class="text">Temat: {escape_xml(subject[:35])}</text>
  <text x="30" y="{y_pos+78}" class="text">Treść: {escape_xml(body_preview)}</text>
'''
    
    y_pos += 140
    
    # SEKCJA 2: DECYZJE
    if decisions:
        svg += f'''  <!-- SEKCJA 2: DECYZJE PRZEPŁYWU -->
  <rect x="20" y="{y_pos}" width="340" height="{50 + min(len(decisions), 4) * 20}" class="info"/>
  <text x="30" y="{y_pos+20}" class="title">🎯 DECYZJE: {len(decisions)}</text>
'''
        for i, decision in enumerate(decisions[:4]):
            result = decision['data'].get('result', '?')
            decision_text = decision['data'].get('decision', 'N/A')[:25]
            svg += f'  <text x="30" y="{y_pos+40+i*18}" class="text">• {decision_text} → {result}</text>\n'
        
        y_pos += 70 + min(len(decisions), 4) * 20
    
    # SEKCJA 3: STATYSTYKA API
    svg += f'''  <!-- SEKCJA 3: API CALLS STATYSTYKA -->
  <rect x="20" y="{y_pos}" width="1160" height="130" class="{'success' if (groq_success > 0 or deepseek_success > 0) else 'error'}"/>
  <text x="30" y="{y_pos+20}" class="title">⚙️ API CALLS — PRÓBY vs SKUTECZNE</text>
  <rect x="30" y="{y_pos+35}" width="540" height="80" fill="rgba(0,0,0,0.03)" stroke="none"/>
  <text x="40" y="{y_pos+50}" class="metric">GROQ PRÓBY: {len(groq_all)}</text>
  <text x="40" y="{y_pos+68}" class="metric">GROQ SKUTECZNE: {groq_success}</text>
  <text x="40" y="{y_pos+86}" class="metric">GROQ NIEUDANE: {groq_fail}</text>
  <rect x="590" y="{y_pos+35}" width="590" height="80" fill="rgba(0,0,0,0.03)" stroke="none"/>
  <text x="600" y="{y_pos+50}" class="metric">DEEPSEEK PRÓBY: {len(deepseek_all)}</text>
  <text x="600" y="{y_pos+68}" class="metric">DEEPSEEK SKUTECZNE: {deepseek_success}</text>
  <text x="600" y="{y_pos+86}" class="metric">DEEPSEEK NIEUDANE: {deepseek_fail}</text>
'''
    
    y_pos += 160
    
    # SEKCJA 4: HARMONOGRAM CZASOWY
    svg += f'''  <!-- SEKCJA 4: HARMONOGRAM CZASOWY WYKONANIA -->
  <rect x="20" y="{y_pos}" width="1160" height="{60 + num_timeline_items * 20}" class="warning"/>
  <text x="30" y="{y_pos+20}" class="title">⏱️ HARMONOGRAM CZASOWY PIERWSZYCH {num_timeline_items} ETAPÓW</text>
'''
    for i, entry in enumerate(entries[:num_timeline_items]):
        ts = entry.get('timestamp', 0)
        entry_type = entry['type'][:18]
        delta = ts - first_ts
        pct = (delta / total_time * 100) if total_time > 0 else 0
        svg += f'  <rect x="30" y="{y_pos+35+i*20}" width="{pct*8}" height="16" fill="#ffc107" opacity="0.6" stroke="none"/>\n'
        svg += f'  <text x="40" y="{y_pos+47+i*20}" class="text">+{delta:5.2f}s [{entry_type:18s}]</text>\n'
    
    y_pos += 80 + num_timeline_items * 20
    
    # SEKCJA 5: SEKCJE RESPONDENTÓW
    section_details = [(e['data'].get('section'), e['data'].get('success')) for e in section_results]
    svg += f'''  <!-- SEKCJA 5: SEKCJE RESPONDENTÓW -->
  <rect x="20" y="{y_pos}" width="1160" height="{60 + max(len(section_details), 1) * 28}" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📋 SEKCJE RESPONDENTÓW: {sections_ok}✓ {sections_fail}✗</text>
'''
    for i, (section_name, success) in enumerate(section_details):
        box_class = 'success' if success else 'error'
        status = '✓' if success else '✗'
        svg += f'  <rect x="30" y="{y_pos+35+i*28}" width="1140" height="24" class="{box_class}"/>\n'
        svg += f'  <text x="40" y="{y_pos+53+i*28}" class="text">{status} {section_name.upper() if section_name else "UNKNOWN"}</text>\n'
    
    svg += '''  <!-- Arrow marker -->
  <defs>
    <marker id="arrowhead" markerWidth="10" markerHeight="10" refX="5" refY="5" orient="auto">
      <polygon points="0,0 10,5 0,10" fill="#0066cc"/>
    </marker>
  </defs>
</svg>'''
    
    return svg


def _build_log_txt_content(logger, response_data) -> str:
    """
    Generuje tekst loggera z podsumowaniem wykonania.
    """
    # API Calls — liczenie prób i skutecznych
    api_calls = [e for e in logger.entries if e['type'] == 'API_CALL']
    groq_calls = [e for e in api_calls if e['data'].get('api') == 'groq']
    groq_success = sum(1 for e in groq_calls if e['data'].get('success'))
    groq_total = len(groq_calls)
    
    deepseek_calls = [e for e in api_calls if e['data'].get('api') == 'deepseek']
    deepseek_success = sum(1 for e in deepseek_calls if e['data'].get('success'))
    deepseek_total = len(deepseek_calls)
    
    nouns_dict = response_data.get('zwykly', {}).get('nouns_dict', {})
    detected_nouns = []
    if isinstance(nouns_dict, dict):
        detected_nouns = [v for v in nouns_dict.values() if isinstance(v, str) and v.strip()]
    
    # Liczenie sekcji
    section_results = [e for e in logger.entries if e['type'] == 'SECTION_RESULT']
    sections_success = sum(1 for e in section_results if e['data'].get('success'))
    sections_total = len(section_results)

    lines = []
    lines.append("=" * 88)
    lines.append("PODSUMOWANIE WYKONANIA AUTORESPONDERA")
    lines.append("=" * 88)
    lines.append(f"Start: {logger.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Sesja: {logger.session_id}")
    lines.append("")
    
    # Metadane sesji
    lines.append("0. METADANE UŻYTKOWNIKA I SESJI")
    in_history = logger.metadata.get('in_history', 'nieznany')
    in_requiem = logger.metadata.get('in_requiem', 'nieznany')
    keywords_used = logger.metadata.get('keywords_used', False)
    
    lines.append(f"- Status historia: {in_history}")
    lines.append(f"- Status requiem: {in_requiem}")
    lines.append(f"- Słowa kluczowe: {'TAK (keywords użyte)' if keywords_used else 'NIE'}")
    lines.append("")
    
    lines.append("1. STATYSTYKA API CALLS — PRÓBY vs SKUTECZNE")
    groq_success = sum(1 for e in groq_calls if e['data'].get('success'))
    deepseek_success = sum(1 for e in deepseek_calls if e['data'].get('success'))
    groq_total = len(groq_calls)
    deepseek_total = len(deepseek_calls)
    
    if groq_total > 0:
        groq_accuracy = (groq_success / groq_total * 100)
        lines.append(f"- Groq: {groq_total} PRÓB | {groq_success} SKUTECZNYCH (dokładność: {groq_accuracy:.1f}%)")
    else:
        lines.append(f"- Groq: 0 prób")
    
    if deepseek_total > 0:
        deepseek_accuracy = (deepseek_success / deepseek_total * 100)
        lines.append(f"- DeepSeek: {deepseek_total} PRÓB | {deepseek_success} SKUTECZNYCH (dokładność: {deepseek_accuracy:.1f}%)")
    else:
        lines.append(f"- DeepSeek: 0 prób")
    
    lines.append(f"- RAZEM API CALLS: {len(api_calls)}")
    lines.append("")
    lines.append("2. SEKCJE RESPONDENTÓW — REALIZACJA")
    lines.append(f"- Sekcje uruchomione: {sections_total}")
    lines.append(f"- Sekcje pomyślne: {sections_success}")
    if sections_total > 0:
        success_rate = (sections_success / sections_total * 100)
        lines.append(f"- Współczynnik sukcesu: {success_rate:.1f}%")
    lines.append("")
    lines.append("3. PRZEFILTROWANA LISTA SEKCJI")
    section_keys = [k for k in response_data.keys() if k not in ("log_txt", "log_svg", "log")]
    if section_keys:
        for section_name in sorted(section_keys):
            section_data = response_data.get(section_name, {})
            has_html = bool(section_data.get('reply_html', '').strip())
            has_attachments = len(section_data.get('docx_list', [])) > 0 or len(section_data.get('images', [])) > 0
            status = '✓' if (has_html or has_attachments) else '✗'
            lines.append(f"  {status} {section_name.upper()}")
            if has_html:
                lines.append(f"      - HTML: {len(section_data.get('reply_html', ''))} znaków")
            if has_attachments:
                lines.append(f"      - Załączniki: {len(section_data.get('docx_list', []))} + {len(section_data.get('images', []))} obrazów")
    else:
        lines.append("  (brak wygenerowanych sekcji)")
    lines.append("")
    
    lines.append("4. HARMONOGRAM CZASOWY WYKONANIA")
    if logger.entries:
        first_ts = logger.entries[0].get('timestamp', 0)
        last_ts = logger.entries[-1].get('timestamp', 0)
        total_time = last_ts - first_ts
        lines.append(f"- Czas całkowity: {total_time:.2f}s")
        lines.append("- Pierwsze 10 etapów:")
        for i, entry in enumerate(logger.entries[:10]):
            ts = entry.get('timestamp', 0)
            delta = ts - first_ts
            type_str = entry['type'][:20].ljust(20)
            lines.append(f"  [{i+1:2d}] +{delta:6.2f}s: {type_str}")
    lines.append("")
    
    lines.append("5. SZCZEGÓŁOWE WPISY LOGGERA")
    for entry in logger.entries:
        timestamp = entry.get('timestamp', 0.0)
        lines.append(f"[{entry['type']}] +{timestamp:.2f}s")
        lines.extend(_format_log_entry_data(entry.get('data')))
        lines.append("")

    if detected_nouns:
        lines.append("6. WYEKSTRAHOWANE RZECZOWNIKI")
        for noun in detected_nouns:
            lines.append(f"- {noun}")
        lines.append("")
    
    lines.append("7. WNIOSKI I PODSUMOWANIE")
    sections_rate = (sections_success / sections_total * 100) if sections_total > 0 else 0
    if sections_rate == 100:
        lines.append("- Status: ✓ SUCCESS — Wszystkie sekcje wygenerowane pomyślnie!")
    elif sections_rate >= 75:
        lines.append(f"- Status: ✓ DOBRY — Wykonanie prawie bez problemów ({sections_rate:.0f}% sukcesu)")
    elif sections_rate >= 50:
        lines.append(f"- Status: ⚠ ŚREDNI — Napotkane problemy (tylko {sections_rate:.0f}% sekcji pomyślnych)")
    else:
        lines.append(f"- Status: ✗ ZŁY — Znaczne problemy ({sections_rate:.0f}% sekcji pomyślnych)")
    
    if groq_total > 0:
        lines.append(f"- Groq: {(groq_success/groq_total*100):.0f}% odpowiedzi było skutecznych")
    if deepseek_total > 0:
        lines.append(f"- DeepSeek: {(deepseek_success/deepseek_total*100):.0f}% odpowiedzi było skutecznych")
    if keywords_used:
        lines.append("- ⓘ KEYWORDS_TEST był aktywny — FLUX wyrwany")
    lines.append("")

    lines.append("=" * 88)
    lines.append("KONIEC PODSUMOWANIA")
    lines.append("=" * 88)
    return "\n".join(lines)



@app.route("/webhook", methods=["POST"])
def webhook():
    # ── Pobranie session ID z RENDER_INSTANCE_ID lub generowanie nowego ──────
    render_instance_id = os.getenv("RENDER_INSTANCE_ID", "")
    session_id = render_instance_id if render_instance_id else None
    
    # Inicjalizuj logger dla tego żądania z session ID
    logger = init_logger(session_id=session_id)
    
    data = request.json or {}
    body = data.get("body", "")

    if not body or not body.strip():
        logger.log_decision("empty_body_check", "body.strip() == ''", False)
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # Loguj oryginalną wiadomość
    sender = data.get("sender", "")
    sender_name = data.get("sender_name", "")
    subject = data.get("subject", "")
    logger.log_input(sender, subject, body, sender_name)

    # ── Pola nadawcy i historia ───────────────────────────────────────────────
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None
    attachments      = data.get("attachments")      or []
    save_to_drive    = bool(data.get("save_to_drive"))
    test_mode        = bool(data.get("test_mode"))
    # ── KEYWORDS_TEST (disable_flux) ──────────────────────────────────────────
    # KEYWORDS_TEST to parametr flagi do ZABLOKOWANIA generowania obrazków FLUX
    # w konkretnym responderycie, ale NIE zmienia logikę którzy respondenci się
    # uruchamiają ani nie wpływa na zapis do historii/drive.
    # Każdy responder dostaje disable_flux i sam decyduje czy generować Flux czy nie.
    disable_flux     = bool(data.get("disable_flux"))  # disable_flux=True ⟷ wyłącz FLUX w tym requestzie
    retry_responders = data.get("retry_responders") or []
    attempt_count    = int(data.get("attempt_count", 1)) if data.get("attempt_count") else 1
    skip_save_to_history = bool(data.get("skip_save_to_history"))
    
    # ── KEYWORDS_TEST (disable_flux) ──────────────────────────────────────────
    # KEYWORDS_TEST - parametr aby wyłączyć generowanie FLUX (obrazków) w respondericach.
    # Pochodzi z GAS script gdy wiadomość zawiera słowo z listy KEYWORDS_TEST.
    # disable_flux=True przesilany jako test_mode do respondentów. Respondenci którzy
    # generują Flux (zwykly, emocje, itp) sprawdzają ten parametr i wy generowanie.
    # WAŻNE: disable_flux NIE wpływa na zapis do historii, Drive, ani które respondenci
    # się uruchamiają — TYLKO na to czy generować Flux czy nie.
    keywords_used = False
    if disable_flux:
        keywords_used = True
        logger.set_metadata("keywords_used", True)
        logger.log_decision("disable_flux", "disable_flux=True", "FLUX wyłączony dla tego requestu")

    # Loguj zmienne
    logger.log_variables_detected({
        "sender": sender,
        "sender_name": sender_name,
        "has_previous_body": bool(previous_body),
        "has_previous_subject": bool(previous_subject),
        "num_attachments": len(attachments),
        "save_to_drive": save_to_drive,
        "test_mode": test_mode,
        "disable_flux": disable_flux,
        "is_retry": bool(retry_responders),
        "attempt_count": attempt_count,
        "skip_save_to_history": skip_save_to_history,
    })

    # ── Konfiguracja Drive ───────────────────────────────────────────────────
    drive_folder_id = os.getenv("DRIVE_FOLDER_ID")
    smierc_sheet_id = os.getenv("SMIERC_HISTORY_SHEET_ID")
    history_sheet_id = os.getenv("HISTORY_SHEET_ID")
    
    # ── Sprawdzenie statusu użytkownika ───────────────────────────────────────
    from drive_utils import check_user_in_sheet
    if not test_mode:  # Nie sprawdzaj użytkownika dla testów
        in_history_status = "tak" if check_user_in_sheet(history_sheet_id, sender) else "nie"
        in_requiem_status = "tak" if check_user_in_sheet(smierc_sheet_id, sender) else "nie"
    else:
        in_history_status = "test_mode"
        in_requiem_status = "test_mode"
    logger.set_metadata("in_history", in_history_status)
    logger.set_metadata("in_requiem", in_requiem_status)

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble      = bool(data.get("wants_scrabble"))
    wants_analiza       = bool(data.get("wants_analiza"))
    wants_emocje        = bool(data.get("wants_emocje"))
    wants_generator_pdf = bool(data.get("wants_generator_pdf"))
    wants_smierc        = bool(data.get("wants_smierc"))
    wants_text_reply    = bool(data.get("wants_text_reply", True))
    wants_nawiazanie    = bool(previous_body or previous_subject)
    is_retry            = bool(retry_responders)

    flask_app = app

    def run(fn, *args, **kwargs):
        with flask_app.app_context():
            return fn(*args, **kwargs)

    # ── FALA 1: lekkie respondery ─────────────────────────────────────────────
    requested_sections = set(retry_responders) if is_retry else set()
    if not is_retry:
        has_special_responder = wants_scrabble or wants_analiza or wants_emocje or wants_generator_pdf or wants_smierc
        if wants_text_reply and not has_special_responder:
            requested_sections.update(["zwykly", "biznes"])
            logger.log_decision("text_reply_decision", "wants_text_reply=True and no special responder", "Dodaję zwykly/biznes")
        elif has_special_responder:
            logger.log_decision(
                "text_reply_decision",
                f"special responders active: scrabble={wants_scrabble}, analiza={wants_analiza}, emocje={wants_emocje}, generator_pdf={wants_generator_pdf}, smierc={wants_smierc}",
                "Nie dodaję zwykly/biznes, specjalny responder obsłuży odpowiedź"
            )

        if wants_scrabble:
            requested_sections.add("scrabble")
        if wants_analiza:
            requested_sections.add("analiza")
        if wants_emocje:
            requested_sections.add("emocje")
        if wants_generator_pdf:
            requested_sections.add("generator_pdf")
        if wants_nawiazanie:
            requested_sections.add("nawiazanie")

    wave1 = {}
    if "zwykly" in requested_sections:
        _prev   = previous_body
        _sender = sender
        _sname  = sender_name
        # ── test_mode ze KEYWORDS_TEST (disable_flux) ──────────────────────────
        # Jeśli disable_flux=True (z KEYWORDS_TEST w mailu), to zwykly.py
        # dostanie test_mode=True i wy generowanie Flux (będzie zastępczy obrazek)
        wave1["zwykly"] = lambda: run(
            build_zwykly_section,
            body,
            _prev,
            _sender,
            _sname,
            test_mode=disable_flux or test_mode,
            attachments=attachments,
        )
    if "biznes" in requested_sections:
        wave1["biznes"] = lambda: run(build_biznes_section, body)
    if "scrabble" in requested_sections:
        wave1["scrabble"] = lambda: run(build_scrabble_section, body)
    if "nawiazanie" in requested_sections:
        wave1["nawiazanie"] = lambda: run(
            build_nawiazanie_section,
            body=body,
            previous_body=previous_body,
            previous_subject=previous_subject,
            sender=sender,
            sender_name=sender_name,
        )

    response_data = _run_parallel(wave1, flask_app)

    has_wave2 = False
    if is_retry:
        has_wave2 = bool({"obrazek", "emocje", "analiza", "generator_pdf", "smierc"} & requested_sections)
    else:
        has_wave2 = bool(wants_emocje or wants_analiza or wants_generator_pdf or wants_smierc)

    # ── ZAWSZE generuj logi po Fali 1 ───────────────────────────────────────
    log_txt_content = _build_log_txt_content(logger, response_data)
    log_txt_b64 = base64.b64encode(log_txt_content.encode('utf-8')).decode('utf-8')
    response_data['log_txt'] = {'base64': log_txt_b64, 'content_type': 'text/plain', 'filename': 'log.txt'}
    
    svg_content = _build_log_svg_content(logger)
    log_svg_b64 = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
    response_data['log_svg'] = {'base64': log_svg_b64, 'content_type': 'image/svg+xml', 'filename': 'log.svg'}

    # ── WYSYŁKA PO FALI 1 ─────────────────────────────────────────────────────
    # Wyślij wszystkie respondery które zostały wygenerowane
    html_fala1 = "".join(filter(None, [
        response_data.get("zwykly",     {}).get("reply_html", ""),
        response_data.get("biznes",     {}).get("reply_html", ""),
        response_data.get("analiza",    {}).get("reply_html", ""),
        response_data.get("emocje",     {}).get("reply_html", ""),
        response_data.get("nawiazanie", {}).get("reply_html", ""),
        response_data.get("scrabble",   {}).get("reply_html", ""),
        response_data.get("generator_pdf", {}).get("reply_html", ""),
    ]))
    zalaczniki_fala1 = zbierz_zalaczniki_z_response(
        {k: response_data[k] for k in ("zwykly", "biznes", "scrabble", "analiza", "emocje", "generator_pdf", "log", "log_txt", "log_svg")
         if k in response_data}
    )
    if html_fala1.strip() and ("zwykly" in requested_sections or "biznes" in requested_sections or "nawiazanie" in requested_sections or "analiza" in requested_sections or "emocje" in requested_sections or "scrabble" in requested_sections or "generator_pdf" in requested_sections):
        success = wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'}",
            html_body  = html_fala1,
            zalaczniki = zalaczniki_fala1,
        )
        if success and history_sheet_id and not skip_save_to_history:
            save_to_history_sheet(
                history_sheet_id,
                sender,
                f"Re: {previous_subject or 'Twoja wiadomość'}",
                _strip_html_to_text(html_fala1)[:1000],
                is_response=True,
            )
    elif zalaczniki_fala1:
        # Wysyłka tylko załączników, jeśli nie ma tekstu
        success = wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'} (załączniki)",
            html_body  = "<p>Załączniki.</p>",
            zalaczniki = zalaczniki_fala1,
        )
        if success and history_sheet_id and not skip_save_to_history:
            save_to_history_sheet(history_sheet_id, sender, f"Re: {previous_subject or 'Twoja wiadomość'} (załączniki)", "Załączniki", is_response=True)

    # ── FALA 2: ciężkie respondery ────────────────────────────────────────────
    wave2 = {}
    if is_retry:
        # ── KEYWORDS_TEST (disable_flux) ──────────────────────────────────────
        # Przesyłam test_mode=disable_flux do wave2 respondentów także w retry,
        # aby respondenci wiedzieli że mają wy generowanie FLUX
        if "emocje" in requested_sections:
            wave2["emocje"]  = lambda: run(build_emocje_section, body, attachments, test_mode=disable_flux or test_mode)
        if "analiza" in requested_sections:
            wave2["analiza"] = lambda: run(
                build_analiza_section,
                body,
                attachments,
                sender=sender,
                sender_name=sender_name,
                test_mode=disable_flux or test_mode,
            )
        if "generator_pdf" in requested_sections:
            _sn   = sender_name
            _body = body
            wave2["generator_pdf"] = lambda: run(
                build_generator_pdf_section, _body, sender_name=_sn, test_mode=disable_flux or test_mode
            )
        if "smierc" in requested_sections:
            _sender       = sender
            _body_smierc  = body
            _etap         = data.get("etap", 1)
            _data_smierci = data.get("data_smierci", "nieznanego dnia")
            _historia     = data.get("historia", [])
            wave2["smierc"] = lambda: run(
                build_smierc_section,
                sender_email=_sender,
                body=_body_smierc,
                etap=_etap,
                data_smierci_str=_data_smierci,
                historia=_historia,
                test_mode=disable_flux or test_mode,  # KEYWORDS_TEST (disable_flux) → test_mode dla FLUX
            )
    else:
        if wants_emocje:
            # disable_flux ze KEYWORDS_TEST blokuje FLUX w responderycie
            wave2["emocje"]  = lambda: run(build_emocje_section, body, attachments, test_mode=disable_flux or test_mode)
        if wants_analiza:
            wave2["analiza"] = lambda: run(
                build_analiza_section,
                body,
                attachments,
                sender=sender,
                sender_name=sender_name,
                # disable_flux z KEYWORDS_TEST przekazywany jako test_mode
                test_mode=disable_flux or test_mode,
            )
        if wants_generator_pdf:
            _sn   = sender_name
            _body = body
            # disable_flux: jeśli KEYWORDS_TEST, wyłącz Flux w generator_pdf
            wave2["generator_pdf"] = lambda: run(
                build_generator_pdf_section, _body, sender_name=_sn, test_mode=disable_flux or test_mode
            )
        if wants_smierc:
            _sender       = sender
            _body_smierc  = body
            _etap         = data.get("etap", 1)
            _data_smierci = data.get("data_smierci", "nieznanego dnia")
            _historia     = data.get("historia", [])
            wave2["smierc"] = lambda: run(
                build_smierc_section,
                sender_email=_sender,
                body=_body_smierc,
                etap=_etap,
                data_smierci_str=_data_smierci,
                historia=_historia,
                test_mode=disable_flux or test_mode,  # KEYWORDS_TEST (disable_flux) → test_mode dla FLUX
            )

    if wave2:
        response_data.update(_run_parallel(wave2, flask_app))

        # ── Generuj logi ──────────────────────────────────────────────────────
        log_txt_content = _build_log_txt_content(logger, response_data)
        log_txt_b64 = base64.b64encode(log_txt_content.encode('utf-8')).decode('utf-8')
        response_data['log_txt'] = {'base64': log_txt_b64, 'content_type': 'text/plain', 'filename': 'log.txt'}

        svg_content = _build_log_svg_content(logger)
        log_svg_b64 = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
        response_data['log_svg'] = {'base64': log_svg_b64, 'content_type': 'image/svg+xml', 'filename': 'log.svg'}

        # ── WYSYŁKA PO FALI 2 ─────────────────────────────────────────────────
        html_fala2 = "".join(filter(None, [
            response_data.get("obrazek",       {}).get("reply_html", ""),
            response_data.get("emocje",        {}).get("reply_html", ""),
            response_data.get("analiza",       {}).get("reply_html", ""),
            response_data.get("generator_pdf", {}).get("reply_html", ""),
            response_data.get("smierc",        {}).get("reply_html", ""),
        ]))
        success_fala2 = wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'} (część 2)",
            html_body  = html_fala2 or "<p>Załączniki z drugiej fali.</p>",
            zalaczniki = zbierz_zalaczniki_z_response(
                {k: response_data[k] for k in
                 ("obrazek", "emocje", "analiza", "generator_pdf", "smierc", "log", "log_txt", "log_svg")
                 if k in response_data}
            ),
        )
        if success_fala2 and history_sheet_id and not skip_save_to_history:
            save_to_history_sheet(
                history_sheet_id,
                sender,
                f"Re: {previous_subject or 'Twoja wiadomość'} (część 2)",
                _strip_html_to_text(html_fala2 or "Załączniki")[:1000],
                is_response=True,
            )

    # Zabezpieczenie — nawiazanie zawsze ma has_history
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {
            "has_history": False, "reply_html": "", "analysis": ""
        }

    def section_success(key: str, value) -> bool:
        if not value or not isinstance(value, dict):
            return False
        if key == "nawiazanie":
            return bool(value.get("has_history") or value.get("reply_html"))
        return bool(value)

    # Użyj już zdefiniowanego requested_sections (z Fali 1)
    # Nie buduj go znowu!

    failed_sections = [key for key in requested_sections if not section_success(key, response_data.get(key))]
    if failed_sections:
        response_data["processed_status"] = {
            "status": "partial",
            "failed": failed_sections,
            "attempt_count": attempt_count,
            "details": {
                key: response_data.get(key) or "no_data_returned"
                for key in failed_sections
            }
        }
    else:
        response_data["processed_status"] = {"status": "ok"}

    # ── Zapis do Google Drive jeśli włączone ──────────────────────────────────
    if save_to_drive and drive_folder_id:
        drive_uploads = []

        # Top-level pliki wynikowe
        for top_field in ("log_txt", "log_svg"):
            file_obj = response_data.get(top_field)
            if isinstance(file_obj, dict) and file_obj.get("base64") and file_obj.get("filename"):
                if _upload_drive_item(file_obj, drive_folder_id):
                    drive_uploads.append(f"{top_field}/{file_obj['filename']}")

        # Sekcje responderów
        for key, value in response_data.items():
            if not isinstance(value, dict):
                continue
            section_uploads = _upload_drive_section_files(value, drive_folder_id)
            drive_uploads.extend([f"{key}/{name}" for name in section_uploads])

        if drive_uploads:
            app.logger.info(f"Zapisano do Drive: {', '.join(drive_uploads)}")
        response_data["saved_to_drive"] = True

    # ── Aktualizacja arkusza śmierci jeśli potrzebne ──────────────────────────
    if smierc_sheet_id and "smierc" in response_data and response_data["smierc"]:
        smierc_data = response_data["smierc"]
        if "nowy_etap" in smierc_data:
            # Przykład: zaktualizuj arkusz z nowym etapem
            # Zakładamy format: email w nazwie zakładki, dane w kolumnach
            # To wymaga dostosowania do Twojej struktury arkusza
            try:
                # Przykładowa aktualizacja — dostosuj do potrzeb
                range_name = f"{sender.replace('@', '_').replace('.', '_')}!A{smierc_data['nowy_etap'] + 1}"
                values = [[smierc_data["nowy_etap"], "", body[:2000], _strip_html_to_text(smierc_data.get("reply_html", ""))[:2000], ""]]
                update_sheet_with_data(smierc_sheet_id, range_name, values)
                app.logger.info(f"Zaktualizowano arkusz śmierci dla {sender}, etap {smierc_data['nowy_etap']}")
            except Exception as e:
                app.logger.error(f"Błąd aktualizacji arkusza śmierci: {e}")

    # ── Zapis do arkusza historii ─────────────────────────────────────────────
    skip_save_to_history = bool(data.get("skip_save_to_history"))
    if history_sheet_id and not skip_save_to_history:
        success = save_to_history_sheet(history_sheet_id, sender, data.get("subject", ""), body)
        if not success:
            app.logger.error(f"Błąd zapisu do arkusza historii dla {sender}")

    # ── Logowanie ─────────────────────────────────────────────────────────────
    smierc_data        = response_data.get("smierc", {})
    smierc_images_cnt  = (
        len(smierc_data.get("images", []))
        if isinstance(smierc_data, dict) and isinstance(smierc_data.get("images"), list)
        else 0
    )
    debug_txt         = smierc_data.get("debug_txt", {}) if isinstance(smierc_data, dict) else {}
    smierc_has_debug  = bool(debug_txt.get("base64") if isinstance(debug_txt, dict) else False)

    app.logger.info(
        "Response: biznes=%s | zwykly=%s | scrabble=%s | analiza=%s | emocje=%s "
        "| obrazek=%s | nawiazanie=%s | generator_pdf=%s | smierc=%s "
        "(images=%d, debug=%s) | sender=%s",
        bool(response_data.get("biznes",    {}).get("pdf",  {}).get("base64")),
        bool(response_data.get("zwykly",    {}).get("pdf",  {}).get("base64")),
        "tak" if "scrabble"       in response_data else "nie",
        "tak" if "analiza"        in response_data else "nie",
        "tak" if "emocje"         in response_data else "nie",
        "tak" if "obrazek"        in response_data else "nie",
        "tak" if response_data.get("nawiazanie", {}).get("has_history") else "nie",
        "tak" if response_data.get("generator_pdf", {}).get("pdf") else "nie",
        "tak" if "smierc"         in response_data else "nie",
        smierc_images_cnt,
        "tak" if smierc_has_debug else "nie",
        sender_name or sender or "(brak)",
    )

    # Finalizuj logger
    logger.finalize()

    return jsonify(response_data), 200


@app.route("/webhook_gif", methods=["POST"])
def webhook_gif():
    """Przyjmuje dwa PNG jako base64, zwraca dwa GIFy jako base64."""
    data     = request.json or {}
    png1_b64 = data.get("png1_base64")
    png2_b64 = data.get("png2_base64")

    if not png1_b64 and not png2_b64:
        return jsonify({"error": "Brak png1_base64 i png2_base64"}), 400

    app.logger.info("/webhook_gif — odebrano PNG: png1=%s png2=%s",
                    bool(png1_b64), bool(png2_b64))

    def gen_gif1():
        return make_gif(png1_b64) if png1_b64 else None

    def gen_gif2():
        return make_gif(png2_b64) if png2_b64 else None

    gif1_b64 = gen_gif1()
    gif2_b64 = gen_gif2()

    app.logger.info("/webhook_gif — GIFy: gif1=%s gif2=%s",
                    bool(gif1_b64), bool(gif2_b64))

    return jsonify({
        "gif1": {
            "base64":       gif1_b64,
            "content_type": "image/gif",
            "filename":     "komiks_ai.gif",
        },
        "gif2": {
            "base64":       gif2_b64,
            "content_type": "image/gif",
            "filename":     "komiks_ai_retro.gif",
        },
    }), 200


@app.route("/oauth/callback")
def oauth_callback():
    """Endpoint do obsługi OAuth callback — wymienia code na tokeny."""
    code = request.args.get('code')
    if not code:
        return "Brak kodu autoryzacyjnego w URL.", 400

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    redirect_uri = request.url_root.rstrip('/') + request.path  # np. https://app.onrender.com/oauth/callback

    if not client_id or not client_secret:
        return "Brak GMAIL_CLIENT_ID lub GMAIL_CLIENT_SECRET w env.", 500

    # Wymień code na tokeny
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        resp = requests.post(token_url, data=data, timeout=10)
        resp.raise_for_status()
        tokens = resp.json()
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        app.logger.info("OAuth tokeny uzyskane: access_token=%s, refresh_token=%s", access_token, refresh_token)
        return f"""
        <h1>OAuth zakończony sukcesem!</h1>
        <p>Skopiuj poniższe tokeny do zmiennych środowiskowych w Render:</p>
        <ul>
            <li><strong>GMAIL_ACCESS_TOKEN:</strong> {access_token}</li>
            <li><strong>GMAIL_REFRESH_TOKEN:</strong> {refresh_token}</li>
        </ul>
        <p>Możesz zamknąć tę stronę.</p>
        """, 200
    except Exception as e:
        app.logger.error("Błąd wymiany kodu na tokeny: %s", e)
        return f"Błąd wymiany kodu: {e}", 500


if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony.")
    if not os.getenv("API_KEY_GROQ"):
        app.logger.warning("API_KEY_GROQ nie ustawiony.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
