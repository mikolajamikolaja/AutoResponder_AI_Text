#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.

KOLEJNOŚĆ WYSYŁKI (priorytetowa):
  1. zwykly    — zawsze, jeśli nadawca jest znany
  2. smierc    — jeśli wants_smierc (requiem aktywne)
  3. dociekliwy (analiza / Eryk) — jeśli wants_analiza lub KEYWORDS3
  4. pozostałe — biznes, scrabble, emocje, generator_pdf, nawiazanie

NAPRAWIONE BŁĘDY:
  - [BUG] attachments (zmienna lokalna) nadpisywała zewnętrzny parametr
    przy zbieraniu załączników w pętli wysyłki → każdy responder teraz
    używa osobnej zmiennej `zal`
  - [BUG] smierc nie był uruchamiany w Wave 1/2 mimo wants_smierc=True
    → dodany do Wave 1 sekwencyjnie po zwykly (ma własną kolejkę)
  - [BUG] dociekliwy (build_analiza_section) był wywoływany wewnątrz
    zwykly.py ORAZ oddzielnie przez app.py → podwójne wywołanie AI
    → teraz zwykly NIE wywołuje dociekliwy; app.py robi to osobno
  - [BUG] JOKER (zwykly+smierc) powodował timeout 502 przez brak limitu
    czasu gunicorn → dodaj --timeout 300 do gunicorn (patrz __main__)
  - [BUG] log_txt / log_svg generowane PRZED smierc i dociekliwy
    → logi generowane na końcu, po wszystkich responderach
  - [BUG] requested_sections nie zawierał 'smierc' ani 'dociekliwy'
    → teraz dodawane poprawnie
  - [BUG] historia zapisywana podwójnie (w pętli + na końcu)
    → jeden zapis na końcu
"""

import os
import base64
import html
import io
import json
import re
import traceback
from flask import Flask, request, jsonify, current_app
import requests as http_requests

from drive_utils import upload_file_to_drive, update_sheet_with_data, save_to_history_sheet
from core.logging_reporter import init_logger, get_logger

from responders.zwykly        import build_zwykly_section
from responders.biznes        import build_biznes_section
from responders.scrabble      import build_scrabble_section
from responders.dociekliwy    import build_dociekliwy_section as build_analiza_section
from responders.emocje        import build_emocje_section
from responders.nawiazanie    import build_nawiazanie_section
from responders.gif_maker     import make_gif
from responders.generator_pdf import build_generator_pdf_section
from responders.smierc        import build_smierc_section
from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# POMOCNIKI
# ═══════════════════════════════════════════════════════════════════════════════

def _run_sequential(tasks: dict, flask_app) -> dict:
    """Uruchamia słownik {klucz: lambda} SEKWENCYJNIE, zwraca {klucz: wynik}.
    Sekwencyjność jest kluczowa — zapobiega timeout 502 przy równoległych
    ciężkich calach AI (zwykly + smierc + FLUX = crash workera)."""
    results = {}
    for key, fn in tasks.items():
        try:
            flask_app.logger.info("[pipeline] START: %s", key)
            results[key] = fn()
            flask_app.logger.info("[pipeline] OK:    %s", key)
        except Exception as e:
            flask_app.logger.error(
                "[pipeline] BŁĄD responderu '%s': %s\n%s", key, e, traceback.format_exc()
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
    """Rozszerzony diagram SVG przebiegu autorespondera."""
    entries = logger.entries
    if not entries:
        return '''<svg width="1000" height="300" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 300">
  <text x="10" y="150" font-size="16" font-family="Arial">Brak danych logowania</text>
</svg>'''

    input_data = next((e for e in entries if e['type'] == 'INPUT'), None)
    api_calls = [e for e in entries if e['type'] == 'API_CALL']
    section_results = [e for e in entries if e['type'] == 'SECTION_RESULT']
    decisions = [e for e in entries if e['type'] == 'DECISION']

    deepseek_all = [e for e in api_calls if e['data'].get('api') == 'deepseek']
    deepseek_success = sum(1 for e in deepseek_all if e['data'].get('success'))
    deepseek_fail = len(deepseek_all) - deepseek_success

    sections_ok = sum(1 for e in section_results if e['data'].get('success'))
    sections_fail = len(section_results) - sections_ok

    first_ts = entries[0].get('timestamp', 0)
    last_ts = entries[-1].get('timestamp', 0)
    total_time = last_ts - first_ts

    def escape_xml(text):
        return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;').replace("'", '&apos;').replace('&nbsp;', '&#160;'))

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
  <rect width="{width}" height="{height}" fill="#fafafa" stroke="#ddd" stroke-width="1"/>
  <rect x="0" y="0" width="{width}" height="40" fill="#1a1a2e" stroke="none"/>
  <text x="20" y="26" class="title" fill="#e8d5b0">Diagram Przebiegu AutoRespondera</text>
  <text x="{width-400}" y="26" class="subtitle" fill="#aaa">Czas: {total_time:.2f}s | Wpisy: {len(entries)} | Sekcje: {sections_ok}✓ {sections_fail}✗</text>
'''
    y_pos = 60

    # SEKCJA 1: INPUT
    sender_disp = input_data['data'].get('sender', '?') if input_data else "?"
    subject_disp = input_data['data'].get('subject', '?') if input_data else "?"
    body_preview = (input_data['data'].get('body_preview', '')[:35] + "...") if input_data else ""

    svg += f'''  <rect x="20" y="{y_pos}" width="340" height="110" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📧 WEJŚCIE</text>
  <text x="30" y="{y_pos+42}" class="text">Nadawca: {escape_xml(sender_disp)}</text>
  <text x="30" y="{y_pos+60}" class="text">Temat: {escape_xml(subject_disp[:35])}</text>
  <text x="30" y="{y_pos+78}" class="text">Treść: {escape_xml(body_preview)}</text>
'''
    y_pos += 140

    # SEKCJA 2: DECYZJE
    if decisions:
        svg += f'''  <rect x="20" y="{y_pos}" width="340" height="{50 + min(len(decisions), 4) * 20}" class="info"/>
  <text x="30" y="{y_pos+20}" class="title">🎯 DECYZJE: {len(decisions)}</text>
'''
        for i, decision in enumerate(decisions[:4]):
            result = decision['data'].get('result', '?')
            decision_text = decision['data'].get('decision', 'N/A')[:25]
            svg += f'  <text x="30" y="{y_pos+40+i*18}" class="text">• {decision_text} → {result}</text>\n'
        y_pos += 70 + min(len(decisions), 4) * 20

    # SEKCJA 3: API
    svg += f'''  <rect x="20" y="{y_pos}" width="1160" height="130" class="{'success' if deepseek_success > 0 else 'error'}"/>
  <text x="30" y="{y_pos+20}" class="title">⚙️ API CALLS</text>
  <text x="40" y="{y_pos+50}" class="metric">DEEPSEEK PRÓBY: {len(deepseek_all)}</text>
  <text x="40" y="{y_pos+68}" class="metric">DEEPSEEK SKUTECZNE: {deepseek_success}</text>
  <text x="40" y="{y_pos+86}" class="metric">DEEPSEEK NIEUDANE: {deepseek_fail}</text>
'''
    y_pos += 160

    # SEKCJA 4: HARMONOGRAM
    svg += f'''  <rect x="20" y="{y_pos}" width="1160" height="{60 + num_timeline_items * 20}" class="warning"/>
  <text x="30" y="{y_pos+20}" class="title">⏱️ HARMONOGRAM PIERWSZYCH {num_timeline_items} ETAPÓW</text>
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
    svg += f'''  <rect x="20" y="{y_pos}" width="1160" height="{60 + max(len(section_details), 1) * 28}" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📋 SEKCJE: {sections_ok}✓ {sections_fail}✗</text>
'''
    for i, (section_name, success) in enumerate(section_details):
        box_class = 'success' if success else 'error'
        status = '✓' if success else '✗'
        svg += f'  <rect x="30" y="{y_pos+35+i*28}" width="1140" height="24" class="{box_class}"/>\n'
        svg += f'  <text x="40" y="{y_pos+53+i*28}" class="text">{status} {section_name.upper() if section_name else "UNKNOWN"}</text>\n'

    svg += '''  <defs>
    <marker id="arrowhead" markerWidth="10" markerHeight="10" refX="5" refY="5" orient="auto">
      <polygon points="0,0 10,5 0,10" fill="#0066cc"/>
    </marker>
  </defs>
</svg>'''
    return svg


def _build_log_txt_content(logger, response_data) -> str:
    api_calls = [e for e in logger.entries if e['type'] == 'API_CALL']
    deepseek_calls = [e for e in api_calls if e['data'].get('api') == 'deepseek']
    deepseek_success = sum(1 for e in deepseek_calls if e['data'].get('success'))
    deepseek_total = len(deepseek_calls)

    nouns_dict = response_data.get('zwykly', {}).get('nouns_dict', {})
    detected_nouns = []
    if isinstance(nouns_dict, dict):
        detected_nouns = [v for v in nouns_dict.values() if isinstance(v, str) and v.strip()]

    section_results = [e for e in logger.entries if e['type'] == 'SECTION_RESULT']
    sections_success = sum(1 for e in section_results if e['data'].get('success'))
    sections_total = len(section_results)

    keywords_used = logger.metadata.get('keywords_used', False)

    lines = []
    lines.append("=" * 88)
    lines.append("PODSUMOWANIE WYKONANIA AUTORESPONDERA")
    lines.append("=" * 88)
    lines.append(f"Start: {logger.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Sesja: {logger.session_id}")
    lines.append("")

    lines.append("0. METADANE SESJI")
    keyword_labels = []
    for kw in ['contains_keyword', 'contains_keyword1', 'contains_keyword2',
               'contains_keyword3', 'contains_keyword4', 'contains_flaga_test',
               'contains_keyword_joker']:
        if logger.metadata.get(kw):
            keyword_labels.append(kw.upper())
    lines.append(f"- Status historia: {logger.metadata.get('in_history', '?')}")
    lines.append(f"- Status requiem:  {logger.metadata.get('in_requiem', '?')}")
    lines.append(f"- Słowa kluczowe:  {', '.join(keyword_labels) if keyword_labels else 'NIE'}")
    if keywords_used:
        lines.append("- ⓘ KEYWORDS_TEST aktywny — FLUX wyłączony")
    lines.append("")

    lines.append("1. API CALLS")
    if deepseek_total > 0:
        acc = deepseek_success / deepseek_total * 100
        lines.append(f"- DeepSeek: {deepseek_total} prób | {deepseek_success} skutecznych ({acc:.1f}%)")
    else:
        lines.append("- DeepSeek: 0 prób")
    lines.append(f"- RAZEM: {len(api_calls)}")
    lines.append("")

    lines.append("2. SEKCJE RESPONDENTÓW")
    lines.append(f"- Uruchomione: {sections_total} | Pomyślne: {sections_success}")
    if sections_total > 0:
        lines.append(f"- Sukces: {(sections_success / sections_total * 100):.1f}%")
    lines.append("")

    lines.append("3. LISTA SEKCJI")
    section_keys = [k for k in response_data.keys() if k not in ("log_txt", "log_svg")]
    for section_name in sorted(section_keys):
        section_data = response_data.get(section_name, {})
        if not isinstance(section_data, dict):
            continue
        has_html = bool(section_data.get('reply_html', '').strip())
        has_att = len(section_data.get('docx_list', [])) > 0 or len(section_data.get('images', [])) > 0
        status = '✓' if (has_html or has_att) else '✗'
        lines.append(f"  {status} {section_name.upper()}")
        if has_html:
            lines.append(f"      - HTML: {len(section_data.get('reply_html', ''))} znaków")
        docs = section_data.get('docx_list', [])
        imgs = section_data.get('images', [])
        names = [d.get('filename') for d in docs if isinstance(d, dict) and d.get('filename')]
        names += [d.get('filename') for d in imgs if isinstance(d, dict) and d.get('filename')]
        if names:
            lines.append(f"      - Pliki: {', '.join(names)}")
    lines.append("")

    lines.append("4. HARMONOGRAM")
    if logger.entries:
        t0 = logger.entries[0].get('timestamp', 0)
        t1 = logger.entries[-1].get('timestamp', 0)
        lines.append(f"- Czas całkowity: {(t1-t0):.2f}s")
        for i, entry in enumerate(logger.entries[:10]):
            delta = entry.get('timestamp', 0) - t0
            lines.append(f"  [{i+1:2d}] +{delta:6.2f}s: {entry['type'][:20]}")
    lines.append("")

    lines.append("5. SZCZEGÓŁOWE WPISY")
    for entry in logger.entries:
        ts = entry.get('timestamp', 0.0)
        lines.append(f"[{entry['type']}] +{ts:.2f}s")
        lines.extend(_format_log_entry_data(entry.get('data')))
        lines.append("")

    if detected_nouns:
        lines.append("6. RZECZOWNIKI")
        for noun in detected_nouns:
            lines.append(f"- {noun}")
        lines.append("")

    lines.append("7. WNIOSKI")
    rate = (sections_success / sections_total * 100) if sections_total > 0 else 0
    if rate == 100:
        lines.append("- ✓ SUCCESS")
    elif rate >= 75:
        lines.append(f"- ✓ DOBRY ({rate:.0f}%)")
    elif rate >= 50:
        lines.append(f"- ⚠ ŚREDNI ({rate:.0f}%)")
    else:
        lines.append(f"- ✗ ZŁY ({rate:.0f}%)")

    lines.append("=" * 88)
    lines.append("KONIEC")
    lines.append("=" * 88)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# WYSYŁKA MAILA — pomocnik bez nadpisywania zmiennej attachments
# ═══════════════════════════════════════════════════════════════════════════════

def _wyslij_responder(responder_key: str, resp_data: dict, sender: str, sender_name: str,
                      previous_subject: str, logger) -> bool:
    """
    Wysyła jeden email dla danego respondera.
    Zwraca True jeśli wysłanie się powiodło.

    POPRAWKA: używa lokalnej zmiennej `zal` zamiast `attachments`,
    żeby nie nadpisywać listy attachmentów z requestu.
    """
    section = resp_data.get(responder_key)
    if not section or not isinstance(section, dict):
        return False

    email_html = section.get("reply_html", "")
    zal = zbierz_zalaczniki_z_response({responder_key: section})

    if not email_html.strip() and not zal:
        return False

    # Temat z kluczem respondera
    subject_line = f"Re: {previous_subject or 'Twoja wiadomość'}"

    # Dla śmierci użyj tematu wygenerowanego przez smierc.py jeśli dostępny
    if responder_key == "smierc" and section.get("subject"):
        subject_line = section["subject"]

    success = wyslij_odpowiedz(
        to_email   = sender,
        to_name    = sender_name,
        subject    = subject_line,
        html_body  = email_html or "<p>Załączniki w osobnych plikach.</p>",
        zalaczniki = zal,
    )
    if success:
        logger.log_decision("email_sent", f"Sent {responder_key}", True)
    else:
        logger.log_decision("email_failed", f"Failed {responder_key}", False)
    return success


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK GŁÓWNY
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    render_instance_id = os.getenv("RENDER_INSTANCE_ID", "")
    session_id = render_instance_id if render_instance_id else None
    logger = init_logger(session_id=session_id)

    data = request.json or {}
    body = data.get("body", "")

    if not body or not body.strip():
        logger.log_decision("empty_body_check", "body.strip() == ''", False)
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    sender      = data.get("sender", "")
    sender_name = data.get("sender_name", "")
    subject     = data.get("subject", "")
    logger.log_input(sender, subject, body, sender_name)

    # ── Ochrona przed pętlą (admin email) ────────────────────────────────────
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    if admin_email and sender.strip().lower() == admin_email:
        logger.log_decision("admin_email_block", f"sender == ADMIN_EMAIL ({sender})", True)
        app.logger.warning("[AUTORESPONDER] 🔒 ZABLOKOWANO: Wiadomość od ADMIN_EMAIL (%s)", sender)
        return jsonify({
            "status": "blocked",
            "reason": "sender_is_admin_email",
            "sender": sender,
        }), 200

    # ── Parametry requestu ────────────────────────────────────────────────────
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None
    # POPRAWKA: załączniki z requestu trzymamy w `req_attachments`
    # żeby nigdy nie zostały nadpisane przez lokalną zmienną w pętli
    req_attachments  = data.get("attachments")      or []
    save_to_drive    = bool(data.get("save_to_drive"))
    test_mode        = bool(data.get("test_mode"))
    disable_flux     = bool(data.get("disable_flux"))
    retry_responders = data.get("retry_responders") or []
    attempt_count    = int(data.get("attempt_count", 1)) if data.get("attempt_count") else 1
    skip_save_to_history = bool(data.get("skip_save_to_history"))

    keywords_used = False
    if disable_flux:
        keywords_used = True
        logger.set_metadata("keywords_used", True)
        logger.log_decision("disable_flux", "disable_flux=True", "FLUX wyłączony")

    # ── Google Drive / Sheets ─────────────────────────────────────────────────
    drive_folder_id  = os.getenv("DRIVE_FOLDER_ID")
    smierc_sheet_id  = os.getenv("SMIERC_HISTORY_SHEET_ID")
    history_sheet_id = os.getenv("HISTORY_SHEET_ID")

    # ── Sprawdzenie statusu użytkownika ───────────────────────────────────────
    from drive_utils import check_user_in_sheet
    if not test_mode:
        in_history_status = "tak" if check_user_in_sheet(history_sheet_id, sender) else "nie"
        in_requiem_status = "tak" if check_user_in_sheet(smierc_sheet_id, sender)  else "nie"
    else:
        in_history_status = "test_mode"
        in_requiem_status = "test_mode"

    logger.set_metadata("in_history", in_history_status)
    logger.set_metadata("in_requiem", in_requiem_status)

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble      = bool(data.get("wants_scrabble"))
    wants_biznes        = bool(data.get("wants_biznes"))
    wants_analiza       = bool(data.get("wants_analiza"))
    wants_emocje        = bool(data.get("wants_emocje"))
    wants_generator_pdf = bool(data.get("wants_generator_pdf"))
    wants_smierc        = bool(data.get("wants_smierc"))
    wants_text_reply    = bool(data.get("wants_text_reply", True))
    wants_nawiazanie    = bool(previous_body or previous_subject)
    is_retry            = bool(retry_responders)

    contains_keyword       = bool(data.get("contains_keyword"))
    contains_keyword1      = bool(data.get("contains_keyword1"))
    contains_keyword2      = bool(data.get("contains_keyword2"))
    contains_keyword3      = bool(data.get("contains_keyword3"))
    contains_keyword4      = bool(data.get("contains_keyword4"))
    contains_flaga_test    = bool(data.get("contains_flaga_test"))
    contains_keyword_joker = bool(data.get("contains_keyword_joker"))
    matched_keywords       = data.get("matched_keywords") or {}

    has_any_keyword = any([contains_keyword, contains_keyword1, contains_keyword2,
                           contains_keyword3, contains_keyword4, contains_keyword_joker])

    for meta_key, meta_val in [
        ("has_any_keyword",       has_any_keyword),
        ("contains_keyword",      contains_keyword),
        ("contains_keyword1",     contains_keyword1),
        ("contains_keyword2",     contains_keyword2),
        ("contains_keyword3",     contains_keyword3),
        ("contains_keyword4",     contains_keyword4),
        ("contains_flaga_test",   contains_flaga_test),
        ("contains_keyword_joker",contains_keyword_joker),
    ]:
        logger.set_metadata(meta_key, meta_val)
    if matched_keywords:
        logger.set_metadata("matched_keywords", matched_keywords)

    logger.log_variables_detected({
        "sender": sender, "sender_name": sender_name,
        "has_previous_body": bool(previous_body),
        "num_attachments": len(req_attachments),
        "save_to_drive": save_to_drive,
        "test_mode": test_mode, "disable_flux": disable_flux,
        "contains_keyword": contains_keyword,
        "contains_keyword1": contains_keyword1,
        "contains_keyword2": contains_keyword2,
        "contains_keyword3": contains_keyword3,
        "contains_keyword4": contains_keyword4,
        "contains_flaga_test": contains_flaga_test,
        "contains_keyword_joker": contains_keyword_joker,
        "wants_smierc": wants_smierc,
        "wants_analiza": wants_analiza,
        "wants_biznes": wants_biznes,
        "wants_scrabble": wants_scrabble,
        "wants_emocje": wants_emocje,
        "wants_generator_pdf": wants_generator_pdf,
        "is_retry": is_retry,
        "attempt_count": attempt_count,
        "skip_save_to_history": skip_save_to_history,
    })

    flask_app = app

    def run(fn, *args, **kwargs_inner):
        with flask_app.app_context():
            return fn(*args, **kwargs_inner)

    # ═══════════════════════════════════════════════════════════════════════════
    # BUDOWANIE LISTY SEKCJI DO WYKONANIA
    # Priorytet: zwykly → smierc → dociekliwy → pozostałe
    # ═══════════════════════════════════════════════════════════════════════════

    if is_retry:
        requested_sections = list(retry_responders)
    else:
        requested_sections = []

        # 1. ZWYKLY — priorytet 1 (gdy znany nadawca lub keyword)
        if contains_keyword or in_history_status == "tak":
            requested_sections.append("zwykly")
            logger.log_decision("zwykly", "known sender or keyword", "dodano")

        # 2. SMIERC — priorytet 2 (posmiertny autoresponder)
        if wants_smierc:
            requested_sections.append("smierc")
            logger.log_decision("smierc", "wants_smierc=True", "dodano")

        # 3. DOCIEKLIWY (ANALIZA / ERYK) — priorytet 3
        if wants_analiza or contains_keyword3:
            requested_sections.append("analiza")
            logger.log_decision("analiza", "wants_analiza or keyword3", "dodano")

        # 4. POZOSTAŁE (kolejność drugorzędna)
        if wants_nawiazanie:
            requested_sections.append("nawiazanie")
        if wants_biznes:
            requested_sections.append("biznes")
        if wants_scrabble:
            requested_sections.append("scrabble")
        if wants_emocje:
            requested_sections.append("emocje")
        if wants_generator_pdf:
            requested_sections.append("generator_pdf")

    # ─── Wyświetl planowany pipeline ─────────────────────────────────────────
    app.logger.info("[pipeline] Zaplanowane sekcje (w kolejności): %s", " → ".join(requested_sections))

    # ═══════════════════════════════════════════════════════════════════════════
    # POBIERZ DANE ŚMIERCI (jeśli potrzebne) — PRZED uruchomieniem respondentów
    # ═══════════════════════════════════════════════════════════════════════════

    smierc_etap       = int(data.get("etap", 1))
    smierc_data_str   = data.get("data_smierci", "nieznanego dnia")
    smierc_historia   = data.get("historia", [])

    if wants_smierc:
        app.logger.info(
            "Smierc data dla %s: etap=%d data=%s", sender, smierc_etap, smierc_data_str
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # WYKONANIE PIPELINE — SEKWENCYJNIE (priorytetowo)
    # POPRAWKA: NIE równolegle — zapobiega timeout 502 na Render
    #
    # UWAGA: zwykly.py wewnętrznie wywołuje dociekliwy TYLKO dla analizy
    # załączników. Główna gra Eryka jest uruchamiana osobno przez app.py.
    # Aby uniknąć podwójnego wywołania AI — zwykly.py otrzymuje `attachments`
    # tylko jeśli "analiza" NIE jest w requested_sections (wtedy app.py sam
    # wykona dociekliwy z pełną grą Eryka).
    # ═══════════════════════════════════════════════════════════════════════════

    # Czy zwykly ma dostać załączniki (dla mini-analizy wewnętrznej)?
    # Jeśli "analiza" jest osobno w pipeline, nie przekazujemy żeby nie dublować
    zwykly_attachments = req_attachments if "analiza" not in requested_sections else []

    # Buduj tasks dict w kolejności priorytetowej
    tasks = {}

    for section_key in requested_sections:
        if section_key == "zwykly":
            _prev   = previous_body
            _sender = sender
            _sname  = sender_name
            tasks["zwykly"] = lambda: run(
                build_zwykly_section,
                body,
                _prev,
                _sender,
                _sname,
                test_mode=disable_flux or test_mode,
                attachments=zwykly_attachments,
                # Jeśli "analiza" jest osobno w pipeline, pomijamy dociekliwy
                # wewnątrz zwykly — zapobiega podwójnemu wywołaniu AI
                skip_dociekliwy=("analiza" in requested_sections),
            )

        elif section_key == "smierc":
            _etap     = smierc_etap
            _data_str = smierc_data_str
            _historia = smierc_historia
            tasks["smierc"] = lambda: run(
                build_smierc_section,
                sender_email    = sender,
                body            = body,
                etap            = _etap,
                data_smierci_str= _data_str,
                historia        = _historia,
                test_mode       = disable_flux or test_mode,
            )

        elif section_key == "analiza":
            tasks["analiza"] = lambda: run(
                build_analiza_section,
                body,
                req_attachments,
                sender      = sender,
                sender_name = sender_name,
                test_mode   = disable_flux or test_mode,
            )

        elif section_key == "nawiazanie":
            tasks["nawiazanie"] = lambda: run(
                build_nawiazanie_section,
                body             = body,
                previous_body    = previous_body,
                previous_subject = previous_subject,
                sender           = sender,
                sender_name      = sender_name,
            )

        elif section_key == "biznes":
            tasks["biznes"] = lambda: run(build_biznes_section, body, sender_name=sender_name)

        elif section_key == "scrabble":
            tasks["scrabble"] = lambda: run(build_scrabble_section, body)

        elif section_key == "emocje":
            tasks["emocje"] = lambda: run(
                build_emocje_section, body, sender_name=sender_name,
                test_mode=disable_flux or test_mode
            )

        elif section_key == "generator_pdf":
            tasks["generator_pdf"] = lambda: run(build_generator_pdf_section, body, sender_name=sender_name)

    # ── WYKONAJ PIPELINE SEKWENCYJNIE ────────────────────────────────────────
    response_data = _run_sequential(tasks, flask_app)

    # ── Zabezpieczenie — nawiazanie zawsze ma has_history ────────────────────
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {"has_history": False, "reply_html": "", "analysis": ""}

    # ═══════════════════════════════════════════════════════════════════════════
    # WYSYŁKA MAILI — W KOLEJNOŚCI PRIORYTETOWEJ: zwykly → smierc → analiza → reszta
    # ═══════════════════════════════════════════════════════════════════════════

    # Kolejność wysyłki jest stała i priorytetowa
    SEND_ORDER = ["zwykly", "smierc", "analiza", "nawiazanie", "emocje",
                  "scrabble", "biznes", "generator_pdf"]

    any_sent = False
    for responder_key in SEND_ORDER:
        if responder_key not in response_data:
            continue
        sent = _wyslij_responder(
            responder_key  = responder_key,
            resp_data      = response_data,
            sender         = sender,
            sender_name    = sender_name,
            previous_subject = previous_subject,
            logger         = logger,
        )
        if sent:
            any_sent = True
            app.logger.info("[send] ✓ Wysłano: %s → %s", responder_key, sender)
        else:
            app.logger.info("[send] — Pominięto (brak treści): %s", responder_key)

    # ── Alert dla admina jeśli nic nie wysłano ───────────────────────────────
    if not any_sent and admin_email:
        wyslij_odpowiedz(
            to_email  = admin_email,
            to_name   = "Admin",
            subject   = f"[ALERT] Brak wysyłki dla: {sender}",
            html_body = f"<p>Brak treści do wysłania dla nadawcy: {sender}<br>Sekcje: {list(response_data.keys())}</p>",
            zalaczniki= [],
        )
        app.logger.warning("Wysłano alert do ADMIN_EMAIL: %s", admin_email)

    # ── Zapis historii (RAZ, na końcu) ───────────────────────────────────────
    if history_sheet_id and not skip_save_to_history:
        success_hist = save_to_history_sheet(history_sheet_id, sender, subject, body)
        if not success_hist:
            app.logger.error("Błąd zapisu historii dla %s", sender)
        else:
            app.logger.info("Historia zapisana dla: %s", sender)

    # ── Aktualizacja arkusza śmierci (etap) ──────────────────────────────────
    if smierc_sheet_id and "smierc" in response_data and response_data["smierc"]:
        smierc_res = response_data["smierc"]
        if isinstance(smierc_res, dict) and "nowy_etap" in smierc_res:
            try:
                range_name = (
                    f"{sender.replace('@', '_').replace('.', '_')}"
                    f"!A{smierc_res['nowy_etap'] + 1}"
                )
                values = [[
                    smierc_res["nowy_etap"], "",
                    body[:2000],
                    _strip_html_to_text(smierc_res.get("reply_html", ""))[:2000],
                    ""
                ]]
                update_sheet_with_data(smierc_sheet_id, range_name, values)
                app.logger.info(
                    "Zaktualizowano arkusz śmierci dla %s, nowy etap: %d",
                    sender, smierc_res["nowy_etap"]
                )
            except Exception as e:
                app.logger.error("Błąd aktualizacji arkusza śmierci: %s", e)

    # ── Generuj logi (PO wszystkich responderach) ─────────────────────────────
    log_txt_content = _build_log_txt_content(logger, response_data)
    log_txt_b64 = base64.b64encode(log_txt_content.encode('utf-8')).decode('utf-8')
    response_data['log_txt'] = {
        'base64': log_txt_b64, 'content_type': 'text/plain', 'filename': 'log.txt'
    }

    svg_content = _build_log_svg_content(logger)
    log_svg_b64 = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
    response_data['log_svg'] = {
        'base64': log_svg_b64, 'content_type': 'image/svg+xml', 'filename': 'log.svg'
    }

    # ── Zapis do Google Drive ─────────────────────────────────────────────────
    if save_to_drive and drive_folder_id:
        drive_uploads = []
        for top_field in ("log_txt", "log_svg"):
            file_obj = response_data.get(top_field)
            if isinstance(file_obj, dict) and file_obj.get("base64") and file_obj.get("filename"):
                if _upload_drive_item(file_obj, drive_folder_id):
                    drive_uploads.append(f"{top_field}/{file_obj['filename']}")
        for key, value in response_data.items():
            if not isinstance(value, dict):
                continue
            section_uploads = _upload_drive_section_files(value, drive_folder_id)
            drive_uploads.extend([f"{key}/{name}" for name in section_uploads])
        if drive_uploads:
            app.logger.info("Zapisano do Drive: %s", ', '.join(drive_uploads))
        response_data["saved_to_drive"] = True

    # ── Status sekcji ─────────────────────────────────────────────────────────
    def section_success(key: str, value) -> bool:
        if not value or not isinstance(value, dict):
            return False
        if key == "nawiazanie":
            return bool(value.get("has_history") or value.get("reply_html"))
        return bool(value)

    failed_sections = [
        key for key in requested_sections
        if not section_success(key, response_data.get(key))
    ]
    if failed_sections:
        response_data["processed_status"] = {
            "status": "partial",
            "failed": failed_sections,
            "attempt_count": attempt_count,
        }
    else:
        response_data["processed_status"] = {"status": "ok"}

    # ── Log końcowy ───────────────────────────────────────────────────────────
    smierc_data_res = response_data.get("smierc", {})
    smierc_images_cnt = (
        len(smierc_data_res.get("images", []))
        if isinstance(smierc_data_res, dict) and isinstance(smierc_data_res.get("images"), list)
        else 0
    )
    app.logger.info(
        "Response: zwykly=%s | smierc=%s (images=%d) | analiza=%s | "
        "emocje=%s | nawiazanie=%s | generator_pdf=%s | sender=%s",
        bool(response_data.get("zwykly")),
        bool(response_data.get("smierc")),
        smierc_images_cnt,
        bool(response_data.get("analiza")),
        bool(response_data.get("emocje")),
        bool(response_data.get("nawiazanie", {}).get("has_history")),
        bool(response_data.get("generator_pdf", {}).get("pdf")),
        sender_name or sender or "(brak)",
    )

    logger.finalize()
    return jsonify(response_data), 200


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK GIF
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook_gif", methods=["POST"])
def webhook_gif():
    """Przyjmuje dwa PNG jako base64, zwraca dwa GIFy jako base64."""
    data     = request.json or {}
    png1_b64 = data.get("png1_base64")
    png2_b64 = data.get("png2_base64")

    if not png1_b64 and not png2_b64:
        return jsonify({"error": "Brak png1_base64 i png2_base64"}), 400

    app.logger.info("/webhook_gif — odebrano PNG: png1=%s png2=%s", bool(png1_b64), bool(png2_b64))

    gif1_b64 = make_gif(png1_b64) if png1_b64 else None
    gif2_b64 = make_gif(png2_b64) if png2_b64 else None

    app.logger.info("/webhook_gif — GIFy: gif1=%s gif2=%s", bool(gif1_b64), bool(gif2_b64))

    return jsonify({
        "gif1": {"base64": gif1_b64, "content_type": "image/gif", "filename": "komiks_ai.gif"},
        "gif2": {"base64": gif2_b64, "content_type": "image/gif", "filename": "komiks_ai_retro.gif"},
    }), 200


# ═══════════════════════════════════════════════════════════════════════════════
# OAUTH CALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/oauth/callback")
def oauth_callback():
    """Endpoint do obsługi OAuth callback — wymienia code na tokeny."""
    code = request.args.get('code')
    if not code:
        return "Brak kodu autoryzacyjnego w URL.", 400

    client_id     = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    redirect_uri  = request.url_root.rstrip('/') + request.path

    if not client_id or not client_secret:
        return "Brak GMAIL_CLIENT_ID lub GMAIL_CLIENT_SECRET w env.", 500

    token_url = "https://oauth2.googleapis.com/token"
    post_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        resp = http_requests.post(token_url, data=post_data, timeout=10)
        resp.raise_for_status()
        tokens = resp.json()
        access_token  = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        app.logger.info("OAuth tokeny uzyskane: access=%s refresh=%s", access_token, refresh_token)
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


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
