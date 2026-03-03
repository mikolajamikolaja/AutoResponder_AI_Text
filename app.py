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

from responders.zwykly     import build_zwykly_section
from responders.biznes     import build_biznes_section
from responders.scrabble   import build_scrabble_section
from responders.analiza    import build_analiza_section
from responders.emocje     import build_emocje_section
from responders.obrazek    import build_obrazek_section
from responders.nawiazanie import build_nawiazanie_section

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    body = data.get("body", "")

    if not body or not body.strip():
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # ── Pola nadawcy i historia (przesyłane przez Apps Script) ────────────────
    sender           = data.get("sender",      "")
    sender_name      = data.get("sender_name", "")  # "Jan Kowalski" z nagłówka From
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None

    # ── Zawsze generowane ─────────────────────────────────────────────────────
    response_data = {
        "zwykly": build_zwykly_section(body),
        "biznes": build_biznes_section(body),
    }

    # ── Nawiązanie do poprzedniej wiadomości (zawsze sprawdzane) ──────────────
    response_data["nawiazanie"] = build_nawiazanie_section(
        body=body,
        previous_body=previous_body,
        previous_subject=previous_subject,
        sender=sender,
        sender_name=sender_name,
    )

    # ── Generowane tylko na żądanie (flaga wants_scrabble z Apps Script) ──────
    if data.get("wants_scrabble"):
        response_data["scrabble"] = build_scrabble_section(body)

    # ── Analiza powtórzeń (flaga wants_analiza z Apps Script) ─────────────────
    if data.get("wants_analiza"):
        attachments = data.get("attachments") or []
        response_data["analiza"] = build_analiza_section(body, attachments)

    # ── Analiza emocjonalna (flaga wants_emocje z Apps Script) ────────────────
    if data.get("wants_emocje"):
        attachments = data.get("attachments") or []
        response_data["emocje"] = build_emocje_section(body, attachments)

    # ── Obrazek AI (flaga wants_obrazek z Apps Script) ────────────────────────
    if data.get("wants_obrazek"):
        response_data["obrazek"] = build_obrazek_section(body)

    # ── Logowanie ─────────────────────────────────────────────────────────────
    app.logger.info(
        "Response: biznes.pdf=%s | zwykly.pdf=%s | scrabble=%s | analiza=%s | emocje=%s | obrazek=%s | nawiazanie=%s | sender_name=%s",
        bool(response_data["biznes"].get("pdf",  {}).get("base64")),
        bool(response_data["zwykly"].get("pdf",  {}).get("base64")),
        "tak" if "scrabble"                                 in response_data else "nie",
        "tak" if "analiza"                                  in response_data else "nie",
        "tak" if "emocje"                                   in response_data else "nie",
        "tak" if "obrazek"                                  in response_data else "nie",
        "tak" if response_data["nawiazanie"]["has_history"] else "nie (brak historii)",
        sender_name or "(brak)",
    )

    return jsonify(response_data), 200


if __name__ == "__main__":
    if not os.getenv("KLUCZ_GROQ"):
        app.logger.warning("KLUCZ_GROQ nie ustawiony — wywołania AI zwrócą None.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
