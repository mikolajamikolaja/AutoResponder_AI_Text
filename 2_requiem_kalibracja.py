"""
requiem_kalibracja.py  v7
Lokalne narzędzie do testowania promptów i wszystkich responderów projektu.
Umieść w katalogu głównym projektu — obok app.py.

Struktura oczekiwana:
  projekt/
  ├── app.py
  ├── requiem_kalibracja.py   ← ten plik
  ├── responders/
  │   ├── smierc.py, zwykly.py, obrazek.py, analiza.py,
  │   │   emocje.py, biznes.py, scrabble.py, nawiazanie.py,
  │   │   generator_pdf.py, gif_maker.py
  ├── prompts/
  │   ├── test.txt             ← domyślna wiadomość testowa
  │   ├── requiem_*.txt
  │   └── ...
  └── backup/                  ← tworzony automatycznie
      └── Wyniki_HH_MM_SS/
          ├── smierc/          ← wyniki kalibracji etapów
          │   └── api_log.txt  ← pełne payloady API (system+user+response)
          ├── zwykly/
          │   └── api_log.txt
          ├── obrazek/
          └── ...

Klucze API czytane ze zmiennych środowiskowych PC:
  DEEPSEEK_API_KEY, GROQ_API_KEY, HF_TOKEN

Zmiany v7:
  - smierc: obsługa etapów 1-6 / 8-19 / 20-50 / 51+ (Wysłannik)
  - api_log.txt: pełne payloady (system prompt + user msg + odpowiedź API)
    dla KAŻDEGO respondera — ułatwia wykrywanie przekroczenia limitów
  - Spinner etapu rozszerzony do max z pozagrobowe.txt
"""

import os
import re
import sys
import base64
import shutil
import threading
import datetime
import logging
import types
import importlib.util
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

# ── Ścieżki ───────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent          # katalog główny projektu (obok app.py)
PROJECT_DIR    = SCRIPT_DIR
BACKUP_DIR     = PROJECT_DIR / "backup"
RESPONDERS_DIR = PROJECT_DIR / "responders"
PROMPTS_DIR    = PROJECT_DIR / "prompts"
SMIERC_PY      = RESPONDERS_DIR / "smierc.py"

FILE_PAWEL_1_6     = PROMPTS_DIR / "requiem_PAWEL_system_1-6.txt"
FILE_PAWEL_7       = PROMPTS_DIR / "requiem_PAWEL_system_7.txt"
FILE_PAWEL_8_19    = PROMPTS_DIR / "requiem_PAWEL_system_8-19.txt"
FILE_PAWEL_20_50   = PROMPTS_DIR / "requiem_PAWEL_system_20-50.txt"
FILE_WYSLANNIK     = PROMPTS_DIR / "requiem_WYSLANNIK_system_8_.txt"
FILE_FLUX_GROQ_SYS = PROMPTS_DIR / "requiem_WYSLANNIK_flux_groq_system.txt"
FILE_IMAGE_STYLE   = PROMPTS_DIR / "requiem_WYSLANNIK_IMAGE_STYLE.txt"
FILE_POZAGROBOWE   = PROMPTS_DIR / "pozagrobowe.txt"

TODAY = datetime.date.today().strftime("%d.%m.%Y")

# ── Dodaj katalog projektu do sys.path ────────────────────────────────────────
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="[%(levelname)s] %(name)s: %(message)s")


# ── Mock flask.current_app ────────────────────────────────────────────────────
class _MockApp:
    """Podmiana flask.current_app — dziala bez serwera Flask."""
    logger = logging.getLogger("kalibracja")

    @staticmethod
    def __bool__():
        return True


_mock_app = _MockApp()

import unittest.mock as _umock
_flask_patch = _umock.patch("flask.current_app", _mock_app)
_flask_patch.start()


# ── Mock core.* ───────────────────────────────────────────────────────────────
# Wszystkie respondery importuja: core.ai_client, core.files, core.html_builder

# ── Globalny log API — zbiera pełne payloady z całej sesji ───────────────────
_api_call_log: list = []   # lista dict: {responder, api, system, user, response, chars_s, chars_u, chars_r, czas}

def _api_log_entry(responder: str, api: str, system: str, user: str,
                   response: str, czas_s: float):
    """Dodaje wpis do globalnego logu API."""
    _api_call_log.append({
        "responder": responder,
        "api":       api,
        "system":    system or "",
        "user":      user    or "",
        "response":  response or "",
        "chars_s":   len(system or ""),
        "chars_u":   len(user   or ""),
        "chars_r":   len(response or ""),
        "czas":      f"{czas_s:.2f}s",
    })

def build_api_log_txt(responder: str) -> str:
    """Buduje pełny tekst api_log.txt dla danego respondera."""
    sep  = "=" * 70
    sep2 = "-" * 50
    entries = [e for e in _api_call_log if e["responder"] == responder]
    lines   = [
        sep,
        f"API LOG — {responder.upper()}",
        f"Data: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Liczba wywołań API: {len(entries)}",
        sep, "",
    ]
    for i, e in enumerate(entries, 1):
        lines += [
            f"=== WYWOŁANIE #{i} — {e['api']} — {e['czas']} ===",
            f"Znaki system: {e['chars_s']}  |  znaki user: {e['chars_u']}  |  znaki odpowiedź: {e['chars_r']}",
            sep2,
            ">>> SYSTEM PROMPT:",
            e["system"] or "(brak)",
            sep2,
            ">>> USER MESSAGE:",
            e["user"] or "(brak)",
            sep2,
            ">>> ODPOWIEDŹ API:",
            e["response"] or "(brak / błąd)",
            "",
        ]
    if not entries:
        lines.append("(brak wywołań API dla tego respondera)")
    lines.append(sep)
    return "\n".join(lines)


def _deepseek_local(system: str, user: str, model=None, timeout=60,
                    _responder_ctx: str = "?"):
    """Lokalne wywolanie DeepSeek przez OpenAI SDK. Loguje pełny payload."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        _mock_app.logger.warning("Brak DEEPSEEK_API_KEY w srodowisku")
        _api_log_entry(_responder_ctx, "DeepSeek", system, user, "[BRAK KLUCZA API]", 0)
        return None
    import datetime as _dt
    _t0 = _dt.datetime.now()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model=model or "deepseek-chat",
            messages=[
                {"role": "system", "content": system or ""},
                {"role": "user",   "content": user   or ""},
            ],
            max_tokens=800,
            temperature=0.85,
        )
        result = resp.choices[0].message.content.strip()
        czas = (_dt.datetime.now() - _t0).total_seconds()
        _api_log_entry(_responder_ctx, "DeepSeek", system, user, result, czas)
        return result
    except Exception as e:
        czas = (_dt.datetime.now() - _t0).total_seconds()
        _api_log_entry(_responder_ctx, "DeepSeek", system, user, f"[BŁĄD: {e}]", czas)
        _mock_app.logger.warning("DeepSeek blad: %s", e)
        return None


def _extract_clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    return text


def _sanitize_model_output(text):
    return text.strip() if text else ""


def _read_file_base64(path: str):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


def _load_prompt(filename: str, fallback: str = "") -> str:
    path = PROMPTS_DIR / filename
    try:
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
    except Exception:
        pass
    return fallback


def _build_html_reply(text: str) -> str:
    if not text:
        return "<p></p>"
    paragraphs = text.split("\n\n")
    html = ""
    for p in paragraphs:
        p = p.strip()
        if p:
            html += f"<p>{p.replace(chr(10), '<br>')}</p>\n"
    return html or f"<p>{text}</p>"


# Buduj moduły mock i rejestruj w sys.modules
_core_mod  = types.ModuleType("core")
_ai_mod    = types.ModuleType("core.ai_client")
_files_mod = types.ModuleType("core.files")
_html_mod  = types.ModuleType("core.html_builder")

_ai_mod.call_deepseek         = _deepseek_local
_ai_mod.extract_clean_text    = _extract_clean_text
_ai_mod.sanitize_model_output = _sanitize_model_output
_ai_mod.MODEL_TYLER           = "deepseek-chat"
_ai_mod.MODEL_BIZ             = "deepseek-chat"
_ai_mod.MODEL_ANALIZA         = "deepseek-chat"

_files_mod.read_file_base64   = _read_file_base64
_files_mod.load_prompt        = _load_prompt

_html_mod.build_html_reply    = _build_html_reply

_core_mod.ai_client           = _ai_mod
_core_mod.files               = _files_mod
_core_mod.html_builder        = _html_mod

sys.modules["core"]               = _core_mod
sys.modules["core.ai_client"]     = _ai_mod
sys.modules["core.files"]         = _files_mod
sys.modules["core.html_builder"]  = _html_mod


# ── Sync kluczy API: lokalne nazwy → nazwy uzywane przez respondery ───────────
def _sync_env():
    for src, dst in [
        ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        ("DEEPSEEK_API_KEY", "API_KEY_DEEPSEEK"),  # obrazek.py
        ("GROQ_API_KEY",     "API_KEY_GROQ"),       # smierc.py
        ("HF_TOKEN",         "HF_TOKEN"),
    ]:
        val = os.environ.get(src, "").strip()
        if val and not os.environ.get(dst):
            os.environ[dst] = val


_sync_env()


# ── Zaladuj smierc.py ─────────────────────────────────────────────────────────
_smierc    = None
_SMIERC_OK = False
try:
    _spec = importlib.util.spec_from_file_location("smierc", SMIERC_PY)
    _smierc = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_smierc)
    _SMIERC_OK = True
except Exception as _e:
    _mock_app.logger.error("Nie udalo sie zaladowac smierc.py: %s", _e)


# ── Cache zaladowanych responderow ────────────────────────────────────────────
_loaded_responders: dict = {}


def _load_responder(name: str):
    """Laduje modul respondera z responders/<name>.py. Cache — nie laduj dwa razy."""
    if name in _loaded_responders:
        return _loaded_responders[name]
    path = RESPONDERS_DIR / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"Brak pliku: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _loaded_responders[name] = mod
    return mod


# ── Funkcje API dla kalibracji smierc ─────────────────────────────────────────
# ── Aktualny kontekst respondera (ustawiany przed każdym testem) ──────────────
_current_responder: str = "?"

def call_deepseek(system: str, user: str):
    return _deepseek_local(system, user, _responder_ctx=_current_responder)


def call_groq(system: str, user: str, max_tokens: int = 300):
    if not _SMIERC_OK:
        return None
    import datetime as _dt
    _t0 = _dt.datetime.now()
    result = _smierc._call_groq(system, user)
    czas = (_dt.datetime.now() - _t0).total_seconds()
    _api_log_entry(_current_responder, "Groq", system, user, result or "[brak/błąd]", czas)
    return result


def call_llm_email(system: str, user: str):
    r = call_deepseek(system, user)
    if r:
        return r, "DeepSeek"
    r = call_groq(system, user, max_tokens=800)
    if r:
        return r, "Groq (fallback)"
    return None, "brak"


def call_llm_flux(system: str, user: str):
    if _SMIERC_OK:
        return _smierc._generate_flux_prompt(user)
    return None, "smierc.py niedostepny"


def generate_flux_image(prompt: str):
    if not _SMIERC_OK:
        return None, "smierc.py niedostepny"
    result = _smierc._generate_flux_image(prompt)
    if result:
        return base64.b64decode(result["base64"]), None
    return None, "FLUX nie zwrocil obrazka"


def _smierc_info() -> str:
    if not _SMIERC_OK:
        return "smierc.py: BLAD LADOWANIA"
    url   = getattr(_smierc, "HF_API_URL",  "?")
    steps = getattr(_smierc, "HF_STEPS",    "?")
    guid  = getattr(_smierc, "HF_GUIDANCE", "?")
    model = url.split("/")[-1] if "/" in url else url
    return f"smierc.py OK  ->  {model}  steps={steps}  guidance={guid}"


# ── Paleta kolorow UI ─────────────────────────────────────────────────────────
BG      = "#0d0d0d"
BG2     = "#161616"
BG3     = "#1e1e1e"
ACCENT  = "#c8a96e"
ACCENT2 = "#8b5e3c"
FG      = "#e8e0d0"
FG2     = "#a09080"
FG3     = "#6a5a4a"
BTN_BG  = "#2a1f14"
SUCCESS = "#4a7c59"
ERR     = "#7c3a3a"
GREEN   = "#2d6b3a"
GREEN_H = "#3d8f4e"
RED_BTN = "#6b2d2d"
RED_H   = "#8f3d3d"
BORDER  = "#3a2e22"

FONT_MONO  = ("Consolas", 10)
FONT_BTN   = ("Georgia", 9)
FONT_TITLE = ("Georgia", 12, "bold")
FONT_FILE  = ("Consolas", 8)
FONT_LBL   = ("Georgia", 9, "italic")


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_txt(path: Path, fallback="") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return fallback


def load_etapy() -> dict:
    etapy = {}
    try:
        for line in FILE_POZAGROBOWE.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^(\d+)\.\s+(.+)$', line.strip())
            if m:
                etapy[int(m.group(1))] = m.group(2).strip()
    except Exception:
        pass
    return etapy


def ts_now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def elapsed(start: datetime.datetime) -> str:
    s = (datetime.datetime.now() - start).total_seconds()
    return f"{s:.1f}s"


# ── Zapis sesji smierc → backup/Wyniki_.../smierc/ ───────────────────────────
def save_smierc(state: dict) -> Path:
    """
    Zapisuje wyniki kalibracji smierc do backup/Wyniki_HH_MM_SS/smierc/.
    Zwraca sciezke Wyniki_HH_MM_SS (katalog rodzic).
    """
    ts       = datetime.datetime.now().strftime("%H_%M_%S")
    run_dir  = BACKUP_DIR / f"Wyniki_{ts}"
    resp_dir = run_dir / "smierc"
    prom_dir = resp_dir / "prompts"
    prom_dir.mkdir(parents=True, exist_ok=True)

    if state.get("img_bytes"):
        (resp_dir / "niebo_wyslannik.png").write_bytes(state["img_bytes"])

    if state.get("body"):
        (resp_dir / "tekst_zrodlowy.txt").write_text(state["body"], encoding="utf-8")

    debug = (
        "=== REQUIEM RESPONDER — DEBUG FLUX ===\n"
        f"Wygenerowano: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Email provider: {state.get('email_prov','')}\n"
        f"FLUX prompt provider: {state.get('flux_prov','')}\n\n"
        "--- Odpowiedz Wyslannika ---\n"
        f"{state.get('wyslannik','')}\n\n"
        "--- Prompt FLUX ---\n"
        f"{state.get('flux','')}\n\n"
        "--- Parametry FLUX ---\n"
        f"Model: {getattr(_smierc, 'HF_API_URL', '?').split('/')[-1] if _SMIERC_OK else '?'}\n"
        f"steps: {getattr(_smierc, 'HF_STEPS', '?') if _SMIERC_OK else '?'}\n"
        f"guidance: {getattr(_smierc, 'HF_GUIDANCE', '?') if _SMIERC_OK else '?'}\n"
        f"smierc.py: {'zaladowany OK' if _SMIERC_OK else 'BLAD LADOWANIA'}\n"
    )
    (resp_dir / "_.txt").write_text(debug, encoding="utf-8")

    log    = state.get("log", [])
    raport = _build_smierc_raport(state, log)
    (resp_dir / "raport.txt").write_text(raport, encoding="utf-8")

    for f in PROMPTS_DIR.glob("requiem_*.txt"):
        shutil.copy2(f, prom_dir / f.name)
    if SMIERC_PY.exists():
        shutil.copy2(SMIERC_PY, prom_dir / "smierc.py")

    return run_dir


def _build_smierc_raport(state: dict, log: list) -> str:
    sep   = "=" * 60
    lines = [
        sep,
        "REQUIEM KALIBRACJA — RAPORT SESJI (smierc)",
        f"Data: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        sep, "",
        "=== TEKST ZRODLOWY ===",
        state.get("body", "(brak)"), "",
        sep, "=== LOGI ZDARZEN ===", "",
    ]
    lines += log
    lines += ["", sep, "=== WYNIKI GENEROWANIA ===", ""]

    for key, label in [
        ("pawel_1_6",  "ETAP 1-6  — Paweł z zaświatów"),
        ("pawel_7",    "ETAP 7    — Reinkarnacja"),
        ("pawel_8_19", "ETAP 8-19 — Paweł remont nieba"),
        ("pawel_20_50","ETAP 20-50 — Paweł remont kosmiczny"),
        ("wyslannik",  "ETAP 51+  — Wysłannik"),
    ]:
        if state.get(key):
            lines += [
                f"--- {label} ---",
                f"Provider: {state.get(key+'_prov','?')}",
                f"Czas: {state.get(key+'_czas','?')}", "",
                state[key], "",
            ]

    if state.get("flux"):
        lines += [
            sep, "=== PROMPT FLUX ===",
            f"Provider: {state.get('flux_prov','?')}",
            f"Czas: {state.get('flux_czas','?')}", "",
            state["flux"], "",
        ]

    if state.get("img_bytes"):
        lines += [
            sep, "=== OBRAZEK FLUX ===",
            f"Czas: {state.get('img_czas','?')}",
            f"Rozmiar: {len(state['img_bytes']):,} B",
            "Plik: niebo_wyslannik.png", "",
        ]

    lines += [sep, "=== UZYTE PLIKI PROMPTOW ===", ""]
    for f in PROMPTS_DIR.glob("requiem_*.txt"):
        lines.append(f"  {f.name}")
        try:
            lines.append(f.read_text(encoding="utf-8").strip())
        except Exception:
            lines.append("(blad odczytu)")
        lines.append("")

    lines.append(sep)
    return "\n".join(str(l) for l in lines)


# ── Zapis wyniku respondera ───────────────────────────────────────────────────
def _save_responder_result(resp_dir: Path, name: str, result: dict,
                           body: str, elapsed_s: float):
    """
    Zapisuje wszystkie artefakty zwrocone przez respondent + raport diagnostyczny.
    """
    log = [
        f"RESPONDER: {name}",
        f"Czas wykonania: {elapsed_s:.2f}s",
        f"Data: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50,
        f"Tekst wejsciowy ({len(body)} znakow):",
        body[:500] + ("..." if len(body) > 500 else ""),
        "=" * 50,
        "ZWROCONE KLUCZE: " + ", ".join(result.keys()),
        "=" * 50,
        "SZCZEGOLY ARTEFAKTOW:",
    ]

    def _save_b64(data_b64: str, filename: str, label: str):
        try:
            (resp_dir / filename).write_bytes(base64.b64decode(data_b64))
            size_kb = len(data_b64) * 3 // 4 // 1024
            log.append(f"  OK  {label}: {filename}  (~{size_kb} KB)")
        except Exception as e:
            log.append(f"  ERR {label}: {filename}  — {e}")

    # reply_html
    if result.get("reply_html"):
        html = result["reply_html"]
        full = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:Arial,sans-serif;max-width:800px;"
            "margin:20px auto;padding:20px;background:#f9f9f9;line-height:1.6}"
            "h1{color:#555;border-bottom:1px solid #ddd;padding-bottom:8px}</style>"
            f"</head><body><h1>Responder: {name}</h1>{html}</body></html>"
        )
        (resp_dir / "reply.html").write_text(full, encoding="utf-8")
        log.append(f"  OK  reply_html: reply.html  ({len(html)} znakow)")

    # Obrazki: image, image2, emoticon
    for key in ("image", "image2", "emoticon"):
        item = result.get(key) or {}
        if item and item.get("base64"):
            fname = item.get("filename") or f"{key}.png"
            _save_b64(item["base64"], fname, key)

    # Lista wykresow (emocje: images=[...])
    for i, item in enumerate(result.get("images") or []):
        if item and item.get("base64"):
            fname = item.get("filename") or f"wykres_{i}.png"
            _save_b64(item["base64"], fname, f"images[{i}]")

    # PDF
    pdf = result.get("pdf") or {}
    if pdf and pdf.get("base64"):
        fname = pdf.get("filename") or "wynik.pdf"
        _save_b64(pdf["base64"], fname, "pdf")

    # Dokumenty DOCX/TXT (analiza: docx_list=[], emocje: docs=[])
    all_docs = list(result.get("docx_list") or []) + list(result.get("docs") or [])
    for i, doc in enumerate(all_docs):
        if doc and doc.get("base64"):
            fname = doc.get("filename") or f"dokument_{i}.bin"
            _save_b64(doc["base64"], fname, f"doc[{i}]")

    # Dodatkowe TXT (obrazek: prompt1_txt, prompt2_txt; smierc: debug_txt)
    for key in ("prompt1_txt", "prompt2_txt", "debug_txt"):
        item = result.get(key) or {}
        if item and item.get("base64"):
            fname = item.get("filename") or f"{key}.txt"
            _save_b64(item["base64"], fname, key)

    # GIF
    gif = result.get("gif") or {}
    if gif and gif.get("base64"):
        fname = gif.get("filename") or "animacja.gif"
        _save_b64(gif["base64"], fname, "gif")

    # Metadane (klucze skalarne — przydatne diagnostycznie)
    SKIP = {"reply_html", "image", "image2", "emoticon", "images", "pdf",
            "docx_list", "docs", "prompt1_txt", "prompt2_txt", "debug_txt",
            "gif", "status"}
    scalar_items = [(k, v) for k, v in result.items()
                    if k not in SKIP and not isinstance(v, (bytes, dict, list))]
    if scalar_items:
        log += ["", "METADANE:"]
        for k, v in scalar_items:
            log.append(f"  {k} = {str(v)[:150]}")

    (resp_dir / "raport.txt").write_text("\n".join(log), encoding="utf-8")

    # ── api_log.txt — pełne payloady API ──────────────────────────────────────
    try:
        api_log_txt = build_api_log_txt(name)
        (resp_dir / "api_log.txt").write_text(api_log_txt, encoding="utf-8")
    except Exception as e:
        (resp_dir / "api_log_error.txt").write_text(str(e), encoding="utf-8")


# ── Funkcje testujace kazdy responder ─────────────────────────────────────────

def _test_smierc(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "smierc"
    if not _SMIERC_OK:
        raise RuntimeError("smierc.py nie zaladowany — sprawdz blad wyzej")
    etap_test = 9   # etap 9 = remont nieba (FLUX generowany automatycznie)
    result = _smierc.build_smierc_section(
        sender_email="test@test.pl",
        body=body,
        etap=etap_test,
        data_smierci_str=TODAY,
        historia=[],
    )
    img_ok = "TAK" if (result.get("image") or {}).get("base64") else "NIE"
    result["status"] = f"etap_in={etap_test} etap_out={result.get('nowy_etap','?')} image={img_ok}"
    return result


def _test_zwykly(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "zwykly"
    mod    = _load_responder("zwykly")
    result = mod.build_zwykly_section(body)
    result["status"] = f"emocja={result.get('detected_emotion','?')}"
    return result


def _test_obrazek(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "obrazek"
    mod    = _load_responder("obrazek")
    result = mod.build_obrazek_section(body)
    img1   = "TAK" if (result.get("image")  or {}).get("base64") else "NIE"
    img2   = "TAK" if (result.get("image2") or {}).get("base64") else "NIE"
    result["status"] = f"img1={img1} img2={img2}"
    return result


def _test_analiza(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "analiza"
    mod    = _load_responder("analiza")
    result = mod.build_analiza_section(body)
    result["status"] = f"docx={len(result.get('docx_list', []))}"
    return result


def _test_emocje(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "emocje"
    mod     = _load_responder("emocje")
    result  = mod.build_emocje_section(body)
    wykresy = len(result.get("images", []))
    raporty = len(result.get("docs",   []))
    result["status"] = f"wykresy={wykresy} raporty={raporty}"
    return result


def _test_biznes(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "biznes"
    mod    = _load_responder("biznes")
    result = mod.build_biznes_section(body)
    result["status"] = f"temat={result.get('topic','?')}"
    return result


def _test_scrabble(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "scrabble"
    mod    = _load_responder("scrabble")
    result = mod.build_scrabble_section(body)
    img    = "TAK" if (result.get("image") or {}).get("base64") else "NIE"
    result["status"] = f"img={img}"
    return result


def _test_nawiazanie(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "nawiazanie"
    mod    = _load_responder("nawiazanie")
    result = mod.build_nawiazanie_section(
        body=body,
        previous_body=(
            "Dzien dobry, wcześniej pisałem w sprawie mojego kota Mruka. "
            "Martwiłem sie o jego zdrowie i nie wiedziałem co robic."
        ),
        previous_subject="Poprzednie zapytanie testowe",
        sender="test@test.pl",
        sender_name="Testowy Nadawca",
    )
    hist = "TAK" if result.get("has_history") else "NIE"
    result["status"] = f"historia={hist}"
    return result


def _test_generator_pdf(body: str, resp_dir: Path) -> dict:
    global _current_responder
    _current_responder = "generator_pdf"
    mod = _load_responder("generator_pdf")
    fn  = getattr(mod, "build_generator_pdf_section", None)
    if fn is None:
        candidates = [v for k, v in vars(mod).items()
                      if k.startswith("build_") and callable(v)]
        fn = candidates[0] if candidates else None
    if fn is None:
        raise RuntimeError("Brak funkcji build_* w generator_pdf.py")
    result = fn(body, sender_name="Testowy Nadawca", n=5, diff="sredni")
    pdf_ok = "TAK" if (result.get("pdf") or {}).get("base64") else "NIE"
    result["status"] = f"pdf={pdf_ok}"
    return result


# ── GUI ───────────────────────────────────────────────────────────────────────
class RequiemApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("✦ REQUIEM KALIBRACJA v7 ✦")
        self.root.configure(bg=BG)
        self.root.geometry("900x1100")
        self.root.minsize(700, 600)

        self.etap_var = tk.IntVar(value=1)
        self.etapy    = load_etapy()
        self.max_etap = max(self.etapy.keys()) if self.etapy else 7
        self._s       = self._empty_state()
        self._build_ui()

    def _empty_state(self) -> dict:
        return {
            "body":           "",
            "pawel_1_6":      "", "pawel_1_6_prov":  "", "pawel_1_6_czas":  "",
            "pawel_7":        "", "pawel_7_prov":    "", "pawel_7_czas":    "",
            "pawel_8_19":     "", "pawel_8_19_prov": "", "pawel_8_19_czas": "",
            "pawel_20_50":    "", "pawel_20_50_prov":"", "pawel_20_50_czas":"",
            "wyslannik":      "", "wyslannik_prov":  "", "wyslannik_czas":  "",
            "email_prov":     "",
            "flux":           "", "flux_prov":       "", "flux_czas":       "",
            "img_bytes":      None, "img_czas": "", "img_err": "",
            "log":            [],
        }

    def _log(self, msg: str):
        self._s["log"].append(f"[{ts_now()}] {msg}")

    # ── Buduj UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar",
                        background=BG3, troughcolor=BG, arrowcolor=ACCENT,
                        bordercolor=BORDER, lightcolor=BG3, darkcolor=BG3)

        outer  = tk.Frame(self.root, bg=BG)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                               style="Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.mf = tk.Frame(canvas, bg=BG)
        cw = canvas.create_window((0, 0), window=self.mf, anchor="nw")
        self.mf.bind("<Configure>",
                     lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(cw, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        f = self.mf

        # ── Naglowek ─────────────────────────────────────────────────────────
        tk.Label(f, text="✦  REQUIEM  KALIBRACJA  v7  ✦",
                 font=("Georgia", 16, "bold"), fg=ACCENT, bg=BG).pack(pady=(18, 2))
        tk.Label(f,
                 text="DeepSeek → email  |  Groq → prompt FLUX  |  fallback wzajemny",
                 font=("Georgia", 9, "italic"), fg=FG2, bg=BG).pack(pady=(0, 2))
        smierc_color = SUCCESS if _SMIERC_OK else ERR
        tk.Label(f, text=f"  [smierc.py]  {_smierc_info()}",
                 font=FONT_FILE, fg=smierc_color, bg=BG).pack(pady=(0, 4))
        self.api_status = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.api_status.pack(pady=(0, 6))
        self._check_api_keys()
        self._sep(f)

        # ── Ustawienia ────────────────────────────────────────────────────────
        cfg = tk.Frame(f, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        cfg.pack(fill=tk.X, padx=20, pady=5)
        tk.Label(cfg, text="ETAP PAWŁA", font=FONT_FILE,
                 fg=FG3, bg=BG2).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 10))
        tk.Spinbox(cfg, from_=1, to=self.max_etap, textvariable=self.etap_var,
                   width=4, font=FONT_MONO, bg=BG3, fg=ACCENT,
                   buttonbackground=BTN_BG, insertbackground=ACCENT,
                   highlightthickness=0, bd=0
                   ).grid(row=0, column=1, padx=10, pady=(10, 10), sticky="w")
        etap_range_txt = f"(1–{self.max_etap}  |  51+ = Wysłannik)"
        tk.Label(cfg, text=etap_range_txt, font=FONT_FILE,
                 fg=FG3, bg=BG2).grid(row=0, column=2, padx=4, pady=(10, 10), sticky="w")
        tk.Label(cfg, text=f"data smierci: {TODAY}", font=FONT_FILE,
                 fg=FG3, bg=BG2).grid(row=0, column=3, padx=10, pady=(10, 10), sticky="w")
        self._sep(f)

        # ── 1 Wiadomosc ───────────────────────────────────────────────────────
        self._title(f, "1  WIADOMOSC NADAWCY")
        self.body_text = self._textbox(f, h=6, ro=False)
        _default_body = "Wpisz tutaj przykladowa wiadomosc od nadawcy..."
        _test_file = PROMPTS_DIR / "test.txt"
        if _test_file.exists():
            try:
                _default_body = _test_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        self.body_text.insert("1.0", _default_body)
        self.body_text.bind("<FocusIn>", self._clear_ph)
        self._sep(f)

        # ── 2 Pawel 1-6 ───────────────────────────────────────────────────────
        self._title(f, "2  ETAP 1–6 — Paweł z zaświatów (pierwsze etapy)")
        self._badge(f, FILE_PAWEL_1_6.name)
        self.res_pawel = self._textbox(f, h=5)
        self.pawel_meta = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.pawel_meta.pack(anchor="w", padx=20)
        self._btn(f, f"Generuj z pliku: {FILE_PAWEL_1_6.name}", self._gen_pawel_1_6)
        self._sep(f)

        # ── 3 Pawel 7 — teraz to etap reinkarnacji / ostatni etap ─────────────
        self._title(f, "3  ETAP 7 — Reinkarnacja / pożegnanie")
        self._badge(f, FILE_PAWEL_7.name)
        self.res_pawel7 = self._textbox(f, h=5)
        self.pawel7_meta = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.pawel7_meta.pack(anchor="w", padx=20)
        self._btn(f, f"Generuj z pliku: {FILE_PAWEL_7.name}", self._gen_pawel_7)
        self._sep(f)

        # ── 3b Pawel 8-19 ─────────────────────────────────────────────────────
        self._title(f, "3b  ETAP 8–19 — Paweł remont nieba (FLUX generowany)")
        self._badge(f, FILE_PAWEL_8_19.name)
        tk.Label(f, text="  Etap z pozagrobowe.txt — wytyczna głównego tematu FLUX",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        self.res_pawel_8_19 = self._textbox(f, h=5)
        self.pawel_8_19_meta = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.pawel_8_19_meta.pack(anchor="w", padx=20)
        self._btn(f, f"Generuj z pliku: {FILE_PAWEL_8_19.name}", self._gen_pawel_8_19)
        self._sep(f)

        # ── 3c Pawel 20-50 ────────────────────────────────────────────────────
        self._title(f, "3c  ETAP 20–50 — Paweł remont kosmiczny (FLUX generowany)")
        self._badge(f, FILE_PAWEL_20_50.name)
        tk.Label(f, text="  Etap z pozagrobowe.txt — wytyczna głównego tematu FLUX",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        self.res_pawel_20_50 = self._textbox(f, h=5)
        self.pawel_20_50_meta = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.pawel_20_50_meta.pack(anchor="w", padx=20)
        self._btn(f, f"Generuj z pliku: {FILE_PAWEL_20_50.name}", self._gen_pawel_20_50)
        self._sep(f)

        # ── 4 Wyslannik (etap 51+) ────────────────────────────────────────────
        self._title(f, "4  ETAP 51+ — Wysłannik z wyższych sfer")
        self._badge(f, FILE_WYSLANNIK.name)
        tk.Label(f, text="  Provider: DeepSeek -> email  (fallback: Groq)",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        self.res_wyslannik = self._textbox(f, h=6)
        self.wyslannik_meta = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.wyslannik_meta.pack(anchor="w", padx=20, pady=(0, 4))
        self._btn(f, f"Generuj z pliku: {FILE_WYSLANNIK.name}", self._gen_wyslannik)
        self._sep(f)

        # ── 5 Prompt FLUX ─────────────────────────────────────────────────────
        _flux_model = (getattr(_smierc, "HF_API_URL", "").split("/")[-1]
                       if _SMIERC_OK else "FLUX.1-schnell")
        self._title(f, f"5  PROPONOWANY TEKST DO {_flux_model.upper()}")
        self._badge(f, FILE_FLUX_GROQ_SYS.name)
        tk.Label(f, text="  Provider: Groq -> prompt FLUX  (fallback: DeepSeek)",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        self.flux_text = self._textbox(f, h=4)
        self.flux_status = tk.Label(f, text="", font=FONT_LBL, fg=FG2, bg=BG)
        self.flux_status.pack(anchor="w", padx=20, pady=2)
        self._btn(f, "Generuj tekst proponowany do FLUX", self._gen_flux_tekst)
        self._sep(f)

        # ── 6 Obrazek FLUX ────────────────────────────────────────────────────
        self._title(f, "6  GENERUJ OBRAZEK  ->  AUTO-ZAPIS")
        tk.Label(f,
                 text="  Po wygenerowaniu — auto-zapis do backup/Wyniki_HH_MM_SS/smierc/",
                 font=FONT_FILE, fg=FG2, bg=BG).pack(anchor="w", padx=20, pady=(0, 4))
        self.img_status = tk.Label(f,
                                   text="Najpierw wygeneruj tekst proponowany (krok 5)",
                                   font=FONT_LBL, fg=FG3, bg=BG)
        self.img_status.pack(anchor="w", padx=20, pady=4)
        _flux_btn_label = f"GENERUJ OBRAZEK  ({_flux_model})  ->  auto-zapis"
        self.btn_obrazek = self._btn(f, _flux_btn_label,
                                     self._gen_obrazek, green=True, state=tk.DISABLED)
        self._sep(f)

        # ── 7 TESTUJ WSZYSTKIE RESPONDERY ────────────────────────────────────
        self._title(f, "7  TESTUJ WSZYSTKIE RESPONDERY  ->  auto-zapis do backup/")
        tk.Label(f,
                 text=(
                     "  Odpala wszystkie respondery lokalnie tak jak na Render:\n"
                     "  smierc  zwykly  obrazek  analiza  emocje\n"
                     "  biznes  scrabble  nawiazanie  generator_pdf\n\n"
                     "  Klucze API z lokalnych zmiennych srodowiskowych PC.\n"
                     "  Wyniki: backup/Wyniki_HH_MM_SS/<nazwa_respondera>/"
                 ),
                 font=FONT_FILE, fg=FG2, bg=BG, justify=tk.LEFT
                 ).pack(anchor="w", padx=20, pady=(0, 6))

        self.all_status = tk.Label(f, text="Czeka na uruchomienie...",
                                   font=FONT_LBL, fg=FG3, bg=BG)
        self.all_status.pack(anchor="w", padx=20, pady=(0, 4))

        # Pasek postepu
        prog_frame = tk.Frame(f, bg=BG)
        prog_frame.pack(fill=tk.X, padx=20, pady=(0, 6))
        self.all_progress = ttk.Progressbar(
            prog_frame, orient="horizontal", mode="determinate")
        self.all_progress.pack(fill=tk.X)

        # Log
        log_border = tk.Frame(f, highlightbackground=BORDER,
                              highlightthickness=1, bg=BORDER)
        log_border.pack(fill=tk.X, padx=20, pady=(0, 4))
        self.all_log = tk.Text(
            log_border, height=12, wrap=tk.WORD,
            font=("Consolas", 8), bg=BG3, fg=FG2,
            state=tk.DISABLED, relief=tk.FLAT, bd=0, padx=8, pady=6)
        vsb2 = ttk.Scrollbar(log_border, orient="vertical",
                              command=self.all_log.yview,
                              style="Vertical.TScrollbar")
        self.all_log.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side=tk.RIGHT, fill=tk.Y)
        self.all_log.pack(fill=tk.X)

        self._btn(f, "TESTUJ WSZYSTKIE RESPONDERY  ->  auto-zapis wynikow",
                  self._test_all_responders, red=True)
        self._sep(f)

        # ── 8 Reczny zapis ────────────────────────────────────────────────────
        self._title(f, "8  ZAPISZ RECZNIE W DOWOLNYM MOMENCIE")
        tk.Label(f,
                 text=(
                     "  Zapisuje wyniki kalibracji smierc (etapy 2-6) do backup/Wyniki.../smierc/\n"
                     "  Zawartosci: niebo_wyslannik.png  _.txt  tekst_zrodlowy.txt  raport.txt"
                 ),
                 font=FONT_FILE, fg=FG2, bg=BG, justify=tk.LEFT
                 ).pack(anchor="w", padx=20, pady=(0, 4))
        self.backup_status = tk.Label(f, text="", font=FONT_LBL, fg=FG2, bg=BG)
        self.backup_status.pack(anchor="w", padx=20, pady=2)
        self._btn(f, "ZAPISZ TERAZ  +  wyczysc ekran", self._save_manual)

        tk.Frame(f, bg=BG, height=40).pack()

    # ── Widget helpers ────────────────────────────────────────────────────────
    def _check_api_keys(self):
        ds = "OK DeepSeek" if os.environ.get("DEEPSEEK_API_KEY") else "BRAK DeepSeek"
        gr = "OK Groq"     if os.environ.get("GROQ_API_KEY")     else "BRAK Groq"
        hf = "OK HF_TOKEN" if os.environ.get("HF_TOKEN")         else "BRAK HF_TOKEN"
        ok = all(os.environ.get(k) for k in
                 ["DEEPSEEK_API_KEY", "GROQ_API_KEY", "HF_TOKEN"])
        self.api_status.configure(
            text=f"{ds}   {gr}   {hf}", fg=SUCCESS if ok else ERR)

    def _sep(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill=tk.X, padx=20, pady=8)

    def _title(self, p, text):
        tk.Label(p, text=text, font=FONT_TITLE,
                 fg=ACCENT, bg=BG).pack(anchor="w", padx=20, pady=(4, 2))

    def _badge(self, p, name):
        tk.Label(p, text=f"  {name}", font=FONT_FILE,
                 fg=FG3, bg=BG).pack(anchor="w", padx=20, pady=(0, 2))

    def _textbox(self, parent, h=4, ro=True) -> tk.Text:
        frame = tk.Frame(parent, highlightbackground=BORDER,
                         highlightthickness=1, bg=BORDER)
        frame.pack(fill=tk.X, padx=20, pady=4)
        txt = tk.Text(frame, height=h, wrap=tk.WORD, font=FONT_MONO,
                      bg=BG3, fg=FG, insertbackground=ACCENT,
                      selectbackground=ACCENT2, selectforeground=FG,
                      relief=tk.FLAT, bd=0, padx=10, pady=8,
                      state=tk.DISABLED if ro else tk.NORMAL)
        sb  = ttk.Scrollbar(frame, orient="vertical", command=txt.yview,
                            style="Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _resize(e=None):
            lines = int(txt.index("end-1c").split(".")[0])
            txt.configure(height=max(h, min(lines + 1, 32)))

        txt.bind("<Configure>", _resize)
        return txt

    def _btn(self, parent, label, cmd, green=False, red=False, state=tk.NORMAL):
        if green:
            bg, fg, hv = GREEN,   "#ffffff", GREEN_H
        elif red:
            bg, fg, hv = RED_BTN, "#ffffff", RED_H
        else:
            bg, fg, hv = BTN_BG,  ACCENT,   ACCENT2
        b = tk.Button(parent, text=label, command=cmd,
                      font=FONT_BTN, fg=fg, bg=bg,
                      activebackground=hv,
                      activeforeground="#ffffff" if (green or red) else BG,
                      relief=tk.FLAT, bd=0, pady=9, padx=16,
                      cursor="hand2", state=state)
        b.pack(fill=tk.X, padx=20, pady=(4, 8))
        b.bind("<Enter>", lambda e: b.configure(bg=hv))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def _clear_ph(self, e):
        placeholder = "Wpisz tutaj przykladowa wiadomosc od nadawcy..."
        if self.body_text.get("1.0", tk.END).strip() == placeholder:
            self.body_text.delete("1.0", tk.END)

    def _set(self, w: tk.Text, text: str):
        w.configure(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        w.insert("1.0", text)
        lines = text.count("\n") + 1
        w.configure(height=max(4, min(lines + 2, 32)), state=tk.DISABLED)

    def _loading(self, w: tk.Text, label: str = ""):
        self._set(w, f"generuje... {label}")

    def _get_body(self) -> str:
        b = self.body_text.get("1.0", tk.END).strip()
        placeholder = "Wpisz tutaj przykladowa wiadomosc od nadawcy..."
        return "" if b == placeholder else b

    def _get_wyslannik(self) -> str:
        return self.res_wyslannik.get("1.0", tk.END).strip()

    # ── Log sekcji testowania ─────────────────────────────────────────────────
    def _alog(self, msg: str, color: str = None):
        entry = f"[{ts_now()}] {msg}"
        self.all_log.configure(state=tk.NORMAL)
        if color:
            tag = f"c_{color.replace('#', '')}"
            self.all_log.tag_configure(tag, foreground=color)
            self.all_log.insert(tk.END, entry + "\n", tag)
        else:
            self.all_log.insert(tk.END, entry + "\n")
        self.all_log.see(tk.END)
        self.all_log.configure(state=tk.DISABLED)
        self.root.update_idletasks()

    # ── Generatory kalibracji smierc ──────────────────────────────────────────
    def _gen_pawel_1_6(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci", "Wpisz wiadomosc nadawcy.")
            return
        self._s["body"] = body
        self._loading(self.res_pawel)
        self.pawel_meta.configure(text="", fg=FG3)
        etap_tresc = self.etapy.get(self.etap_var.get(), "Podróz trwa")
        t0 = datetime.datetime.now()
        self._log(f"START Pawel 1-6 | etap: {etap_tresc}")

        def _run():
            tmpl = load_txt(FILE_PAWEL_1_6,
                "Jestes Pawlem — zmarlym mezczyznà piszacym z zaswiatow. "
                "Piszesz po polsku. Odpowiedz max 5 zdan. "
                "Podpisz sie: — Autoresponder Pawla-zza-swiatow. "
                "Wspomnij ze umarlés na suchoty dnia {data_smierci_str}.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            result, prov = call_llm_email(
                system, f"Etap w zaswiatach: {etap_tresc}\nWiadomosc: {body}")
            czas = elapsed(t0)
            self._s.update({"pawel_1_6": result or "", "pawel_1_6_prov": prov,
                             "pawel_1_6_czas": czas})
            self._log(f"KONIEC Pawel 1-6 | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.res_pawel, result or "[Blad API]"),
                self.pawel_meta.configure(text=f"  {prov}  |  {czas}", fg=FG3)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_pawel_7(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci", "Wpisz wiadomosc nadawcy.")
            return
        self._s["body"] = body
        self._loading(self.res_pawel7)
        self.pawel7_meta.configure(text="", fg=FG3)
        etap_tresc = self.etapy.get(self.max_etap, "Reinkarnacja nadchodzi nieuchronnie")
        t0 = datetime.datetime.now()
        self._log(f"START Pawel 7 | etap: {etap_tresc}")

        def _run():
            tmpl = load_txt(FILE_PAWEL_7,
                "Jestes Pawlem — zmarlym mezczyznà piszacym z zaswiatow. "
                "Ton: spokojny, wzruszajacy. Odpowiedz max 5 zdan. "
                "Poinformuj ze nadchodzi reinkarnacja. Pozegnaj sie cieplo.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            result, prov = call_llm_email(
                system, f"Etap: {etap_tresc}\nWiadomosc: {body}")
            czas = elapsed(t0)
            self._s.update({"pawel_7": result or "", "pawel_7_prov": prov,
                             "pawel_7_czas": czas})
            self._log(f"KONIEC Pawel 7 | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.res_pawel7, result or "[Blad API]"),
                self.pawel7_meta.configure(text=f"  {prov}  |  {czas}", fg=FG3)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_pawel_8_19(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci", "Wpisz wiadomosc nadawcy.")
            return
        self._s["body"] = body
        self._loading(self.res_pawel_8_19)
        self.pawel_8_19_meta.configure(text="", fg=FG3)
        etap_nr    = max(8, min(19, self.etap_var.get()))
        etap_tresc = self.etapy.get(etap_nr, "Remonty - remont nieba")
        t0 = datetime.datetime.now()
        self._log(f"START Pawel 8-19 | etap {etap_nr}: {etap_tresc}")

        def _run():
            global _current_responder
            _current_responder = "smierc_8_19"
            tmpl = load_txt(FILE_PAWEL_8_19,
                "Jestes Pawlem — zmarlym mezczyznà piszacym z zaswiatow. "
                "Ton: absurdalny, z humorem robotniczym. Odpowiedz max 5 zdan. "
                "Wspomnij ze umarlés na suchoty dnia {data_smierci_str}. "
                "Nawiaz do wiadomosci paradoksalnie chwalac Ziemie.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            user   = f"Etap w zaswiatach: {etap_tresc}\nWiadomosc: {body}"
            result, prov = call_llm_email(system, user)
            czas = elapsed(t0)
            self._s.update({"pawel_8_19": result or "", "pawel_8_19_prov": prov,
                             "pawel_8_19_czas": czas})
            self._log(f"KONIEC Pawel 8-19 | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.res_pawel_8_19, result or "[Blad API]"),
                self.pawel_8_19_meta.configure(text=f"  etap {etap_nr}: {etap_tresc[:40]}  |  {prov}  |  {czas}", fg=FG3)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_pawel_20_50(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci", "Wpisz wiadomosc nadawcy.")
            return
        self._s["body"] = body
        self._loading(self.res_pawel_20_50)
        self.pawel_20_50_meta.configure(text="", fg=FG3)
        etap_nr    = max(20, min(self.max_etap, self.etap_var.get()))
        etap_tresc = self.etapy.get(etap_nr, "Remonty - remont kosmiczny")
        t0 = datetime.datetime.now()
        self._log(f"START Pawel 20-50 | etap {etap_nr}: {etap_tresc}")

        def _run():
            global _current_responder
            _current_responder = "smierc_20_50"
            tmpl = load_txt(FILE_PAWEL_20_50,
                "Jestes Pawlem — zmarlym mezczyznà piszacym z zaswiatow. "
                "Ton: absurdalny, z humorem robotniczym. Odpowiedz max 5 zdan. "
                "Wspomnij ze umarlés na suchoty dnia {data_smierci_str}. "
                "Nawiaz do wiadomosci paradoksalnie chwalac Ziemie.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            user   = f"Etap w zaswiatach: {etap_tresc}\nWiadomosc: {body}"
            result, prov = call_llm_email(system, user)
            czas = elapsed(t0)
            self._s.update({"pawel_20_50": result or "", "pawel_20_50_prov": prov,
                             "pawel_20_50_czas": czas})
            self._log(f"KONIEC Pawel 20-50 | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.res_pawel_20_50, result or "[Blad API]"),
                self.pawel_20_50_meta.configure(text=f"  etap {etap_nr}: {etap_tresc[:40]}  |  {prov}  |  {czas}", fg=FG3)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_wyslannik(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci", "Wpisz wiadomosc nadawcy.")
            return
        self._s["body"] = body
        self._loading(self.res_wyslannik)
        self.wyslannik_meta.configure(text="", fg=FG3)
        t0 = datetime.datetime.now()
        self._log("START Wyslannik")

        def _run():
            system = load_txt(FILE_WYSLANNIK,
                "Jestes wyslannikiem z wyzszych sfer duchowych. "
                "Ton: dostojny, poetycki. Max 4 zdania. "
                "Podpisz sie: — Wyslannik z wyzszych sfer")
            result, prov = call_llm_email(system, f"Osoba pyta: {body}")
            czas = elapsed(t0)
            self._s.update({"wyslannik": result or "", "wyslannik_prov": prov,
                             "wyslannik_czas": czas, "email_prov": prov})
            self._log(f"KONIEC Wyslannik | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.res_wyslannik, result or "[Blad API]"),
                self.wyslannik_meta.configure(text=f"  {prov}  |  {czas}", fg=FG3)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_flux_tekst(self):
        wyslannik = self._get_wyslannik()
        if not wyslannik or wyslannik.startswith("generu") or wyslannik == "[Blad API]":
            messagebox.showwarning("Brak tekstu Wyslannika",
                                   "Najpierw wygeneruj odpowiedz Wyslannika (krok 4).")
            return
        self.flux_status.configure(text="Groq generuje prompt FLUX...", fg=FG2)
        self._set(self.flux_text, "...")
        self.btn_obrazek.configure(state=tk.DISABLED)
        t0 = datetime.datetime.now()
        self._log("START generowanie promptu FLUX")

        def _run():
            system = load_txt(FILE_FLUX_GROQ_SYS,
                "You are a creative prompt engineer for FLUX image generator. "
                "Write a surreal image prompt in English (max 75 words). "
                "Return ONLY the prompt.")
            user   = f"Generate FLUX image prompt based on:\n\n{wyslannik}"
            result, prov = call_llm_flux(system, user)
            if not result:
                result = load_txt(FILE_IMAGE_STYLE,
                    "surreal heavenly paradise, divine golden light, "
                    "celestial beings, otherworldly atmosphere, vivid colors")
                prov = "statyczny fallback"
            czas = elapsed(t0)
            self._s.update({"flux": result, "flux_prov": prov, "flux_czas": czas})
            self._log(f"KONIEC prompt FLUX | provider: {prov} | czas: {czas}")
            self.root.after(0, lambda: (
                self._set(self.flux_text, result),
                self.flux_status.configure(
                    text=f"Gotowy — {prov}  |  {czas}", fg=SUCCESS),
                self.btn_obrazek.configure(state=tk.NORMAL)
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _gen_obrazek(self):
        if not self._s.get("flux"):
            messagebox.showwarning("Brak promptu",
                                   "Najpierw wygeneruj tekst proponowany (krok 5).")
            return
        _flux_model_name = (getattr(_smierc, "HF_API_URL", "").split("/")[-1]
                            if _SMIERC_OK else "FLUX")
        _flux_steps = getattr(_smierc, "HF_STEPS", 5) if _SMIERC_OK else 5
        self.img_status.configure(
            text=f"Generuje {_flux_model_name} (steps={_flux_steps})...", fg=FG2)
        self.btn_obrazek.configure(state=tk.DISABLED)
        t0 = datetime.datetime.now()
        self._log("START generowanie obrazka FLUX")

        def _run():
            img_bytes, err = generate_flux_image(self._s["flux"])
            czas = elapsed(t0)
            self._s["img_bytes"] = img_bytes
            self._s["img_czas"]  = czas
            self._s["img_err"]   = err or ""

            if img_bytes:
                self._log(f"KONIEC obrazek OK | {len(img_bytes):,} B | {czas}")
                try:
                    run_dir = save_smierc(self._s)
                    self._log(f"AUTO-ZAPIS -> {run_dir.name}/smierc/")
                    self.root.after(0, lambda: self.img_status.configure(
                        text=(f"OK Obrazek ({len(img_bytes):,} B)  |  {czas}"
                              f"  ->  {run_dir.name}/smierc/"),
                        fg=SUCCESS))
                except Exception as e:
                    self._log(f"BLAD auto-zapisu: {e}")
                    self.root.after(0, lambda: self.img_status.configure(
                        text=f"OK Obrazek | BLAD zapisu: {e}", fg=ERR))
            else:
                self._log(f"BLAD obrazek: {err} | {czas}")
                self.root.after(0, lambda: (
                    self.img_status.configure(text=f"BLAD FLUX: {err}", fg=ERR),
                    self.btn_obrazek.configure(state=tk.NORMAL)
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _save_manual(self):
        body = self._get_body()
        if body:
            self._s["body"] = body
        if not any([self._s.get("pawel_1_6"), self._s.get("wyslannik"),
                    self._s.get("flux"), self._s.get("img_bytes")]):
            messagebox.showwarning("Brak danych",
                                   "Wygeneruj najpierw jakis wynik.")
            return
        self._log("RECZNY ZAPIS")
        try:
            run_dir = save_smierc(self._s)
            self.backup_status.configure(
                text=f"Zapisano: {run_dir.name}/smierc/", fg=SUCCESS)
            messagebox.showinfo("Zapisano",
                f"Katalog:\n{run_dir / 'smierc'}\n\n"
                f"  niebo_wyslannik.png\n  _.txt\n"
                f"  tekst_zrodlowy.txt\n  raport.txt\n"
                f"  prompts/")
            self._clear_all()
        except Exception as e:
            messagebox.showerror("Blad zapisu", str(e))

    def _clear_all(self):
        for w in [self.res_pawel, self.res_pawel7, self.res_pawel_8_19,
                  self.res_pawel_20_50, self.res_wyslannik, self.flux_text]:
            self._set(w, "")
        for lbl in [self.pawel_meta, self.pawel7_meta, self.pawel_8_19_meta,
                    self.pawel_20_50_meta, self.wyslannik_meta]:
            lbl.configure(text="", fg=FG3)
        self.flux_status.configure(text="", fg=FG2)
        self.img_status.configure(
            text="Najpierw wygeneruj tekst proponowany (krok 5)", fg=FG3)
        self.backup_status.configure(text="", fg=FG2)
        self.btn_obrazek.configure(state=tk.DISABLED)
        self._s = self._empty_state()

    # ── Testuj wszystkie Respondery ───────────────────────────────────────────
    def _test_all_responders(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomosci",
                                   "Wpisz wiadomosc w polu 1 lub wczytaj test.txt.")
            return

        # Reset UI
        self.all_log.configure(state=tk.NORMAL)
        self.all_log.delete("1.0", tk.END)
        self.all_log.configure(state=tk.DISABLED)
        self.all_status.configure(text="Trwa testowanie...", fg=FG2)
        self.all_progress["value"] = 0

        RESPONDERS = [
            ("smierc",        _test_smierc),
            ("zwykly",        _test_zwykly),
            ("obrazek",       _test_obrazek),
            ("analiza",       _test_analiza),
            ("emocje",        _test_emocje),
            ("biznes",        _test_biznes),
            ("scrabble",      _test_scrabble),
            ("nawiazanie",    _test_nawiazanie),
            ("generator_pdf", _test_generator_pdf),
        ]
        total = len(RESPONDERS)

        def _run():
            import traceback
            ts      = datetime.datetime.now().strftime("%H_%M_%S")
            run_dir = BACKUP_DIR / f"Wyniki_{ts}"
            run_dir.mkdir(parents=True, exist_ok=True)

            self.root.after(0, lambda: self._alog(
                f"=== START TESTU WSZYSTKICH RESPONDEROW — {ts} ===", ACCENT))
            self.root.after(0, lambda: self._alog(
                f"Katalog: {run_dir}", FG2))
            self.root.after(0, lambda: self._alog(
                f"Tekst: {body[:100]}...", FG2))
            self.root.after(0, lambda: self._alog(""))

            summary = [
                "TEST WSZYSTKICH RESPONDEROW",
                f"Data: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Katalog: {run_dir}",
                f"Tekst ({len(body)} zn): {body[:300]}",
                "=" * 60,
            ]
            ok_n  = 0
            err_n = 0

            for idx, (name, test_fn) in enumerate(RESPONDERS):
                resp_dir = run_dir / name
                resp_dir.mkdir(exist_ok=True)

                self.root.after(0, lambda n=name:
                    self._alog(f"  [{n}] uruchamiam...", FG2))

                t0 = datetime.datetime.now()
                try:
                    _api_call_log.clear()   # reset logu przed każdym responderem
                    result   = test_fn(body, resp_dir)
                    czas     = (datetime.datetime.now() - t0).total_seconds()
                    status   = result.pop("status", "OK")

                    _save_responder_result(resp_dir, name, result, body, czas)

                    self.root.after(0, lambda n=name, s=status, c=czas:
                        self._alog(f"  OK  [{n}]  {s}  ({c:.1f}s)", SUCCESS))
                    summary.append(f"OK  {name:<16}  {status}  ({czas:.1f}s)")
                    ok_n += 1

                except Exception as e:
                    czas    = (datetime.datetime.now() - t0).total_seconds()
                    err_msg = str(e)
                    tb      = traceback.format_exc()
                    self.root.after(0, lambda n=name, err=err_msg[:100], c=czas:
                        self._alog(f"  ERR [{n}]  {err}  ({c:.1f}s)", ERR))
                    summary.append(
                        f"ERR {name:<16}  {err_msg[:60]}  ({czas:.1f}s)")
                    err_n += 1
                    (resp_dir / "error.txt").write_text(
                        f"BLAD: {e}\n\n{tb}", encoding="utf-8")

                pct = int((idx + 1) / total * 100)
                self.root.after(0, lambda p=pct:
                    self.all_progress.configure(value=p))

            summary += [
                "=" * 60,
                f"RAZEM: {ok_n} OK  |  {err_n} BLEDOW  z {total}",
            ]
            (run_dir / "podsumowanie.txt").write_text(
                "\n".join(summary), encoding="utf-8")

            final_col = SUCCESS if err_n == 0 else ERR
            final_msg = (f"OK wszystkie ({ok_n}/{total})" if err_n == 0
                         else f"UWAGA {ok_n} OK / {err_n} BLEDOW z {total}")

            self.root.after(0, lambda: (
                self._alog(""),
                self._alog(
                    f"=== KONIEC ===  {final_msg}  ->  {run_dir.name}", final_col),
                self.all_status.configure(
                    text=f"{final_msg}  ->  {run_dir.name}", fg=final_col),
                self.all_progress.configure(value=100),
            ))

        threading.Thread(target=_run, daemon=True).start()


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app  = RequiemApp(root)
    root.mainloop()
