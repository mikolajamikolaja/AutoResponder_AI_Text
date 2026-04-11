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
import io
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
from responders.obrazek       import build_obrazek_section
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


@app.route("/webhook", methods=["POST"])
def webhook():
    # Inicjalizuj logger dla tego żądania
    logger = init_logger()
    
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
    retry_responders = data.get("retry_responders") or []
    attempt_count    = int(data.get("attempt_count", 1)) if data.get("attempt_count") else 1

    # Loguj zmienne
    logger.log_variables_detected({
        "sender": sender,
        "sender_name": sender_name,
        "has_previous_body": bool(previous_body),
        "has_previous_subject": bool(previous_subject),
        "num_attachments": len(attachments),
        "save_to_drive": save_to_drive,
        "test_mode": test_mode,
        "is_retry": bool(retry_responders),
        "attempt_count": attempt_count,
    })

    # ── Konfiguracja Drive ───────────────────────────────────────────────────
    drive_folder_id = os.getenv("DRIVE_FOLDER_ID")
    smierc_sheet_id = os.getenv("SMIERC_HISTORY_SHEET_ID")
    history_sheet_id = os.getenv("HISTORY_SHEET_ID")

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble      = bool(data.get("wants_scrabble"))
    wants_analiza       = bool(data.get("wants_analiza"))
    wants_emocje        = bool(data.get("wants_emocje"))
    wants_obrazek       = bool(data.get("wants_obrazek"))
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
        if wants_text_reply:
            requested_sections.update(["zwykly", "biznes"])  # Włączone
            pass
        if wants_scrabble:
            requested_sections.add("scrabble")
        if wants_nawiazanie:
            requested_sections.add("nawiazanie")

    wave1 = {}
    if "zwykly" in requested_sections:
        _prev   = previous_body
        _sender = sender
        _sname  = sender_name
        wave1["zwykly"] = lambda: run(build_zwykly_section, body, _prev, _sender, _sname, test_mode=test_mode)
    if "biznes" in requested_sections:
        wave1["biznes"] = lambda: run(build_biznes_section, body)
    if "nawiazanie" in requested_sections:
        wave1["nawiazanie"] = lambda: run(
            build_nawiazanie_section,
            body=body,
            previous_body=previous_body,
            previous_subject=previous_subject,
            sender=sender,
            sender_name=sender_name,
        )
    if "scrabble" in requested_sections:
        wave1["scrabble"] = lambda: run(build_scrabble_section, body)

    response_data = _run_parallel(wave1, flask_app)

    # ── WYSYŁKA PO FALI 1 ─────────────────────────────────────────────────────
    html_fala1 = "".join(filter(None, [
        response_data.get("zwykly",     {}).get("reply_html", ""),
        response_data.get("biznes",     {}).get("reply_html", ""),
        response_data.get("nawiazanie", {}).get("reply_html", ""),
        response_data.get("scrabble",   {}).get("reply_html", ""),
    ]))
    zalaczniki_fala1 = zbierz_zalaczniki_z_response(
        {k: response_data[k] for k in ("zwykly", "biznes", "scrabble", "log")
         if k in response_data}
    )
    if html_fala1.strip() and ("zwykly" in requested_sections or "biznes" in requested_sections or "nawiazanie" in requested_sections):
        wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'}",
            html_body  = html_fala1,
            zalaczniki = zalaczniki_fala1,
        )
    elif zalaczniki_fala1:
        # Wysyłka tylko załączników, jeśli nie ma tekstu
        wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'} (załączniki)",
            html_body  = "<p>Załączniki z pierwszej fali.</p>",
            zalaczniki = zalaczniki_fala1,
        )

    # ── FALA 2: ciężkie respondery ────────────────────────────────────────────
    wave2 = {}
    if is_retry:
        if "obrazek" in requested_sections:
            wave2["obrazek"] = lambda: run(build_obrazek_section, body)
        if "emocje" in requested_sections:
            wave2["emocje"]  = lambda: run(build_emocje_section, body, attachments)
        if "analiza" in requested_sections:
            wave2["analiza"] = lambda: run(build_analiza_section, body, attachments)
        if "generator_pdf" in requested_sections:
            _sn   = sender_name
            _body = body
            wave2["generator_pdf"] = lambda: run(
                build_generator_pdf_section, _body, sender_name=_sn
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
                test_mode=test_mode,
            )
    else:
        if wants_obrazek:
            wave2["obrazek"] = lambda: run(build_obrazek_section, body)
        if wants_emocje:
            wave2["emocje"]  = lambda: run(build_emocje_section, body, attachments)
        if wants_analiza:
            wave2["analiza"] = lambda: run(build_analiza_section, body, attachments)
        if wants_generator_pdf:
            _sn   = sender_name
            _body = body
            wave2["generator_pdf"] = lambda: run(
                build_generator_pdf_section, _body, sender_name=_sn
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
                test_mode=test_mode,
            )

    if wave2:
        response_data.update(_run_parallel(wave2, flask_app))

        # ── Generuj logi ──────────────────────────────────────────────────────
        logger = get_logger()
        groq_count = sum(1 for e in logger.entries if e['type'] == 'API_CALL' and e['data'].get('api') == 'groq')
        deepseek_count = sum(1 for e in logger.entries if e['type'] == 'API_CALL' and e['data'].get('api') == 'deepseek')
        nouns = []  # Jeśli analiza wykrywa nouns, dodać tutaj

        log_txt_content = f"Podsumowanie wykonania:\n- Groq użyty: {groq_count} razy\n- DeepSeek użyty: {deepseek_count} razy\n- Rzeczowniki wykryte: {', '.join(nouns) if nouns else 'brak'}\n"
        log_txt_b64 = base64.b64encode(log_txt_content.encode('utf-8')).decode('utf-8')
        response_data['log_txt'] = {'base64': log_txt_b64, 'content_type': 'text/plain', 'filename': 'log.txt'}

        svg_content = '''<svg width="500" height="100" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 100">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="#000"/>
    </marker>
  </defs>
  <text x="10" y="30" font-size="16" font-family="Arial">Email received</text>
  <line x1="120" y1="25" x2="180" y2="25" stroke="#000" stroke-width="2" marker-end="url(#arrow)"/>
  <text x="190" y="30" font-size="16" font-family="Arial">Processing</text>
  <line x1="280" y1="25" x2="340" y2="25" stroke="#000" stroke-width="2" marker-end="url(#arrow)"/>
  <text x="350" y="30" font-size="16" font-family="Arial">Response sent</text>
</svg>'''
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
        wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'} (część 2)",
            html_body  = html_fala2 or "<p>Załączniki z drugiej fali.</p>",
            zalaczniki = zbierz_zalaczniki_z_response(
                {k: response_data[k] for k in
                 ("obrazek", "emocje", "analiza", "generator_pdf", "smierc", "log")
                 if k in response_data}
            ),
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

    if not is_retry:
        requested_sections = set()
        if wants_text_reply:
            # requested_sections.update(["zwykly", "biznes"])  # Wyłączone
            pass
        if wants_scrabble:
            requested_sections.add("scrabble")
        if wants_analiza:
            requested_sections.add("analiza")
        if wants_emocje:
            requested_sections.add("emocje")
        if wants_obrazek:
            requested_sections.add("obrazek")
        if wants_generator_pdf:
            requested_sections.add("generator_pdf")
        if wants_smierc:
            requested_sections.add("smierc")
        if wants_nawiazanie:
            requested_sections.add("nawiazanie")
    else:
        requested_sections = set(retry_responders)

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
        for key, value in response_data.items():
            if isinstance(value, dict):
                # Obrazy
                if "images" in value and isinstance(value["images"], list):
                    for img in value["images"]:
                        if "base64" in img and "filename" in img:
                            upload_result = upload_file_to_drive(
                                img["base64"], img["filename"], img.get("content_type", "image/png"), drive_folder_id
                            )
                            if upload_result:
                                img["drive_url"] = upload_result["url"]
                                drive_uploads.append(f"{img['filename']}: {upload_result['url']}")
                            else:
                                app.logger.error(f"Błąd uploadu {img['filename']} do Drive")

                # PDF
                if "pdf" in value and isinstance(value["pdf"], dict) and "base64" in value["pdf"]:
                    upload_result = upload_file_to_drive(
                        value["pdf"]["base64"], value["pdf"]["filename"], "application/pdf", drive_folder_id
                    )
                    if upload_result:
                        value["pdf"]["drive_url"] = upload_result["url"]
                        drive_uploads.append(f"{value['pdf']['filename']}: {upload_result['url']}")
                    else:
                        app.logger.error(f"Błąd uploadu PDF {value['pdf']['filename']} do Drive")

                # Debug txt dla smierc
                if "debug_txt" in value and isinstance(value["debug_txt"], dict) and "base64" in value["debug_txt"]:
                    upload_result = upload_file_to_drive(
                        value["debug_txt"]["base64"], value["debug_txt"]["filename"], "text/plain", drive_folder_id
                    )
                    if upload_result:
                        value["debug_txt"]["drive_url"] = upload_result["url"]
                        drive_uploads.append(f"{value['debug_txt']['filename']}: {upload_result['url']}")
                    else:
                        app.logger.error(f"Błąd uploadu debug txt do Drive")

        if drive_uploads:
            app.logger.info(f"Zapisano do Drive: {', '.join(drive_uploads)}")

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
                values = [[smierc_data["nowy_etap"], "", body[:2000], smierc_data.get("reply_html", "")[:2000], ""]]
                update_sheet_with_data(smierc_sheet_id, range_name, values)
                app.logger.info(f"Zaktualizowano arkusz śmierci dla {sender}, etap {smierc_data['nowy_etap']}")
            except Exception as e:
                app.logger.error(f"Błąd aktualizacji arkusza śmierci: {e}")

    # ── Zapis do arkusza historii ─────────────────────────────────────────────
    if history_sheet_id:
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
