#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.
Wszystkie respondery uruchamiane RÓWNOLEGLE przez ThreadPoolExecutor.
Dzięki temu czas odpowiedzi = czas najwolniejszego respondera, nie suma wszystkich.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    # ── Pola nadawcy i historia ───────────────────────────────────────────────
    sender           = data.get("sender",      "")
    sender_name      = data.get("sender_name", "")
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None
    attachments      = data.get("attachments") or []

    # ── Flagi żądania ─────────────────────────────────────────────────────────
    wants_scrabble = bool(data.get("wants_scrabble"))
    wants_analiza  = bool(data.get("wants_analiza"))
    wants_emocje   = bool(data.get("wants_emocje"))
    wants_obrazek  = bool(data.get("wants_obrazek"))

    flask_app = app._get_current_object()

    # ── Pomocnicza funkcja: uruchom w app context ─────────────────────────────
    def run(fn, *args, **kwargs):
        with flask_app.app_context():
            return fn(*args, **kwargs)

    # ── Zbuduj listę zadań do równoległego wykonania ──────────────────────────
    tasks = {
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
        tasks["scrabble"] = lambda: run(build_scrabble_section, body)
    if wants_analiza:
        tasks["analiza"]  = lambda: run(build_analiza_section, body, attachments)
    if wants_emocje:
        tasks["emocje"]   = lambda: run(build_emocje_section, body, attachments)
    if wants_obrazek:
        tasks["obrazek"]  = lambda: run(build_obrazek_section, body)

    # ── Uruchom wszystkie równolegle ──────────────────────────────────────────
    response_data = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                response_data[key] = future.result()
            except Exception as e:
                app.logger.error("Błąd responderu '%s': %s", key, e)
                # Zwróć pustą sekcję zamiast crashować cały webhook
                response_data[key] = {}

    # Upewnij się że nawiazanie zawsze ma has_history (zabezpieczenie)
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {"has_history": False, "reply_html": "", "analysis": ""}

    # ── Logowanie ─────────────────────────────────────────────────────────────
    app.logger.info(
        "Response: biznes=%s | zwykly=%s | scrabble=%s | analiza=%s | emocje=%s | obrazek=%s | nawiazanie=%s | sender=%s",
        bool(response_data.get("biznes", {}).get("pdf",  {}).get("base64")),
        bool(response_data.get("zwykly", {}).get("pdf",  {}).get("base64")),
        "tak" if "scrabble" in response_data else "nie",
        "tak" if "analiza"  in response_data else "nie",
        "tak" if "emocje"   in response_data else "nie",
        "tak" if "obrazek"  in response_data else "nie",
        "tak" if response_data.get("nawiazanie", {}).get("has_history") else "nie",
        sender_name or sender or "(brak)",
    )

    return jsonify(response_data), 200


if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony — wywołania AI zwrócą None.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
