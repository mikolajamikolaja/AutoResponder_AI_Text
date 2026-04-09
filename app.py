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
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify

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
    """Uruchamia słownik {klucz: lambda} równolegle, zwraca {klucz: wynik}."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                flask_app.logger.error(
                    "Błąd responderu '%s': %s\n%s", key, e, traceback.format_exc()
                )
                results[key] = {}
    return results


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    body = data.get("body", "")

    if not body or not body.strip():
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # ── Pola nadawcy i historia ───────────────────────────────────────────────
    sender           = data.get("sender",      "")
    sender_name      = data.get("sender_name", "")
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None
    attachments      = data.get("attachments")      or []

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble      = bool(data.get("wants_scrabble"))
    wants_analiza       = bool(data.get("wants_analiza"))
    wants_emocje        = bool(data.get("wants_emocje"))
    wants_obrazek       = bool(data.get("wants_obrazek"))
    wants_generator_pdf = bool(data.get("wants_generator_pdf"))
    wants_smierc        = bool(data.get("wants_smierc"))
    wants_text_reply    = bool(data.get("wants_text_reply", True))

    flask_app = app

    def run(fn, *args, **kwargs):
        with flask_app.app_context():
            return fn(*args, **kwargs)

    # ── FALA 1: lekkie respondery ─────────────────────────────────────────────
    wave1 = {}
    if wants_text_reply:
        _prev   = previous_body
        _sender = sender
        _sname  = sender_name
        wave1["zwykly"] = lambda: run(build_zwykly_section, body, _prev, _sender, _sname)
        wave1["biznes"] = lambda: run(build_biznes_section, body)
    wave1["nawiazanie"] = lambda: run(
        build_nawiazanie_section,
        body=body,
        previous_body=previous_body,
        previous_subject=previous_subject,
        sender=sender,
        sender_name=sender_name,
    )
    if wants_scrabble:
        wave1["scrabble"] = lambda: run(build_scrabble_section, body)

    response_data = _run_parallel(wave1, flask_app)

    # ── WYSYŁKA PO FALI 1 ─────────────────────────────────────────────────────
    html_fala1 = "".join(filter(None, [
        response_data.get("zwykly",     {}).get("reply_html", ""),
        response_data.get("biznes",     {}).get("reply_html", ""),
        response_data.get("nawiazanie", {}).get("reply_html", ""),
        response_data.get("scrabble",   {}).get("reply_html", ""),
    ]))
    if html_fala1.strip() and wants_text_reply:
        wyslij_odpowiedz(
            to_email   = sender,
            to_name    = sender_name,
            subject    = f"Re: {previous_subject or 'Twoja wiadomość'}",
            html_body  = html_fala1,
            zalaczniki = zbierz_zalaczniki_z_response(
                {k: response_data[k] for k in ("zwykly", "biznes", "scrabble")
                 if k in response_data}
            ),
        )

    # ── FALA 2: ciężkie respondery ────────────────────────────────────────────
    wave2 = {}
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
        )

    if wave2:
        response_data.update(_run_parallel(wave2, flask_app))

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
                 ("obrazek", "emocje", "analiza", "generator_pdf", "smierc")
                 if k in response_data}
            ),
        )

    # Zabezpieczenie — nawiazanie zawsze ma has_history
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {
            "has_history": False, "reply_html": "", "analysis": ""
        }

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

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_gif1 = executor.submit(gen_gif1)
        future_gif2 = executor.submit(gen_gif2)
        gif1_b64    = future_gif1.result()
        gif2_b64    = future_gif2.result()

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
