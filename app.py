#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.

KOLEJNOŚĆ WYSYŁKI (priorytetowa):
  1. zwykly    — zawsze, jeśli nadawca jest znany lub contains_keyword
  2. smierc    — jeśli wants_smierc (requiem aktywne)
  3. dociekliwy (analiza / Eryk) — jeśli wants_analiza lub KEYWORDS3
  4. pozostałe — biznes, scrabble, emocje, generator_pdf, nawiazanie

NAPRAWIONE BŁĘDY (wersja oryginalna):
  - [BUG] attachments (zmienna lokalna) nadpisywała zewnętrzny parametr
  - [BUG] smierc nie był uruchamiany w Wave 1/2 mimo wants_smierc=True
  - [BUG] dociekliwy był wywoływany podwójnie (zwykly.py + app.py)
  - [BUG] JOKER (zwykly+smierc) powodował timeout 502
  - [BUG] log_txt / log_svg generowane PRZED smierc i dociekliwy
  - [BUG] requested_sections nie zawierał 'smierc' ani 'dociekliwy'
  - [BUG] historia zapisywana podwójnie

NOWE POPRAWKI (ta wersja):
  - [FIX] OAuth — dodano /oauth/init z pełnymi scope'ami (gmail.send + drive + sheets)
  - [FIX] OAuth — access_token jest automatycznie odświeżany gdy wygaśnie
  - [FIX] OAuth — /oauth/status do diagnostyki tokenów
  - [FIX] Lambda capture bug — zmienne loop captures naprawione przez default args
  - [FIX] wyslij_odpowiedz używa _get_valid_access_token() zamiast gołego env
"""

import os
import base64
import html
import io
import json
import re
import traceback
import urllib.parse

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

from core.hf_token_manager import hf_tokens

app = Flask(__name__)

# Warm-up tokenów HF przy starcie serwera (nie czekaj na pierwsze żądanie)
with app.app_context():
    hf_tokens.warmup()


# ═══════════════════════════════════════════════════════════════════════════════
# OAUTH — scope'y i zarządzanie tokenami
# ═══════════════════════════════════════════════════════════════════════════════

REQUIRED_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_valid_access_token() -> str:
    """
    Zwraca ważny Gmail access_token.

    Logika:
    1. Pobiera GMAIL_ACCESS_TOKEN z env.
    2. Weryfikuje przez Google tokeninfo API.
    3. Jeśli wygasł lub niepoprawny — odświeża przez refresh_token.
    4. Nowy token zapisuje do os.environ (do czasu restartu procesu).
    5. Jeśli refresh nie możliwy — rzuca RuntimeError z instrukcją.
    """
    access_token  = os.getenv("GMAIL_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    client_id     = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

    # --- Sprawdź czy obecny access_token jest ważny ---
    if access_token:
        try:
            r = http_requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
                timeout=8,
            )
            info = r.json()
            expires_in = int(info.get("expires_in", 0))
            if "error" not in info and expires_in > 30:
                # Token ważny — sprawdź czy ma gmail.send scope
                granted = info.get("scope", "")
                if "gmail.send" not in granted:
                    app.logger.error(
                        "[oauth] ⚠ access_token nie ma scope gmail.send! "
                        "Wejdź na /oauth/init i autoryzuj ponownie. Scope: %s", granted
                    )
                    raise RuntimeError(
                        "GMAIL_ACCESS_TOKEN nie ma scope gmail.send. "
                        "Wejdź na /oauth/init i autoryzuj ponownie."
                    )
                return access_token
        except RuntimeError:
            raise
        except Exception as e:
            app.logger.warning("[oauth] Błąd weryfikacji tokeninfo: %s", e)

    # --- Token wygasł lub pusty — odśwież ---
    if not refresh_token:
        raise RuntimeError(
            "GMAIL_ACCESS_TOKEN wygasł i brak GMAIL_REFRESH_TOKEN. "
            "Wejdź na /oauth/init i autoryzuj aplikację ponownie."
        )
    if not client_id or not client_secret:
        raise RuntimeError(
            "Brak GMAIL_CLIENT_ID lub GMAIL_CLIENT_SECRET w env. "
            "Sprawdź zmienne środowiskowe w Render."
        )

    app.logger.warning("[oauth] access_token wygasł — odświeżam przez refresh_token...")

    try:
        r2 = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=15,
        )
        data = r2.json()
    except Exception as e:
        raise RuntimeError(f"Błąd HTTP przy odświeżaniu tokenu: {e}")

    if "access_token" not in data:
        err_desc = data.get("error_description", data.get("error", str(data)))
        raise RuntimeError(
            f"Odświeżenie tokenu nie powiodło się: {err_desc}. "
            f"Wejdź na /oauth/init i autoryzuj ponownie."
        )

    new_token = data["access_token"]
    os.environ["GMAIL_ACCESS_TOKEN"] = new_token
    app.logger.warning(
        "[oauth] ✅ Token odświeżony. Zaktualizuj GMAIL_ACCESS_TOKEN w Render: %s...",
        new_token[:24],
    )
    return new_token


# ═══════════════════════════════════════════════════════════════════════════════
# OAUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/oauth/init", methods=["GET"])
def oauth_init():
    """
    Krok 1: Wygeneruj URL do Google z PEŁNYMI scope'ami i wyświetl link.
    Odwiedź: GET /oauth/init
    """
    client_id    = os.getenv("GMAIL_CLIENT_ID", "").strip()
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"

    if not client_id:
        return "<h2>Błąd:</h2><p>Brak GMAIL_CLIENT_ID w zmiennych środowiskowych Render.</p>", 500

    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         " ".join(REQUIRED_OAUTH_SCOPES),
        # access_type=offline  → Google zwróci refresh_token (ważny wieczyście)
        "access_type":   "offline",
        # prompt=consent  → WYMUSZA ekran zgody nawet jeśli aplikacja była już autoryzowana
        # KRYTYCZNE: bez tego Google NIE zwróci nowego refresh_token przy ponownej auth
        "prompt":        "consent",
    }

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    scope_items = "".join(f"<li><code>{s}</code></li>" for s in REQUIRED_OAUTH_SCOPES)

    return f"""
    <html><head><title>OAuth Init</title></head>
    <body style="font-family:monospace;padding:40px;max-width:900px">
    <h2>Autoryzacja Gmail OAuth 2.0</h2>
    <p>Kliknij link aby zalogować się przez Google i przyznać wszystkie uprawnienia:</p>
    <p style="margin:24px 0">
        <a href="{auth_url}" style="font-size:20px;color:white;background:#1a73e8;
           padding:12px 24px;border-radius:6px;text-decoration:none">
           ➜ Zaloguj się przez Google
        </a>
    </p>
    <hr>
    <p><strong>Scope'y które zostaną przyznane:</strong></p>
    <ul>{scope_items}</ul>
    <p style="color:red"><strong>Ważne:</strong> Na ekranie Google kliknij
    "Zezwól" na <em>wszystkie</em> uprawnienia.</p>
    </body></html>
    """, 200


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """
    Krok 2: Google przekierowuje tutaj z kodem. Wymieniamy code → tokeny.
    """
    error = request.args.get("error")
    if error:
        return f"<h2>Błąd autoryzacji Google:</h2><p>{error}</p>", 400

    code = request.args.get("code")
    if not code:
        return "<p>Brak kodu autoryzacyjnego. Wróć do <a href='/oauth/init'>/oauth/init</a>.</p>", 400

    client_id     = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    redirect_uri  = request.url_root.rstrip("/") + "/oauth/callback"

    if not client_id or not client_secret:
        return "<p>Brak GMAIL_CLIENT_ID lub GMAIL_CLIENT_SECRET w env.</p>", 500

    try:
        resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()
    except Exception as e:
        app.logger.error("[oauth] Błąd wymiany kodu: %s", e)
        return f"<h2>Błąd wymiany kodu:</h2><pre>{e}</pre>", 500

    access_token  = tokens.get("access_token", "BRAK")
    refresh_token = tokens.get("refresh_token", "")
    scope_granted = tokens.get("scope", "")
    expires_in    = tokens.get("expires_in", "?")

    app.logger.info("[oauth] Tokeny uzyskane — scope: %s", scope_granted)

    # Sprawdź scope'y
    missing = [s for s in REQUIRED_OAUTH_SCOPES if s not in scope_granted]
    scope_status_html = (
        "<p style='color:green'>✅ Wszystkie scope'y przyznane.</p>"
        if not missing else
        "<p style='color:red'>⚠️ Brakujące scope'y: " + ", ".join(missing) +
        ". <a href='/oauth/init'>Autoryzuj ponownie</a>.</p>"
    )

    # Ostrzeżenie o braku refresh_token
    refresh_warning = ""
    if not refresh_token:
        refresh_warning = """
        <div style="background:#fff3cd;border:1px solid #ffc107;padding:16px;
                    border-radius:8px;margin:16px 0">
        <strong>⚠️ Brak refresh_token!</strong><br>
        Google zwraca refresh_token tylko przy pierwszej autoryzacji lub z
        <code>prompt=consent</code>. Wejdź na
        <a href='/oauth/init'>/oauth/init</a> i autoryzuj ponownie.
        </div>
        """
        refresh_token = "(nie zwrócony przez Google — uruchom /oauth/init ponownie)"

    return f"""
    <html><head><title>OAuth OK</title></head>
    <body style="font-family:monospace;padding:40px;max-width:900px">
    <h2>✅ OAuth zakończony</h2>
    {refresh_warning}
    {scope_status_html}
    <p><strong>Token wygasa za:</strong> {expires_in} sekund (~1 godzina)</p>
    <hr>
    <h3>📋 Skopiuj do Render → Environment Variables:</h3>
    <table style="border-collapse:collapse;width:100%">
    <tr style="background:#e8f4f8">
        <th style="padding:10px;border:1px solid #ccc;text-align:left">Zmienna</th>
        <th style="padding:10px;border:1px solid #ccc;text-align:left">Wartość</th>
    </tr>
    <tr>
        <td style="padding:10px;border:1px solid #ccc"><strong>GMAIL_ACCESS_TOKEN</strong></td>
        <td style="padding:10px;border:1px solid #ccc;word-break:break-all">
            <code>{access_token}</code></td>
    </tr>
    <tr>
        <td style="padding:10px;border:1px solid #ccc"><strong>GMAIL_REFRESH_TOKEN</strong></td>
        <td style="padding:10px;border:1px solid #ccc;word-break:break-all">
            <code>{refresh_token}</code></td>
    </tr>
    </table>
    <p style="color:#888;font-size:13px;margin-top:20px">
    refresh_token nie wygasa — trzymaj go bezpiecznie.<br>
    access_token wygasa po ~1h. Aplikacja odświeży go automatycznie
    używając refresh_token.
    </p>
    <hr>
    <p><a href="/oauth/status">➜ Sprawdź status tokenów</a></p>
    </body></html>
    """, 200


@app.route("/oauth/status", methods=["GET"])
def oauth_status():
    """
    Diagnostyka tokenów: sprawdza env, wywołuje tokeninfo Google,
    i próbuje automatycznie odświeżyć wygasły access_token.
    Odwiedź: GET /oauth/status
    """
    access_token  = os.getenv("GMAIL_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    client_id     = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

    # --- Weryfikacja przez tokeninfo ---
    token_info  = {}
    token_error = None
    if access_token:
        try:
            r = http_requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
                timeout=10,
            )
            token_info = r.json()
            if "error" in token_info:
                token_error = token_info.get("error_description", token_info["error"])
        except Exception as e:
            token_error = str(e)
    else:
        token_error = "GMAIL_ACCESS_TOKEN nie ustawiony w env"

    # --- Scope'y ---
    granted_scope = token_info.get("scope", "")
    scope_rows = ""
    for s in REQUIRED_OAUTH_SCOPES:
        icon = "✅" if s in granted_scope else "❌ BRAKUJE"
        scope_rows += (
            f"<tr><td style='padding:6px 12px'>{s}</td>"
            f"<td style='padding:6px 12px'>{icon}</td></tr>"
        )

    # --- Próba odświeżenia jeśli token wygasł ---
    refresh_result_html = ""
    if token_error and refresh_token and client_id and client_secret:
        try:
            r2 = http_requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type":    "refresh_token",
                },
                timeout=15,
            )
            rdata = r2.json()
            if "access_token" in rdata:
                new_tok = rdata["access_token"]
                os.environ["GMAIL_ACCESS_TOKEN"] = new_tok
                refresh_result_html = f"""
                <div style="background:#d4edda;border:1px solid #28a745;
                            padding:16px;border-radius:8px;margin:16px 0">
                <strong>✅ Token odświeżony automatycznie!</strong><br>
                Skopiuj nowy GMAIL_ACCESS_TOKEN do Render env:<br>
                <code style="word-break:break-all">{new_tok}</code>
                </div>"""
                token_error = None
            else:
                refresh_result_html = f"""
                <div style="background:#f8d7da;border:1px solid #dc3545;
                            padding:16px;border-radius:8px;margin:16px 0">
                <strong>❌ Odświeżenie nie powiodło się:</strong>
                <pre>{json.dumps(rdata, indent=2)}</pre>
                <a href="/oauth/init">➜ Autoryzuj ponownie</a>
                </div>"""
        except Exception as e:
            refresh_result_html = f"<p style='color:red'>Błąd odświeżania: {e}</p>"

    env_row = lambda name, val: (
        f"<tr><td style='padding:6px 12px'><strong>{name}</strong></td>"
        f"<td style='padding:6px 12px'>{'✅ ustawiony' if val else '❌ BRAK'}</td></tr>"
    )
    error_html = (
        f"<p style='color:red'>⚠ Błąd tokeninfo: {token_error}</p>" if token_error else ""
    )

    return f"""
    <html><head><title>OAuth Status</title></head>
    <body style="font-family:monospace;padding:40px;max-width:900px">
    <h2>🔍 Status OAuth Tokenów</h2>
    {error_html}
    {refresh_result_html}

    <h3>Zmienne środowiskowe</h3>
    <table style="border-collapse:collapse">
    {env_row("GMAIL_CLIENT_ID", client_id)}
    {env_row("GMAIL_CLIENT_SECRET", client_secret)}
    {env_row("GMAIL_ACCESS_TOKEN", access_token)}
    {env_row("GMAIL_REFRESH_TOKEN", refresh_token)}
    </table>

    <h3>Token Info (Google tokeninfo API)</h3>
    <table style="border-collapse:collapse">
    <tr><td style='padding:6px 12px'><strong>email</strong></td>
        <td style='padding:6px 12px'>{token_info.get("email", "—")}</td></tr>
    <tr><td style='padding:6px 12px'><strong>expires_in</strong></td>
        <td style='padding:6px 12px'>{token_info.get("expires_in", "wygasł lub błąd")} s</td></tr>
    </table>

    <h3>Scope'y</h3>
    <table style="border-collapse:collapse;border:1px solid #ccc">
    <tr style="background:#e8f4f8">
        <th style="padding:6px 12px">Wymagany scope</th>
        <th style="padding:6px 12px">Status</th>
    </tr>
    {scope_rows}
    </table>
    <hr>
    <p><a href="/oauth/init">➜ Autoryzuj ponownie</a></p>
    <p><a href="/admin/hf-status">➜ Stan tokenów HF (FLUX)</a></p>
    </body></html>
    """, 200


# ═══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTYKA TOKENÓW HF (FLUX)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/hf-status")
def hf_status():
    """Diagnostyka stanu tokenów HF — tylko do debugowania."""
    return jsonify({
        "warmed_up": hf_tokens._warmed_up,
        "tokens":    hf_tokens.status_report(),
    })

@app.route("/admin/hf-reset", methods=["POST"])
def hf_reset():
    """Resetuje cache tokenów — ponowny warm-up przy następnym żądaniu."""
    hf_tokens.reset()
    return jsonify({"status": "ok", "message": "Warm-up zostanie powtórzony przy następnym żądaniu"})


# ═══════════════════════════════════════════════════════════════════════════════
# POMOCNIKI
# ═══════════════════════════════════════════════════════════════════════════════

def _run_sequential(tasks: dict, flask_app) -> dict:
    """
    Uruchamia słownik {klucz: lambda} SEKWENCYJNIE, zwraca {klucz: wynik}.
    Sekwencyjność zapobiega timeout 502 przy równoległych ciężkich calach AI
    (zwykly + smierc + FLUX = crash workera).
    """
    results = {}
    for key, fn in tasks.items():
        try:
            flask_app.logger.info("[pipeline] START: %s", key)
            results[key] = fn()
            flask_app.logger.info("[pipeline] OK:    %s", key)
        except Exception as e:
            flask_app.logger.error(
                "[pipeline] BŁĄD responderu '%s': %s\n%s",
                key, e, traceback.format_exc()
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
        folder_id,
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
        return (
            '<svg width="1000" height="300" xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 1000 300">'
            '<text x="10" y="150" font-size="16" font-family="Arial">'
            "Brak danych logowania</text></svg>"
        )

    input_data     = next((e for e in entries if e["type"] == "INPUT"), None)
    api_calls      = [e for e in entries if e["type"] == "API_CALL"]
    section_results = [e for e in entries if e["type"] == "SECTION_RESULT"]
    decisions      = [e for e in entries if e["type"] == "DECISION"]

    deepseek_all     = [e for e in api_calls if e["data"].get("api") == "deepseek"]
    deepseek_success = sum(1 for e in deepseek_all if e["data"].get("success"))
    deepseek_fail    = len(deepseek_all) - deepseek_success

    sections_ok   = sum(1 for e in section_results if e["data"].get("success"))
    sections_fail = len(section_results) - sections_ok

    first_ts   = entries[0].get("timestamp", 0)
    last_ts    = entries[-1].get("timestamp", 0)
    total_time = last_ts - first_ts

    def escape_xml(text):
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
            .replace("&nbsp;", "&#160;")
        )

    num_timeline_items = min(len(entries), 15)
    height = 200 + len(section_results) * 30 + num_timeline_items * 20 + 200
    width  = 1200

    svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
  <defs>
    <style>
      .box     {{ fill: #e8f4f8; stroke: #0066cc; stroke-width: 2; }}
      .success {{ fill: #d4edda; stroke: #28a745; stroke-width: 2; }}
      .error   {{ fill: #f8d7da; stroke: #dc3545; stroke-width: 2; }}
      .warning {{ fill: #fff3cd; stroke: #ffc107; stroke-width: 2; }}
      .info    {{ fill: #d1ecf1; stroke: #17a2b8; stroke-width: 2; }}
      .text    {{ font-family: 'Courier New', monospace; font-size: 12px; }}
      .title   {{ font-weight: bold; font-size: 16px; }}
      .subtitle {{ font-size: 11px; fill: #666; }}
      .metric  {{ font-size: 11px; font-weight: bold; }}
    </style>
  </defs>
  <rect width="{width}" height="{height}" fill="#fafafa" stroke="#ddd" stroke-width="1"/>
  <rect x="0" y="0" width="{width}" height="40" fill="#1a1a2e" stroke="none"/>
  <text x="20" y="26" class="title" fill="#e8d5b0">Diagram Przebiegu AutoRespondera</text>
  <text x="{width - 400}" y="26" class="subtitle" fill="#aaa">
    Czas: {total_time:.2f}s | Wpisy: {len(entries)} | Sekcje: {sections_ok}✓ {sections_fail}✗
  </text>
"""
    y_pos = 60

    # Sekcja 1: INPUT
    sender_disp  = input_data["data"].get("sender", "?") if input_data else "?"
    subject_disp = input_data["data"].get("subject", "?") if input_data else "?"
    body_preview = (
        (input_data["data"].get("body_preview", "")[:35] + "...") if input_data else ""
    )
    svg += f"""  <rect x="20" y="{y_pos}" width="340" height="110" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📧 WEJŚCIE</text>
  <text x="30" y="{y_pos+42}" class="text">Nadawca: {escape_xml(sender_disp)}</text>
  <text x="30" y="{y_pos+60}" class="text">Temat: {escape_xml(subject_disp[:35])}</text>
  <text x="30" y="{y_pos+78}" class="text">Treść: {escape_xml(body_preview)}</text>
"""
    y_pos += 140

    # Sekcja 2: DECYZJE
    if decisions:
        svg += f"""  <rect x="20" y="{y_pos}" width="340" height="{50 + min(len(decisions), 4) * 20}" class="info"/>
  <text x="30" y="{y_pos+20}" class="title">🎯 DECYZJE: {len(decisions)}</text>
"""
        for i, decision in enumerate(decisions[:4]):
            result        = decision["data"].get("result", "?")
            decision_text = decision["data"].get("decision", "N/A")[:25]
            svg += f'  <text x="30" y="{y_pos+40+i*18}" class="text">• {decision_text} → {result}</text>\n'
        y_pos += 70 + min(len(decisions), 4) * 20

    # Sekcja 3: API
    svg += f"""  <rect x="20" y="{y_pos}" width="1160" height="130" class="{'success' if deepseek_success > 0 else 'error'}"/>
  <text x="30" y="{y_pos+20}" class="title">⚙️ API CALLS</text>
  <text x="40" y="{y_pos+50}" class="metric">DEEPSEEK PRÓBY: {len(deepseek_all)}</text>
  <text x="40" y="{y_pos+68}" class="metric">DEEPSEEK SKUTECZNE: {deepseek_success}</text>
  <text x="40" y="{y_pos+86}" class="metric">DEEPSEEK NIEUDANE: {deepseek_fail}</text>
"""
    y_pos += 160

    # Sekcja 4: HARMONOGRAM
    svg += f"""  <rect x="20" y="{y_pos}" width="1160" height="{60 + num_timeline_items * 20}" class="warning"/>
  <text x="30" y="{y_pos+20}" class="title">⏱️ HARMONOGRAM PIERWSZYCH {num_timeline_items} ETAPÓW</text>
"""
    for i, entry in enumerate(entries[:num_timeline_items]):
        ts         = entry.get("timestamp", 0)
        entry_type = entry["type"][:18]
        delta      = ts - first_ts
        pct        = (delta / total_time * 100) if total_time > 0 else 0
        svg += f'  <rect x="30" y="{y_pos+35+i*20}" width="{pct*8}" height="16" fill="#ffc107" opacity="0.6" stroke="none"/>\n'
        svg += f'  <text x="40" y="{y_pos+47+i*20}" class="text">+{delta:5.2f}s [{entry_type:18s}]</text>\n'
    y_pos += 80 + num_timeline_items * 20

    # Sekcja 5: SEKCJE RESPONDENTÓW
    section_details = [
        (e["data"].get("section"), e["data"].get("success")) for e in section_results
    ]
    svg += f"""  <rect x="20" y="{y_pos}" width="1160" height="{60 + max(len(section_details), 1) * 28}" class="box"/>
  <text x="30" y="{y_pos+20}" class="title">📋 SEKCJE: {sections_ok}✓ {sections_fail}✗</text>
"""
    for i, (section_name, success) in enumerate(section_details):
        box_class = "success" if success else "error"
        status    = "✓" if success else "✗"
        svg += f'  <rect x="30" y="{y_pos+35+i*28}" width="1140" height="24" class="{box_class}"/>\n'
        svg += f'  <text x="40" y="{y_pos+53+i*28}" class="text">{status} {(section_name or "UNKNOWN").upper()}</text>\n'

    svg += """  <defs>
    <marker id="arrowhead" markerWidth="10" markerHeight="10" refX="5" refY="5" orient="auto">
      <polygon points="0,0 10,5 0,10" fill="#0066cc"/>
    </marker>
  </defs>
</svg>"""
    return svg


def _build_log_txt_content(logger, response_data) -> str:
    api_calls      = [e for e in logger.entries if e["type"] == "API_CALL"]
    deepseek_calls = [e for e in api_calls if e["data"].get("api") == "deepseek"]
    deepseek_success = sum(1 for e in deepseek_calls if e["data"].get("success"))
    deepseek_total   = len(deepseek_calls)

    nouns_dict = response_data.get("zwykly", {}).get("nouns_dict", {})
    detected_nouns = [
        v for v in (nouns_dict.values() if isinstance(nouns_dict, dict) else [])
        if isinstance(v, str) and v.strip()
    ]

    section_results  = [e for e in logger.entries if e["type"] == "SECTION_RESULT"]
    sections_success = sum(1 for e in section_results if e["data"].get("success"))
    sections_total   = len(section_results)
    keywords_used    = logger.metadata.get("keywords_used", False)

    lines = []
    lines.append("=" * 88)
    lines.append("PODSUMOWANIE WYKONANIA AUTORESPONDERA")
    lines.append("=" * 88)
    lines.append(f"Start: {logger.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Sesja: {logger.session_id}")
    lines.append("")

    lines.append("0. METADANE SESJI")
    keyword_labels = []
    for kw in [
        "contains_keyword", "contains_keyword1", "contains_keyword2",
        "contains_keyword3", "contains_keyword4", "contains_flaga_test",
        "contains_keyword_joker",
    ]:
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
    section_keys = [k for k in response_data if k not in ("log_txt", "log_svg")]
    for section_name in sorted(section_keys):
        section_data = response_data.get(section_name, {})
        if not isinstance(section_data, dict):
            continue
        has_html = bool(section_data.get("reply_html", "").strip())
        has_att  = bool(
            section_data.get("docx_list") or section_data.get("images")
        )
        status = "✓" if (has_html or has_att) else "✗"
        lines.append(f"  {status} {section_name.upper()}")
        if has_html:
            lines.append(f"      - HTML: {len(section_data.get('reply_html', ''))} znaków")
        docs  = section_data.get("docx_list", [])
        imgs  = section_data.get("images", [])
        names = [d.get("filename") for d in docs if isinstance(d, dict) and d.get("filename")]
        names += [d.get("filename") for d in imgs if isinstance(d, dict) and d.get("filename")]
        if names:
            lines.append(f"      - Pliki: {', '.join(names)}")
    lines.append("")

    lines.append("4. HARMONOGRAM")
    if logger.entries:
        t0 = logger.entries[0].get("timestamp", 0)
        t1 = logger.entries[-1].get("timestamp", 0)
        lines.append(f"- Czas całkowity: {(t1-t0):.2f}s")
        for i, entry in enumerate(logger.entries[:10]):
            delta = entry.get("timestamp", 0) - t0
            lines.append(f"  [{i+1:2d}] +{delta:6.2f}s: {entry['type'][:20]}")
    lines.append("")

    lines.append("5. SZCZEGÓŁOWE WPISY")
    for entry in logger.entries:
        ts = entry.get("timestamp", 0.0)
        lines.append(f"[{entry['type']}] +{ts:.2f}s")
        lines.extend(_format_log_entry_data(entry.get("data")))
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

def _wyslij_responder(
    responder_key: str,
    resp_data: dict,
    sender: str,
    sender_name: str,
    previous_subject: str,
    logger,
) -> bool:
    """
    Wysyła jeden email dla danego respondera.
    Zwraca True jeśli wysłanie się powiodło.

    Używa lokalnej zmiennej `zal` zamiast `attachments`,
    żeby nie nadpisywać listy attachmentów z requestu.

    Przed wysyłką pobiera ważny access_token (odświeża jeśli wygasł).
    """
    section = resp_data.get(responder_key)
    if not section or not isinstance(section, dict):
        return False

    email_html = section.get("reply_html", "")
    zal        = zbierz_zalaczniki_z_response({responder_key: section})

    if not email_html.strip() and not zal:
        return False

    subject_line = f"Re: {previous_subject or 'Twoja wiadomość'}"
    if responder_key == "smierc" and section.get("subject"):
        subject_line = section["subject"]

    # Odśwież token przed wysyłką — jeśli wygasł, zostanie automatycznie odnowiony
    try:
        _get_valid_access_token()
    except RuntimeError as e:
        app.logger.error("[send] ⚠ Brak ważnego tokenu — nie wysyłam %s: %s", responder_key, e)
        logger.log_decision("token_error", f"{responder_key}: {e}", False)
        return False

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
    session_id = render_instance_id or None
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
            "status":  "blocked",
            "reason":  "sender_is_admin_email",
            "sender":  sender,
        }), 200

    # ── Parametry requestu ────────────────────────────────────────────────────
    previous_body    = data.get("previous_body")    or None
    previous_subject = data.get("previous_subject") or None
    # req_attachments trzymamy osobno żeby nigdy nie zostały nadpisane przez loop
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

    has_any_keyword = any([
        contains_keyword, contains_keyword1, contains_keyword2,
        contains_keyword3, contains_keyword4, contains_keyword_joker,
    ])

    for meta_key, meta_val in [
        ("has_any_keyword",        has_any_keyword),
        ("contains_keyword",       contains_keyword),
        ("contains_keyword1",      contains_keyword1),
        ("contains_keyword2",      contains_keyword2),
        ("contains_keyword3",      contains_keyword3),
        ("contains_keyword4",      contains_keyword4),
        ("contains_flaga_test",    contains_flaga_test),
        ("contains_keyword_joker", contains_keyword_joker),
    ]:
        logger.set_metadata(meta_key, meta_val)
    if matched_keywords:
        logger.set_metadata("matched_keywords", matched_keywords)

    logger.log_variables_detected({
        "sender":               sender,
        "sender_name":          sender_name,
        "has_previous_body":    bool(previous_body),
        "num_attachments":      len(req_attachments),
        "save_to_drive":        save_to_drive,
        "test_mode":            test_mode,
        "disable_flux":         disable_flux,
        "contains_keyword":     contains_keyword,
        "contains_keyword1":    contains_keyword1,
        "contains_keyword2":    contains_keyword2,
        "contains_keyword3":    contains_keyword3,
        "contains_keyword4":    contains_keyword4,
        "contains_flaga_test":  contains_flaga_test,
        "contains_keyword_joker": contains_keyword_joker,
        "wants_smierc":         wants_smierc,
        "wants_analiza":        wants_analiza,
        "wants_biznes":         wants_biznes,
        "wants_scrabble":       wants_scrabble,
        "wants_emocje":         wants_emocje,
        "wants_generator_pdf":  wants_generator_pdf,
        "is_retry":             is_retry,
        "attempt_count":        attempt_count,
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

        # 1. ZWYKLY — priorytet 1
        if contains_keyword or in_history_status == "tak":
            requested_sections.append("zwykly")
            logger.log_decision("zwykly", "known sender or keyword", "dodano")

        # 2. SMIERC — priorytet 2
        if wants_smierc:
            requested_sections.append("smierc")
            logger.log_decision("smierc", "wants_smierc=True", "dodano")

        # 3. DOCIEKLIWY (ANALIZA / ERYK) — priorytet 3
        if wants_analiza or contains_keyword3:
            requested_sections.append("analiza")
            logger.log_decision("analiza", "wants_analiza or keyword3", "dodano")

        # 4. POZOSTAŁE
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

    app.logger.info(
        "[pipeline] Zaplanowane sekcje (w kolejności): %s",
        " → ".join(requested_sections),
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # POBIERZ DANE ŚMIERCI — PRZED uruchomieniem respondentów
    # ═══════════════════════════════════════════════════════════════════════════

    smierc_etap     = int(data.get("etap", 1))
    smierc_data_str = data.get("data_smierci", "nieznanego dnia")
    smierc_historia = data.get("historia", [])

    if wants_smierc:
        app.logger.info(
            "Smierc data dla %s: etap=%d data=%s", sender, smierc_etap, smierc_data_str
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # BUDOWANIE TASKS
    #
    # UWAGA: Używamy domyślnych argumentów lambdy (_key=key) żeby uniknąć
    # klasycznego Python closure bug — bez tego wszystkie lambdy łapią
    # ostatnią wartość zmiennej loop zamiast bieżącej.
    #
    # Czy zwykly dostaje załączniki?
    # Jeśli "analiza" jest osobno w pipeline — NIE (app.py wywoła ją sam).
    # Zapobiega podwójnemu wywołaniu AI.
    # ═══════════════════════════════════════════════════════════════════════════

    zwykly_attachments = req_attachments if "analiza" not in requested_sections else []
    effective_test_mode = disable_flux or test_mode

    tasks: dict = {}

    for section_key in requested_sections:

        if section_key == "zwykly":
            tasks["zwykly"] = lambda \
                    _body=body, _prev=previous_body, _sender=sender, \
                    _sname=sender_name, _att=zwykly_attachments, \
                    _tm=effective_test_mode, \
                    _skip=("analiza" in requested_sections): \
                run(
                    build_zwykly_section,
                    _body, _prev, _sender, _sname,
                    test_mode=_tm,
                    attachments=_att,
                    skip_dociekliwy=_skip,
                )

        elif section_key == "smierc":
            tasks["smierc"] = lambda \
                    _sender=sender, _body=body, \
                    _etap=smierc_etap, _ds=smierc_data_str, \
                    _hist=smierc_historia, _tm=effective_test_mode: \
                run(
                    build_smierc_section,
                    sender_email     = _sender,
                    body             = _body,
                    etap             = _etap,
                    data_smierci_str = _ds,
                    historia         = _hist,
                    test_mode        = _tm,
                )

        elif section_key == "analiza":
            tasks["analiza"] = lambda \
                    _body=body, _att=req_attachments, \
                    _sender=sender, _sname=sender_name, \
                    _tm=effective_test_mode: \
                run(
                    build_analiza_section,
                    _body, _att,
                    sender      = _sender,
                    sender_name = _sname,
                    test_mode   = _tm,
                )

        elif section_key == "nawiazanie":
            tasks["nawiazanie"] = lambda \
                    _body=body, _prev=previous_body, \
                    _prevs=previous_subject, _sender=sender, \
                    _sname=sender_name: \
                run(
                    build_nawiazanie_section,
                    body             = _body,
                    previous_body    = _prev,
                    previous_subject = _prevs,
                    sender           = _sender,
                    sender_name      = _sname,
                )

        elif section_key == "biznes":
            tasks["biznes"] = lambda _body=body, _sname=sender_name: \
                run(build_biznes_section, _body, sender_name=_sname)

        elif section_key == "scrabble":
            tasks["scrabble"] = lambda _body=body: \
                run(build_scrabble_section, _body)

        elif section_key == "emocje":
            tasks["emocje"] = lambda _body=body, _sname=sender_name, _tm=effective_test_mode: \
                run(build_emocje_section, _body, sender_name=_sname, test_mode=_tm)

        elif section_key == "generator_pdf":
            tasks["generator_pdf"] = lambda _body=body, _sname=sender_name: \
                run(build_generator_pdf_section, _body, sender_name=_sname)

        else:
            app.logger.warning("[pipeline] Nieznana sekcja ignorowana: %s", section_key)

    # ── WYKONAJ PIPELINE SEKWENCYJNIE ────────────────────────────────────────
    response_data = _run_sequential(tasks, flask_app)

    # Zabezpieczenie — nawiazanie zawsze ma has_history
    if "nawiazanie" not in response_data:
        response_data["nawiazanie"] = {"has_history": False, "reply_html": "", "analysis": ""}

    # ═══════════════════════════════════════════════════════════════════════════
    # WYSYŁKA MAILI — kolejność priorytetowa: zwykly → smierc → analiza → reszta
    # ═══════════════════════════════════════════════════════════════════════════

    SEND_ORDER = ["zwykly", "smierc", "analiza", "nawiazanie", "emocje",
                  "scrabble", "biznes", "generator_pdf"]

    any_sent = False
    for responder_key in SEND_ORDER:
        if responder_key not in response_data:
            continue
        sent = _wyslij_responder(
            responder_key    = responder_key,
            resp_data        = response_data,
            sender           = sender,
            sender_name      = sender_name,
            previous_subject = previous_subject,
            logger           = logger,
        )
        if sent:
            any_sent = True
            app.logger.info("[send] ✓ Wysłano: %s → %s", responder_key, sender)
        else:
            app.logger.info("[send] — Pominięto (brak treści): %s", responder_key)

    # ── Alert dla admina jeśli nic nie wysłano ───────────────────────────────
    if not any_sent and admin_email:
        try:
            _get_valid_access_token()
            wyslij_odpowiedz(
                to_email   = admin_email,
                to_name    = "Admin",
                subject    = f"[ALERT] Brak wysyłki dla: {sender}",
                html_body  = (
                    f"<p>Brak treści do wysłania dla nadawcy: {sender}<br>"
                    f"Sekcje: {list(response_data.keys())}</p>"
                ),
                zalaczniki = [],
            )
            app.logger.warning("Wysłano alert do ADMIN_EMAIL: %s", admin_email)
        except RuntimeError as e:
            app.logger.error("[send] Nie można wysłać alertu — brak tokenu: %s", e)

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
                    "",
                ]]
                update_sheet_with_data(smierc_sheet_id, range_name, values)
                app.logger.info(
                    "Zaktualizowano arkusz śmierci dla %s, nowy etap: %d",
                    sender, smierc_res["nowy_etap"],
                )
            except Exception as e:
                app.logger.error("Błąd aktualizacji arkusza śmierci: %s", e)

    # ── Generuj logi (PO wszystkich responderach) ─────────────────────────────
    log_txt_content = _build_log_txt_content(logger, response_data)
    log_txt_b64     = base64.b64encode(log_txt_content.encode("utf-8")).decode("utf-8")
    response_data["log_txt"] = {
        "base64": log_txt_b64, "content_type": "text/plain", "filename": "log.txt"
    }

    svg_content = _build_log_svg_content(logger)
    log_svg_b64 = base64.b64encode(svg_content.encode("utf-8")).decode("utf-8")
    response_data["log_svg"] = {
        "base64": log_svg_b64, "content_type": "image/svg+xml", "filename": "log.svg"
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
            app.logger.info("Zapisano do Drive: %s", ", ".join(drive_uploads))
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
    response_data["processed_status"] = (
        {"status": "partial", "failed": failed_sections, "attempt_count": attempt_count}
        if failed_sections
        else {"status": "ok"}
    )

    # ── Log końcowy ───────────────────────────────────────────────────────────
    smierc_data_res   = response_data.get("smierc", {})
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
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not os.getenv("API_KEY_DEEPSEEK"):
        app.logger.warning("API_KEY_DEEPSEEK nie ustawiony.")
    if not os.getenv("GMAIL_CLIENT_ID"):
        app.logger.warning("GMAIL_CLIENT_ID nie ustawiony — OAuth nie będzie działać.")
    if not os.getenv("GMAIL_REFRESH_TOKEN"):
        app.logger.warning(
            "GMAIL_REFRESH_TOKEN nie ustawiony — tokeny nie będą odświeżane. "
            "Wejdź na /oauth/init."
        )
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
