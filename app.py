#!/usr/bin/env python3
"""
app.py
Webhook backend dla Google Apps Script.

ARCHITEKTURA (ta wersja):
  /webhook → natychmiast 200 accepted → daemon thread wykonuje pipeline

KOLEJNOŚĆ SEKCJI (stała, niezależna od GAS):
  nawiazanie → analiza → zwykly → smierc → generator_pdf → biznes → scrabble → emocje
"""

import os
import html
import json
import re
import threading
import urllib.parse
import traceback  # [POPRAWKA] Przeniesiono z dołu na górę, aby działał wewnątrz funkcji webhook
from datetime import datetime

from flask import (
    Flask,
    request,
    jsonify,
    current_app,
    send_from_directory,
    make_response,
)
import requests as http_requests

from drive_utils import (
    upload_file_to_drive,
    update_sheet_with_data,
    save_to_history_sheet,
)
from core.logging_reporter import init_logger, get_logger

# Importy core
from core.hf_token_manager import hf_tokens
from core.responder_manager import ResponderManager, PipelineBuilder
from core.job_runner import run_pipeline_async, build_section_order
from core.resource_manager import ResourceManager
from core.validator import Validator
from core.sheets_logger import log_odebrano

app = Flask(__name__)

# ── Czas startu aplikacji ───────────────────────────────────────────────────
start_time = datetime.now()

# ── Globalne liczniki ──────────────────────────────────────────────────────
total_emails_processed = 0
last_error_time = None
last_error_message = None


def update_stats():
    """Aktualizuje globalne statystyki."""
    global total_emails_processed
    total_emails_processed += 1


def no_cache_response(response):
    """Ustawia nagłówki, aby przeglądarka i proxy nie cachowały strony statusu."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def log_error(error_msg):
    """Loguje błąd i zapisuje ostatni błąd."""
    global last_error_time, last_error_message
    last_error_time = datetime.now()
    last_error_message = str(error_msg)
    app.logger.error(f"System error: {error_msg}")


# ── Inicjalizacja managerów ─────────────────────────────────────────────────
responder_manager = ResponderManager()
pipeline_builder = PipelineBuilder(responder_manager)
validator = Validator(responder_manager.config)
resource_manager = ResourceManager(
    memory_threshold_mb=responder_manager.config.get("performance", {}).get(
        "memory_threshold_mb", 400
    ),
    max_concurrent=responder_manager.config.get("performance", {}).get(
        "max_concurrent_pipelines", 5
    ),
)


# ── Health check dla GAS ────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health_check():
    """Zwraca status systemu w formacie HTML lub JSON w zależności od Accept header."""
    try:
        mem_info = resource_manager.get_memory_usage()
        uptime = datetime.now() - start_time

        uptime_seconds = int(uptime.total_seconds())
        uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"

        mem_extra = {}
        try:
            import psutil

            proc = psutil.Process()
            proc_mem = proc.memory_info()
            rss_mb = round(proc_mem.rss / 1024 / 1024, 2)
            vms_mb = round(proc_mem.vms / 1024 / 1024, 2)
            num_threads = proc.num_threads()

            # Szacowanie limitu RAM kontenera (Render domyślnie 512MB dla Free)
            container_limit_mb = int(os.getenv("RENDER_MEMORY_LIMIT_MB", "512"))
            sys_used_mb = rss_mb
            sys_available_mb = round(max(container_limit_mb - rss_mb, 0), 2)
            sys_percent = round(min(rss_mb / container_limit_mb * 100, 100), 1)

            mem_extra = {
                "rss_mb": rss_mb,
                "vms_mb": vms_mb,
                "sys_total_mb": container_limit_mb,
                "sys_available_mb": sys_available_mb,
                "sys_used_mb": sys_used_mb,
                "sys_percent": sys_percent,
                "proc_percent": sys_percent,
                "num_threads": num_threads,
            }
        except Exception:
            mem_extra = {
                "rss_mb": round(mem_info.get("rss_mb", 0), 2),
                "vms_mb": 0,
                "sys_total_mb": 512,
                "sys_available_mb": 0,
                "sys_used_mb": 0,
                "sys_percent": round(mem_info.get("percent", 0), 1),
                "proc_percent": round(mem_info.get("percent", 0), 2),
                "num_threads": 0,
            }

        all_responders = responder_manager.config.get("responders", {})
        enabled_responders = [
            k for k, v in all_responders.items() if v.get("enabled", False)
        ]
        disabled_responders = [
            k for k, v in all_responders.items() if not v.get("enabled", False)
        ]

        status_data = {
            "status": "active",
            "version": "Tyler v6",
            "active_pipelines": _active_pipelines,
            "memory_usage_mb": mem_extra["rss_mb"],
            "memory_percent": mem_extra["proc_percent"],
            "uptime": uptime_str,
            "total_emails_processed": total_emails_processed,
            "timestamp": datetime.now().isoformat(),
            "mem_extra": mem_extra,
            "last_error": (
                {
                    "time": last_error_time.isoformat() if last_error_time else None,
                    "message": (
                        last_error_message[:200] + "..."
                        if last_error_message and len(last_error_message) > 200
                        else last_error_message
                    ),
                }
                if last_error_message
                else None
            ),
            "config": {
                "max_concurrent_pipelines": responder_manager.config.get(
                    "performance", {}
                ).get("max_concurrent_pipelines", 5),
                "memory_threshold_mb": responder_manager.config.get(
                    "performance", {}
                ).get("memory_threshold_mb", 400),
                "enabled_responders": enabled_responders,
                "disabled_responders": disabled_responders,
            },
        }

        # Jeśli request chce JSON
        if request.headers.get("Accept", "").find("application/json") != -1:
            response = make_response(jsonify(status_data), 200)
            return no_cache_response(response)

        # Generowanie HTML dla przeglądarki
        mem_color = "#28a745"
        if mem_extra["sys_percent"] > 80:
            mem_color = "#dc3545"
        elif mem_extra["sys_percent"] > 50:
            mem_color = "#ffc107"

        def responder_rows(names, icon):
            if not names:
                return '<div style="color:#999;font-style:italic;padding:4px 0;">brak</div>'
            return "".join(
                f'<div style="padding:5px 0;border-bottom:1px solid #f0f0f0;font-family:monospace;font-size:14px;">'
                + f"{icon} {name}</div>"
                for name in names
            )

        enabled_rows = responder_rows(enabled_responders, "\u2705")
        disabled_rows = responder_rows(disabled_responders, "\u274c")

        error_html = ""
        if status_data.get("last_error"):
            err = status_data["last_error"]
            error_html = (
                '<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:16px;margin:12px 0;">'
                + '<div style="font-weight:bold;color:#856404;margin-bottom:8px;">\u26a0\ufe0f Ostatni B\u0142\u0105d</div>'
                + '<div style="font-family:monospace;font-size:13px;color:#333;">'
                + f"Czas: {err['time']}<br>B\u0142\u0105d: {html.escape(str(err['message']))}"
                + "</div></div>"
            )

        disabled_section = ""
        if disabled_responders:
            disabled_section = (
                '<div class="card"><div class="card-title">\u274c Respondery wyłączone ('
                + str(len(disabled_responders))
                + ")</div>"
                + disabled_rows
                + "</div>"
            )

        sys_bar_pct = min(mem_extra["sys_percent"], 100)
        now_str = status_data["timestamp"][:19].replace("T", " ")

        html_response = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<title>Tyler v6 - Status</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:16px;color:#222}}
.wrap{{max-width:680px;margin:0 auto}}
h1{{font-size:1.3em;margin-bottom:4px}}
.badge{{display:inline-block;background:#28a745;color:white;border-radius:10px;padding:2px 12px;font-size:13px;font-weight:bold;margin-bottom:14px}}
.card{{background:white;border-radius:10px;padding:14px 18px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.07)}}
.card-title{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#888;margin-bottom:10px;font-weight:700}}
.row{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f2f2f2;font-size:14px}}
.row:last-child{{border-bottom:none}}
.lbl{{color:#555}}
.val{{font-weight:600;font-family:monospace}}
.bar-wrap{{background:#eee;border-radius:4px;height:7px;margin:10px 0 3px 0}}
.bar{{height:7px;border-radius:4px;background:{mem_color};width:{sys_bar_pct:.1f}%}}
.bar-lbl{{font-size:11px;color:#999;text-align:right}}
.footer{{text-align:center;font-size:12px;color:#bbb;margin-top:14px}}
.footer a{{color:#999}}
</style>
</head>
<body>
<div class="wrap">
<div style="margin-bottom:14px">
<h1>\U0001f916 AutoResponder AI Text \u2014 Tyler v6</h1>
<span class="badge">System Aktywny</span>
</div>

<div class="card">
<div class="card-title">\U0001f4ca Pami\u0119\u0107 procesu (ten serwer)</div>
<div class="row"><span class="lbl">RAM procesu (RSS)</span><span class="val">{mem_extra["rss_mb"]} MB</span></div>
<div class="row"><span class="lbl">RAM wirtualna (VMS)</span><span class="val">{mem_extra["vms_mb"]} MB</span></div>
<div class="row"><span class="lbl">% RAM procesu</span><span class="val">{mem_extra["proc_percent"]} %</span></div>
<div class="row"><span class="lbl">W\u0105tki procesu</span><span class="val">{mem_extra["num_threads"]}</span></div>
</div>

<div class="card">
<div class="card-title">\U0001f5a5\ufe0f Pami\u0119\u0107 systemu (kontener Render)</div>
<div class="row"><span class="lbl">Ca\u0142kowita RAM</span><span class="val">{mem_extra["sys_total_mb"]} MB</span></div>
<div class="row"><span class="lbl">U\u017cywana RAM</span><span class="val">{mem_extra["sys_used_mb"]} MB</span></div>
<div class="row"><span class="lbl">Dost\u0119pna RAM</span><span class="val">{mem_extra["sys_available_mb"]} MB</span></div>
<div class="bar-wrap"><div class="bar"></div></div>
<div class="bar-lbl">Zaj\u0119te: {mem_extra["sys_percent"]} % RAM kontenera</div>
</div>

<div class="card">
<div class="card-title">\u2699\ufe0f Pipeline i dzia\u0142anie</div>
<div class="row"><span class="lbl">Aktywne pipeline'y</span><span class="val">{status_data["active_pipelines"]}</span></div>
<div class="row"><span class="lbl">Maks. wsp\u00f3\u0142bie\u017cno\u015b\u0107</span><span class="val">{status_data["config"]["max_concurrent_pipelines"]}</span></div>
<div class="row"><span class="lbl">Pr\u00f3g RAM (limit)</span><span class="val">{status_data["config"]["memory_threshold_mb"]} MB</span></div>
<div class="row"><span class="lbl">Przetworzone emaile</span><span class="val">{status_data["total_emails_processed"]}</span></div>
<div class="row"><span class="lbl">Uptime</span><span class="val">{status_data["uptime"]}</span></div>
</div>

<div class="card">
<div class="card-title">\u2705 Respondery w\u0142\u0105czone ({len(enabled_responders)})</div>
{enabled_rows}
</div>

{disabled_section}

{error_html}

<div class="footer">
Ostatnia aktualizacja: {now_str} &nbsp;|&nbsp; <a href="/debug">🔍 Debug pipeline</a> &nbsp;|&nbsp; <a href="/status">JSON API</a> &nbsp;|&nbsp; Auto-refresh co 30s
</div>
</div>
</body>
</html>"""

        response = make_response(html_response, 200)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return no_cache_response(response)

    except Exception as e:
        log_error(str(e))
        return f"B\u0142\u0105d systemu: {str(e)}", 500


# ── Szczegółowy status systemu ──────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def system_status():
    """Zwraca szczegółowy status systemu w formacie JSON."""
    try:
        mem_info = resource_manager.get_memory_usage()
        uptime = datetime.now() - start_time
        uptime_seconds = int(uptime.total_seconds())
        uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"

        status = {
            "status": "active",
            "version": "Tyler v6",
            "active_pipelines": _active_pipelines,
            "memory_usage_mb": round(mem_info["rss_mb"], 2),
            "memory_percent": round(mem_info["percent"], 2),
            "uptime": uptime_str,
            "total_emails_processed": total_emails_processed,
            "timestamp": datetime.now().isoformat(),
            "last_error": (
                {
                    "time": last_error_time.isoformat() if last_error_time else None,
                    "message": last_error_message,
                }
                if last_error_message
                else None
            ),
            "config": {
                "max_concurrent_pipelines": responder_manager.config.get(
                    "performance", {}
                ).get("max_concurrent_pipelines", 5),
                "memory_threshold_mb": responder_manager.config.get(
                    "performance", {}
                ).get("memory_threshold_mb", 400),
                "enabled_responders": [
                    k
                    for k, v in responder_manager.config.get("responders", {}).items()
                    if v.get("enabled", False)
                ],
            },
        }
        response = make_response(jsonify(status), 200)
        return no_cache_response(response)
    except Exception as e:
        response = make_response(jsonify({"status": "error", "message": str(e)}), 500)
        return no_cache_response(response)


# ── Favicon ──────────────────────────────────────────────────────────────────
@app.route("/favicon.ico", methods=["GET"])
def favicon():
    """Zwraca favicon.ico z katalogu images."""
    try:
        image_dir = os.path.join(app.root_path, "images")
        return send_from_directory(image_dir, "favicon.ico", mimetype="image/x-icon")
    except Exception as e:
        app.logger.warning(f"Favicon error: {e}")
        return "", 404


# ── Zarządzanie stanem pipeline ──────────────────────────────────────────────
# Te zmienne i locki służą do śledzenia co robi aktualnie uruchomiony pipeline
# widoczne przez /debug

import threading as _threading

_pipeline_lock = _threading.Lock()
_active_pipelines = 0

_pipeline_state: dict = {
    "message_id": None,
    "sender": None,
    "sender_name": None,
    "subject": None,
    "body": None,
    "started_at": None,
    "finished_at": None,
    "status": "idle",  # idle, running, done, error
    "sections_requested": [],
    "sections": {},
    "combined_reply_html": None,
    "emails_sent": 0,
    "history": [],  # lista 10 ostatnich pipelineów
}
_pipeline_state_lock = _threading.Lock()


def _pipeline_start():
    global _active_pipelines
    with _pipeline_lock:
        _active_pipelines += 1
    resource_manager.pipeline_start()


def _pipeline_done():
    global _active_pipelines
    with _pipeline_lock:
        _active_pipelines = max(0, _active_pipelines - 1)
    resource_manager.pipeline_end()


def _state_pipeline_start(message_id, sender, sender_name, subject, body, sections):
    """Inicjuje stan pipeline przed startem."""
    with _pipeline_state_lock:
        # Archiwizuj obecny stan do historii jeśli istnieje
        if _pipeline_state.get("status") in ("done", "error") and _pipeline_state.get(
            "started_at"
        ):
            _pipeline_state["history"].insert(
                0,
                {
                    "started_at": _pipeline_state["started_at"],
                    "finished_at": _pipeline_state.get("finished_at"),
                    "sender": _pipeline_state.get("sender"),
                    "subject": _pipeline_state.get("subject"),
                    "sections": list(_pipeline_state.get("sections", {}).keys()),
                    "status": _pipeline_state.get("status"),
                    "emails_sent": _pipeline_state.get("emails_sent", 0),
                },
            )
            _pipeline_state["history"] = _pipeline_state["history"][:10]

        _pipeline_state.update(
            {
                "message_id": message_id,
                "sender": sender,
                "sender_name": sender_name,
                "subject": subject,
                "body": body,
                "started_at": datetime.now().isoformat(),
                "finished_at": None,
                "status": "running",
                "sections_requested": list(sections),
                "sections": {},
                "combined_reply_html": None,
                "emails_sent": 0,
            }
        )


def _state_section_start(section_key):
    with _pipeline_state_lock:
        _pipeline_state["sections"][section_key] = {
            "status": "running",
            "started": datetime.now().isoformat(),
            "duration_sec": None,
            "reply_html": None,
            "reply_preview": None,
            "error": None,
            "attachments": [],
        }


def _state_section_done(section_key, result, duration_sec):
    with _pipeline_state_lock:
        s = _pipeline_state["sections"].setdefault(section_key, {})
        s["status"] = "done"
        s["duration_sec"] = round(duration_sec, 2)
        if isinstance(result, dict):
            html_content = result.get("reply_html", "") or ""
            s["reply_html"] = html_content
            s["reply_preview"] = (
                html_content[:300] + "..." if len(html_content) > 300 else html_content
            )
            # Lista załączników z rezultatu
            att_fields = [
                "pdf",
                "image",
                "image2",
                "emoticon",
                "cv_pdf",
                "raport_pdf",
                "gra_html",
                "plakat_svg",
                "horoskop_pdf",
                "karta_rpg_pdf",
                "ankieta_pdf",
                "ankieta_html",
                "debug_txt",
            ]
            s["attachments"] = [f for f in att_fields if result.get(f)]
            lists = ["triptych", "images", "videos", "docs", "docx_list"]
            for lf in lists:
                if result.get(lf):
                    s["attachments"].append(f"{lf}({len(result[lf])})")


def _state_section_error(section_key, error_msg):
    with _pipeline_state_lock:
        s = _pipeline_state["sections"].setdefault(section_key, {})
        s["status"] = "error"
        s["error"] = str(error_msg)[:500]


def _state_section_empty(section_key):
    with _pipeline_state_lock:
        s = _pipeline_state["sections"].setdefault(section_key, {})
        s["status"] = "empty"


def _state_pipeline_done(combined_html, emails_sent):
    with _pipeline_state_lock:
        _pipeline_state["finished_at"] = datetime.now().isoformat()
        _pipeline_state["status"] = "done"
        _pipeline_state["combined_reply_html"] = combined_html
        _pipeline_state["emails_sent"] = emails_sent


# ═══════════════════════════════════════════════════════════════════════════════
# OAUTH — scope'y i zarządzanie tokenami
# ═══════════════════════════════════════════════════════════════════════════════

REQUIRED_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_valid_access_token() -> str:
    """Sprawdza/odświeża token OAuth z env."""
    access_token = os.getenv("GMAIL_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

    if access_token:
        try:
            # Szybka weryfikacja ważności
            r = http_requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
                timeout=8,
            )
            info = r.json()
            expires_in = int(info.get("expires_in", 0))
            if "error" not in info and expires_in > 30:
                # Token jest OK, ale sprawdźmy scope
                granted_scope = info.get("scope", "")
                if "gmail.send" not in granted_scope:
                    app.logger.error("[oauth] ⚠ Token nie posiada uprawnień gmail.send")
                    raise RuntimeError("Brak uprawnień gmail.send")
                return access_token
        except Exception as e:
            app.logger.warning("[oauth] Błąd weryfikacji access_token: %s", e)

    # Odświeżanie
    if not refresh_token or not client_id or not client_secret:
        raise RuntimeError(
            "Brak danych OAuth do odświeżenia tokenu (refresh_token/client_id/secret)"
        )

    try:
        app.logger.info("[oauth] Odświeżanie access_token za pomocą refresh_token...")
        r2 = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        token_data = r2.json()
        if "access_token" in token_data:
            new_token = token_data["access_token"]
            # Zapisujemy do os.environ aby proces pamiętał w tej sesji
            os.environ["GMAIL_ACCESS_TOKEN"] = new_token
            app.logger.info("[oauth] ✅ Token odświeżony pomyślnie.")
            return new_token
        else:
            raise RuntimeError(f"Błąd odświeżania: {token_data.get('error_description', token_data.get('error'))}")
    except Exception as e:
        raise RuntimeError(f"Krytyczny błąd OAuth: {e}")


@app.route("/oauth/init", methods=["GET"])
def oauth_init():
    """Generuje link do autoryzacji Google (do ręcznego wywołania raz)."""
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(REQUIRED_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        params
    )
    return (
        f'<html><body><p>Kliknij poniżej, aby połączyć Tyler v6 z Google:</p><a href="{auth_url}">➜ ZALOGUJ PRZEZ GOOGLE</a>'
        + f"<br><br><small>Redirect URI: {redirect_uri}</small></body></html>"
    )


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Odbiera kod od Google i wymienia na tokeny."""
    code = request.args.get("code")
    if not code:
        return "Błąd: Brak kodu autoryzacji.", 400

    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"

    try:
        resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        tokens = resp.json()
        if "error" in tokens:
            return f"Błąd wymiany kodu: {tokens}"

        res = f"""
        <h3>✅ Autoryzacja zakończona!</h3>
        <p>Skopiuj poniższe tokeny do zmiennych środowiskowych Render:</p>
        <pre style="background:#eee;padding:10px;">
GMAIL_ACCESS_TOKEN: {tokens.get('access_token')}
GMAIL_REFRESH_TOKEN: {tokens.get('refresh_token')}
        </pre>
        """
        return res
    except Exception as e:
        return f"Błąd: {e}", 500


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK — główny endpoint
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Odbiera email od GAS, natychmiast wraca 200, a potem w tle puszcza pipeline.
    """
    from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

    try:
        # Wymuszenie JSONa niezależnie od Content-Type (GAS czasem nie wysyła nagłówka)
        data = request.get_json(force=True, silent=True)
        if not data:
            app.logger.warning("[webhook] Brak poprawnych danych JSON w żądaniu")
            return jsonify({"accepted": False, "error": "Brak danych JSON"}), 400

        # Wyciąganie pól
        message_id = data.get("message_id", "")
        sender = data.get("sender", "")
        sender_name = data.get("sender_name", "")
        subject = data.get("subject", "")
        body = data.get("body", "")

        # Kontekst z Google Sheets (opcjonalny)
        drive_folder_id = data.get("drive_folder_id", "")
        history_sheet_id = data.get("history_sheet_id", "")
        smierc_sheet_id = data.get("smierc_sheet_id", "")

        # Flagi sterujące
        save_to_drive = data.get("save_to_drive", True)
        skip_save_to_history = data.get("skip_save_to_history", False)

        if not sender:
            app.logger.warning("[webhook] Brak nadawcy (sender)")
            return jsonify({"accepted": False, "error": "Brak nadawcy"}), 400

        # [POPRAWKA] Walidacja (teraz zwraca 2 wartości, więc rozpakowanie działa)
        is_valid, validation_error = validator.validate_email(sender, subject, body)
        if not is_valid:
            app.logger.warning("[webhook] Walidacja odrzuciła: %s", validation_error)
            return jsonify({"accepted": False, "error": validation_error}), 400

        # Sprawdzenie zasobów
        if not resource_manager.can_start_pipeline():
            app.logger.warning(
                "[webhook] Odrzucono request: przekroczono limit zasobów lub wątków"
            )
            return jsonify({"accepted": False, "error": "Resource limit"}), 503

        # ── Budowanie planu zadań (Pipeline) ──────────────────────────────────
        # build_sections() przyjmuje słownik flag — mapujemy pola z webhooka
        pipeline_data = {
            "contains_keyword":               data.get("containsKeyword",  False) or data.get("contains_keyword",  False),
            "contains_keyword1":              data.get("containsKeyword1", False) or data.get("contains_keyword1", False),
            "contains_keyword2":              data.get("containsKeyword2", False) or data.get("contains_keyword2", False),
            "contains_keyword3":              data.get("containsKeyword3", False) or data.get("contains_keyword3", False),
            "contains_keyword4":              data.get("containsKeyword4", False) or data.get("contains_keyword4", False),
            "contains_keyword_joker":         data.get("containsJoker",    False) or data.get("contains_keyword_joker", False),
            "contains_keyword_smierc":        data.get("contains_keyword_smierc", False),
            "contains_keyword_generator_pdf": data.get("contains_keyword_generator_pdf", False),
            "wants_smierc":                   data.get("isSmierc",         False) or data.get("wants_smierc", False),
            "wants_scrabble":                 data.get("wants_scrabble",   False),
            "wants_analiza":                  data.get("wants_analiza",    False),
            "wants_emocje":                   data.get("wants_emocje",     False),
            "wants_generator_pdf":            data.get("wants_generator_pdf", False) or data.get("contains_keyword_generator_pdf", False),
            "wants_biznes":                   data.get("isBiz",            False) or data.get("wants_biznes", False),
            "previous_body":                  data.get("previous_body",    ""),
            "in_history_status":              "tak" if (data.get("isAllowed") or data.get("isKnownSender")) else "",
            "in_requiem_status":              "tak" if data.get("isSmierc") else "",
        }
        section_names = pipeline_builder.build_sections(pipeline_data)

        # Mapowanie nazw sekcji na callable — każdy responder importowany lazy
        # żeby nie ładować wszystkich modułów przy starcie serwera
        def _make_task(name):
            _sender        = sender
            _sender_name   = sender_name
            _body          = body
            _data          = data
            _prev_body     = data.get("previous_body", "")
            _attachments   = data.get("attachments", [])
            _smierc_data   = data.get("smircData") or {}
            _disable_flux  = data.get("disable_flux", False) or data.get("contains_flaga_test", False)

            if name == "zwykly":
                def fn():
                    from responders.zwykly import build_zwykly_section
                    return build_zwykly_section(
                        body=_body,
                        previous_body=_prev_body,
                        sender_email=_sender,
                        sender_name=_sender_name,
                        test_mode=_disable_flux,
                        attachments=_attachments,
                    )
                return fn
            elif name == "smierc":
                def fn():
                    from responders.smierc import build_smierc_section
                    return build_smierc_section(
                        sender_email=_sender,
                        body=_body,
                        etap=_smierc_data.get("etap", 1),
                        data_smierci_str=_smierc_data.get("data_smierci", "nieznanego dnia"),
                        historia=_smierc_data.get("historia", []),
                        data=_data,
                    )
                return fn
            elif name == "biznes":
                def fn():
                    from responders.biznes import build_biznes_section
                    return build_biznes_section(body=_body, sender_email=_sender, sender_name=_sender_name, data=_data)
                return fn
            elif name == "scrabble":
                def fn():
                    from responders.scrabble import build_scrabble_section
                    return build_scrabble_section(body=_body, sender_email=_sender, data=_data)
                return fn
            elif name == "emocje":
                def fn():
                    from responders.emocje import build_emocje_section
                    return build_emocje_section(body=_body, sender_email=_sender, data=_data)
                return fn
            elif name == "generator_pdf":
                def fn():
                    from responders.generator_pdf import build_generator_pdf_section
                    return build_generator_pdf_section(body=_body, sender_email=_sender, data=_data)
                return fn
            elif name == "nawiazanie":
                def fn():
                    from responders.nawiazanie import build_nawiazanie_section
                    return build_nawiazanie_section(body=_body, previous_body=_prev_body, previous_subject=_data.get("previous_subject"), sender=_sender, sender_name=_data.get("sender_name", ""))
                return fn
            elif name == "analiza":
                def fn():
                    from responders.analiza import build_analiza_section
                    return build_analiza_section(body=_body, sender_email=_sender, attachments=_attachments, data=_data)
                return fn
            else:
                app.logger.warning("[webhook] Nieznana sekcja: %s — pomijam", name)
                return None

        tasks = {}
        for _name in section_names:
            _fn = _make_task(_name)
            if _fn:
                tasks[_name] = _fn

        if not tasks:
            app.logger.info("[webhook] Brak zadań do wykonania dla tego emaila.")
            return jsonify({"accepted": False, "error": "Brak zadań"}), 200

        # Inicjalizacja loggera sekcji
        # session_id = skrót message_id + sender żeby log był identyfikowalny
        _session_id = (message_id or "")[:16] + "_" + (sender or "").split("@")[0][:12]
        logger = init_logger(session_id=_session_id)
        logger.log_input(sender=sender, subject=subject, body=body, sender_name=sender_name)

        # Logowanie "ODEBRANO" do arkusza (opcjonalne)
        if history_sheet_id:
            try:
                log_odebrano(history_sheet_id, message_id, sender, subject, body)
            except Exception as e:
                app.logger.warning("Błąd podczas log_odebrano: %s", e)

        # ── Uruchomienie Pipeline w tle ──────────────────────────────────────
        _state_pipeline_start(
            message_id, sender, sender_name, subject, body, list(tasks.keys())
        )
        _pipeline_start()

        _pipeline_kwargs = {
            "flask_app": app,
            "data": data,
            "message_id": message_id,
            "tasks": tasks,
            "sender": sender,
            "sender_name": sender_name,
            "previous_subject": data.get("previous_subject", ""),
            "drive_folder_id": drive_folder_id,
            "history_sheet_id": history_sheet_id,
            "smierc_sheet_id": smierc_sheet_id,
            "save_to_drive": save_to_drive,
            "skip_save_to_history": skip_save_to_history,
            "logger": logger,
            "wyslij_fn": wyslij_odpowiedz,
            "zbierz_zalaczniki_fn": zbierz_zalaczniki_z_response,
            "get_token_fn": _get_valid_access_token,
            "on_section_start": _state_section_start,
            "on_section_done": _state_section_done,
            "on_section_error": _state_section_error,
            "on_section_empty": _state_section_empty,
            "on_pipeline_done": _state_pipeline_done,
        }

        def _pipeline_wrapper(**kwargs):
            import logging as _logging
            import traceback as _tb
            _tlog = _logging.getLogger("pipeline_thread")
            try:
                _tlog.error("[thread] START WĄTKU — tasks: %s", list(kwargs.get("tasks", {}).keys()))
                run_pipeline_async(**kwargs)
                _tlog.error("[thread] KONIEC WĄTKU OK")
            except Exception as _ex:
                _tlog.error("[thread] BŁĄD W WĄTKU: %s\n%s", _ex, _tb.format_exc())
                with kwargs["flask_app"].app_context():
                    kwargs["flask_app"].logger.error("[thread] BŁĄD: %s\n%s", _ex, _tb.format_exc())
            finally:
                _pipeline_done()

        thread = threading.Thread(
            target=_pipeline_wrapper,
            kwargs=_pipeline_kwargs,
            daemon=True,
        )
        thread.start()

        update_stats()
        return jsonify({"accepted": True, "message_id": message_id}), 200

    except Exception as e:
        # [POPRAWKA] traceback.format_exc() zadziała poprawnie
        app.logger.error("[webhook] Błąd krytyczny: %s\n%s", e, traceback.format_exc())
        log_error(str(e))
        return jsonify({"accepted": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG — stan ostatniego pipeline
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/debug", methods=["GET"])
def debug_pipeline():
    """Zwraca szczegółowy stan ostatniego pipeline."""
    with _pipeline_state_lock:
        state = dict(_pipeline_state)
        # Usuń duże pola HTML z odpowiedzi JSON
        if "combined_reply_html" in state and state["combined_reply_html"]:
            state["combined_reply_html"] = (
                state["combined_reply_html"][:500] + "..."
                if len(state["combined_reply_html"]) > 500
                else state["combined_reply_html"]
            )
        for sk, sv in state.get("sections", {}).items():
            if "reply_html" in sv and sv["reply_html"]:
                sv["reply_html"] = (
                    sv["reply_html"][:300] + "..."
                    if len(sv["reply_html"]) > 300
                    else sv["reply_html"]
                )
        return jsonify(state), 200


# ═══════════════════════════════════════════════════════════════════════════════
# Uruchomienie
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Import traceback na dole jest już niepotrzebny, bo jest na górze pliku
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)