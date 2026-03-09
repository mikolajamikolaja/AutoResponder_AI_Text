#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.

Respondery uruchamiane w DWÓCH FALACH równolegle:
  Fala 1 (lekkie — tekst AI): zwykly, biznes, scrabble, nawiazanie
  Fala 2 (ciężkie — obrazy/pliki): obrazek, emocje, analiza, generator_pdf

Nowy responder: generator_pdf
  - Aktywowany przez flagę wants_generator_pdf (słowo kluczowe w GAS)
  - Lub przez ALLOWED_LIST_GENERATOR_PDF (adres email zawsze generuje PDF)
  - Groq → DeepSeek fallback
  - Zwraca PDF jako base64 w polu "generator_pdf"
"""
import os
import base64
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify

from responders.zwykly       import build_zwykly_section
from responders.biznes       import build_biznes_section
from responders.scrabble     import build_scrabble_section
from responders.analiza      import build_analiza_section
from responders.emocje       import build_emocje_section
from responders.obrazek      import build_obrazek_section
from responders.nawiazanie   import build_nawiazanie_section
from responders.gif_maker    import make_gif
from responders.generator_pdf import build_generator_pdf_section

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
                flask_app.logger.error("Błąd responderu '%s': %s", key, e)
                results[key] = {}
    return results


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    body = data.get("body", "")[:4000]

    if not body or not body.strip():
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # ── Pola nadawcy i historia ───────────────────────────────────────────────
    sender           = data.get("sender",      "")
    sender_name      = data.get("sender_name", "")
    previous_body    = (data.get("previous_body") or "")[:1500] or None
    previous_subject = data.get("previous_subject") or None
    attachments      = data.get("attachments") or []

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble       = bool(data.get("wants_scrabble"))
    wants_analiza        = bool(data.get("wants_analiza"))
    wants_emocje         = bool(data.get("wants_emocje"))
    wants_obrazek        = bool(data.get("wants_obrazek"))
    wants_generator_pdf  = bool(data.get("wants_generator_pdf"))

    flask_app = app

    def run(fn, *args, **kwargs):
        with flask_app.app_context():
            return fn(*args, **kwargs)

    # ── FALA 1: lekkie respondery + nawiazanie ────────────────────────────────
    wave1 = {
        "zwykly":    lambda: run(build_zwykly_section, body),
        "biznes":    lambda: run(build_biznes_section, body),
        "nawiazanie": lambda: run(
            build_nawiazanie_section,
            body=body,
            previous_body=previous_body,
            previous_subject=previous_subject,
            sender=sender,
            sender_name=sender_name,
        ),
    }
    if wants_scrabble:
        wave1["scrabble"] = lambda: run(build_scrabble_section, body)

    response_data = _run_parallel(wave1, flask_app)

    # ── FALA 2: ciężkie respondery ────────────────────────────────────────────
    wave2 = {}
    if wants_obrazek:
        wave2["obrazek"] = lambda: run(build_obrazek_section, body)
    if wants_emocje:
        wave2["emocje"]  = lambda: run(build_emocje_section, body, attachments)
    if wants_analiza:
        wave2["analiza"] = lambda: run(build_analiza_section, body, attachments)
    if wants_generator_pdf:
        # Przekazujemy sender_name do PDF (auto-wpisuje imię i nazwisko)
        _sn = sender_name
        _body = body
        wave2["generator_pdf"] = lambda: run(
            build_generator_pdf_section, _body, sender_name=_sn
        )

    if wave2:
        response_data.update(_run_parallel(wave2, flask_app))

    # Zabezpieczenie — nawiazanie zawsze ma has_history
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {
            "has_history": False, "reply_html": "", "analysis": ""
        }

    # ── Logowanie ─────────────────────────────────────────────────────────────
    app.logger.info(
        "Response: biznes=%s | zwykly=%s | scrabble=%s | analiza=%s | emocje=%s "
        "| obrazek=%s | nawiazanie=%s | generator_pdf=%s | sender=%s",
        bool(response_data.get("biznes",    {}).get("pdf",  {}).get("base64")),
        bool(response_data.get("zwykly",    {}).get("pdf",  {}).get("base64")),
        "tak" if "scrabble"       in response_data else "nie",
        "tak" if "analiza"        in response_data else "nie",
        "tak" if "emocje"         in response_data else "nie",
        "tak" if "obrazek"        in response_data else "nie",
        "tak" if response_data.get("nawiazanie", {}).get("has_history") else "nie",
        "tak" if response_data.get("generator_pdf", {}).get("pdf") else "nie",
        sender_name or sender or "(brak)",
    )

    return jsonify(response_data), 200


@app.route("/webhook_gif", methods=["POST"])
def webhook_gif():
    """
    Przyjmuje dwa PNG jako base64, zwraca dwa GIFy jako base64.
    """
    data = request.json or {}
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
        gif1_b64 = future_gif1.result()
        gif2_b64 = future_gif2.result()

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


if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony.")
    if not os.getenv("API_KEY_GROQ"):
        app.logger.warning("API_KEY_GROQ nie ustawiony.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
