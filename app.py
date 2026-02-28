#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.
Odbiera żądania i deleguje do odpowiednich responderów.

Aby dodać nowy responder:
1. Stwórz plik responders/nowy.py z funkcją build_nowy_section(body)
2. Dodaj import poniżej
3. Dodaj wywołanie w webhook() i klucz w response_data
4. W Google Apps Script dodaj obsługę nowego klucza
"""
import os
from flask import Flask, request, jsonify

from responders.zwykly   import build_zwykly_section
from responders.biznes   import build_biznes_section
from responders.scrabble import build_scrabble_section
from responders.analiza  import build_analiza_section

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    data    = request.json or {}
    body    = data.get("body", "")

    if not body or not body.strip():
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # ── Zawsze generowane ─────────────────────────────────────────────────────
    response_data = {
        "zwykly":  build_zwykly_section(body),
        "biznes":  build_biznes_section(body),
    }

    # ── Generowane tylko na żądanie (flaga wants_scrabble z Apps Script) ──────
    if data.get("wants_scrabble"):
        response_data["scrabble"] = build_scrabble_section(body)

    # ── Analiza powtórzeń (flaga wants_analiza z Apps Script) ─────────────────
    if data.get("wants_analiza"):
        # attachments — lista [{base64, name}, ...] od Apps Script
        attachments = data.get("attachments") or []
        response_data["analiza"] = build_analiza_section(body, attachments)

    # ── Logowanie ─────────────────────────────────────────────────────────────
    app.logger.info(
        "Response: biznes.pdf=%s | zwykly.pdf=%s | scrabble=%s | analiza=%s",
        bool(response_data["biznes"].get("pdf",   {}).get("base64")),
        bool(response_data["zwykly"].get("pdf",   {}).get("base64")),
        "tak" if "scrabble" in response_data else "nie",
        "tak" if "analiza"  in response_data else "nie",
    )

    return jsonify(response_data), 200


if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony — wywołania AI zwrócą None.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
