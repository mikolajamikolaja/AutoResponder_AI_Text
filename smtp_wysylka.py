"""
smtp_wysylka.py
Wysyłka przez Gmail API (HTTPS port 443) — działa na Render free tier.

Zamiast SMTP używamy oficjalnego Gmail REST API z OAuth2.
Dwie metody autoryzacji (wybierana automatycznie):
  A) Service Account (zalecane dla serwerów) — wymaga GMAIL_SERVICE_ACCOUNT_JSON
  B) OAuth2 Refresh Token (alternatywa) — wymaga GMAIL_CLIENT_ID,
     GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN

Zmienne środowiskowe (Render → Environment):
  SMTP_USER              — adres Gmail z którego wysyłamy (np. bot@gmail.com)
  SMTP_FROM_NAME         — nazwa nadawcy (np. "Bot Tylera")

  Metoda A (Service Account):
    GMAIL_SERVICE_ACCOUNT_JSON — cała zawartość pliku JSON service account
                                  (jako jedna linia, skopiuj z pliku .json)
    lub oddzielne zmienne środowiskowe Render:
      GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY
      GMAIL_SERVICE_ACCOUNT_CLIENT_EMAIL
      GMAIL_SERVICE_ACCOUNT_PROJECT_ID
      GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID
      GMAIL_SERVICE_ACCOUNT_CLIENT_ID
      GMAIL_SERVICE_ACCOUNT_AUTH_URI
      GMAIL_SERVICE_ACCOUNT_TOKEN_URI
      GMAIL_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL
      GMAIL_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL

  Metoda B (OAuth2 Refresh Token):
    GMAIL_CLIENT_ID        — OAuth2 Client ID z Google Cloud Console
    GMAIL_CLIENT_SECRET    — OAuth2 Client Secret
    GMAIL_REFRESH_TOKEN    — Refresh Token (wygenerowany raz przez OAuth flow)
"""

import os
import base64
import logging
import json
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── KONFIGURACJA ──────────────────────────────────────────────────────────────
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Bot")

# Metoda A — Service Account
_SA_JSON_STR = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "").strip()

_GMAIL_SERVICE_ACCOUNT_TYPE = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_TYPE", "service_account"
).strip()
_GMAIL_SERVICE_ACCOUNT_PROJECT_ID = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_PROJECT_ID", ""
).strip()
_GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID", ""
).strip()
_GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY = (
    os.getenv("GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY", "").replace("\\n", "\n").strip()
)
_GMAIL_SERVICE_ACCOUNT_CLIENT_EMAIL = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_CLIENT_EMAIL", ""
).strip()
_GMAIL_SERVICE_ACCOUNT_CLIENT_ID = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_CLIENT_ID", ""
).strip()
_GMAIL_SERVICE_ACCOUNT_AUTH_URI = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"
).strip()
_GMAIL_SERVICE_ACCOUNT_TOKEN_URI = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_TOKEN_URI", "https://oauth2.googleapis.com/token"
).strip()
_GMAIL_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL",
    "https://www.googleapis.com/oauth2/v1/certs",
).strip()
_GMAIL_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL = os.getenv(
    "GMAIL_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL", ""
).strip()

# Metoda B — OAuth2 Refresh Token
_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")

GMAIL_SEND_URL = (
    f"https://gmail.googleapis.com/gmail/v1/users/{SMTP_USER}/messages/send"
)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


# ── AUTORYZACJA ───────────────────────────────────────────────────────────────


def _load_gmail_service_account():
    if _SA_JSON_STR:
        try:
            return json.loads(_SA_JSON_STR)
        except json.JSONDecodeError:
            logger.error("[gmail] Nieprawidłowe GMAIL_SERVICE_ACCOUNT_JSON")
            return None

    if (
        not _GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY
        or not _GMAIL_SERVICE_ACCOUNT_CLIENT_EMAIL
    ):
        return None

    return {
        "type": _GMAIL_SERVICE_ACCOUNT_TYPE,
        "project_id": _GMAIL_SERVICE_ACCOUNT_PROJECT_ID,
        "private_key_id": _GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID,
        "private_key": _GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY,
        "client_email": _GMAIL_SERVICE_ACCOUNT_CLIENT_EMAIL,
        "client_id": _GMAIL_SERVICE_ACCOUNT_CLIENT_ID,
        "auth_uri": _GMAIL_SERVICE_ACCOUNT_AUTH_URI,
        "token_uri": _GMAIL_SERVICE_ACCOUNT_TOKEN_URI,
        "auth_provider_x509_cert_url": _GMAIL_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL,
        "client_x509_cert_url": _GMAIL_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL,
    }


def _get_access_token_service_account() -> Optional[str]:
    """Pobiera access token przez Service Account + JWT (metoda A)."""
    try:
        import time
        import jwt  # pip install PyJWT cryptography

        sa = _load_gmail_service_account()
        if not sa:
            logger.error("[gmail] Brak konfiguracji Service Account dla Gmail API")
            return None

        now = int(time.time())
        payload = {
            "iss": sa["client_email"],
            "sub": SMTP_USER,
            "scope": " ".join(GMAIL_SCOPES),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, sa["private_key"], algorithm="RS256")
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        logger.error("[gmail] Service Account token błąd: %s", e)
        return None


def _get_access_token_refresh() -> Optional[str]:
    """Pobiera access token przez Refresh Token (metoda B)."""
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "refresh_token": _REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        logger.error("[gmail] Refresh Token błąd: %s", e)
        return None


def _get_access_token() -> Optional[str]:
    """Automatycznie wybiera metodę autoryzacji."""
    if _SA_JSON_STR:
        logger.debug("[gmail] Używam metody A (Service Account)")
        return _get_access_token_service_account()
    if _CLIENT_ID and _CLIENT_SECRET and _REFRESH_TOKEN:
        logger.debug("[gmail] Używam metody B (Refresh Token)")
        return _get_access_token_refresh()
    logger.error("[gmail] Brak konfiguracji OAuth2 — ustaw zmienne środowiskowe.")
    return None


# ── WYSYŁKA ───────────────────────────────────────────────────────────────────


def wyslij_odpowiedz(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    zalaczniki: Optional[List[Optional[dict]]] = None,
    reply_to: Optional[str] = None,
) -> bool:
    if not SMTP_USER:
        logger.error("[gmail] Brak SMTP_USER.")
        return False

    # ── WYJĄTEK: Zablokuj wysyłkę do ADMIN_EMAIL (ochrona przed pętlą) ─────
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    if admin_email and to_email.strip().lower() == admin_email:
        logger.warning(
            "[gmail] 🔒 BLOKADA WYSYŁKI: Nie wysyłam do ADMIN_EMAIL (%s) — ochrona przed pętlą",
            to_email,
        )
        return False

    # ── Buduj wiadomość MIME ──────────────────────────────────────────────────
    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg["Reply-To"] = reply_to or SMTP_USER

    msg.attach(MIMEText(html_body or "<p>Brak treści</p>", "html", "utf-8"))

    attached_count = 0
    attachment_errors = []
    for item in zalaczniki or []:
        if not item or not item.get("base64"):
            if item and item.get("filename"):
                attachment_errors.append(f"{item.get('filename')} (brak base64)")
            continue
        try:
            raw = base64.b64decode(item["base64"])
            ctype = item.get("content_type", "application/octet-stream")
            if not ctype or "/" not in ctype:
                logger.warning(
                    "[gmail] Nieprawidłowy content_type '%s' dla %s — używam application/octet-stream",
                    ctype,
                    item.get("filename", "?"),
                )
                ctype = "application/octet-stream"
            main_type, sub_type = ctype.split("/", 1)
            part = MIMEBase(main_type, sub_type)
            part.set_payload(raw)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=item.get("filename", "zalacznik"),
            )
            msg.attach(part)
            attached_count += 1
            logger.debug(
                "[gmail] ✓ Załącznik: %s (%s)", item.get("filename", "?"), len(raw)
            )
        except Exception as e:
            filename = item.get("filename", "unknown")
            attachment_errors.append(f"{filename} ({str(e)[:50]})")
            logger.warning("[gmail] ✗ Błąd załącznika %s: %s", filename, e)

    if attachment_errors:
        logger.warning(
            "[gmail] Błędy przy załącznikach (%d): %s",
            len(attachment_errors),
            "; ".join(attachment_errors[:3]),
        )

    # ── Kodowanie do base64url (wymagane przez Gmail API) ─────────────────────
    raw_bytes = msg.as_bytes()
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    # ── Pobierz token i wyślij ────────────────────────────────────────────────
    access_token = _get_access_token()
    if not access_token:
        logger.error("[gmail] Nie udało się uzyskać access token.")
        return False

    try:
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw_b64},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info(
                "[gmail] ✅ Wysłano do %s | Załączników: %d", to_email, attached_count
            )
            return True
        else:
            logger.error("[gmail] ❌ HTTP %d: %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("[gmail] ❌ Błąd krytyczny: %s", e)
        return False


# ── ZBIERANIE ZAŁĄCZNIKÓW — PEŁNA WERSJA ─────────────────────────────────────


def zbierz_zalaczniki_z_response(response_data: dict) -> List[dict]:
    """
    Zbiera WSZYSTKIE pliki ze wszystkich sekcji response_data i zwraca
    płaską listę słowników {base64, content_type, filename}.

    Obsługiwane sekcje: zwykly, biznes, scrabble, dociekliwy, emocje,
                        obrazek, generator_pdf, smierc, nawiazanie.

    Top-level fields (BEZPOŚREDNIO w response_data):
      log_svg, log_txt

    Pola pojedyncze w sekcjach (obiekt z base64):
      pdf, emoticon, cv_pdf, log_psych, ankieta_html, ankieta_pdf,
      horoskop_pdf, karta_rpg_pdf, raport_pdf, debug_txt,
      explanation_txt, plakat_svg, gra_html, image, image2,
      prompt1_txt, prompt2_txt

    Pola listowe (lista obiektów z base64):
      triptych, images, videos, docs, docx_list
    """
    result: List[dict] = []
    seen_filenames: set = set()
    missing_attachments: List[dict] = []  # Śledź brakujące base64

    # TOP-LEVEL FIELDS — bezpośrednio w response_data (nie wewnątrz sekcji)
    TOP_LEVEL_FIELDS = ["log_svg", "log_txt"]

    # Pola pojedyncze (każde to dict z kluczem "base64")
    SINGLE_FIELDS = [
        "pdf",
        "emoticon",
        "cv_pdf",
        "log_psych",
        "psych_photo_1",
        "psych_photo_2",
        "ankieta_html",
        "ankieta_pdf",
        "horoskop_pdf",
        "karta_rpg_pdf",
        "raport_pdf",
        "debug_txt",
        "explanation_txt",
        "plakat_svg",
        "gra_html",
        "image",
        "image2",
        "prompt1_txt",
        "prompt2_txt",
    ]

    # Pola listowe (każde to lista dict-ów z kluczem "base64")
    LIST_FIELDS = [
        "triptych",
        "images",
        "videos",
        "docs",
        "docx_list",
    ]

    def _dodaj(item: object, field_name: str = "", section_name: str = "") -> None:
        """Dodaje jeden obiekt-załącznik do listy wynikowej (jeśli ma base64)."""
        if not isinstance(item, dict):
            return

        filename = item.get("filename") or "zalacznik"

        if not item.get("base64"):
            # Zarejestruj brakujący base64
            missing_attachments.append(
                {
                    "filename": filename,
                    "field": field_name,
                    "section": section_name,
                    "reason": "missing_base64",
                }
            )
            logger.debug(
                "[zbierz] BRAKUJE base64: %s (sekcja: %s, pole: %s)",
                filename,
                section_name,
                field_name,
            )
            return

        # Unikaj duplikatów (ten sam plik w wielu sekcjach)
        if filename in seen_filenames:
            logger.debug("[zbierz] Pomijam duplikat: %s", filename)
            return
        seen_filenames.add(filename)
        result.append(
            {
                "base64": item["base64"],
                "content_type": item.get("content_type", "application/octet-stream"),
                "filename": filename,
            }
        )
        logger.debug(
            "[zbierz] Dodano załącznik: %s (%s)",
            filename,
            item.get("content_type", "?"),
        )

    # ── Top-level fields (np. log_svg z app.py) ──────────────────────────────
    for field in TOP_LEVEL_FIELDS:
        if field in response_data:
            _dodaj(response_data[field], field, "top-level")

    # ── Sekcje respondery (zwykly, biznes, emocje, etc.) ────────────────────
    for section_key, section_val in response_data.items():
        # Przeskocz top-level fields
        if section_key in TOP_LEVEL_FIELDS:
            continue
        if not isinstance(section_val, dict):
            continue

        # Pola pojedyncze w sekcji
        for field in SINGLE_FIELDS:
            item = section_val.get(field)
            if item is not None:  # Jeśli pole istnieje, nawet jeśli None
                _dodaj(item, field, section_key)

        # Pola listowe w sekcji
        for field in LIST_FIELDS:
            arr = section_val.get(field)
            if isinstance(arr, list):
                for idx, element in enumerate(arr):
                    _dodaj(element, f"{field}[{idx}]", section_key)

    logger.info(
        "[zbierz] Łącznie załączników: %d (z %d sekcji), brakuje: %d",
        len(result),
        len(response_data),
        len(missing_attachments),
    )

    # Loguj brakujące załączniki po kilka
    if missing_attachments:
        logger.warning(
            "[zbierz] OSTRZEŻENIE: Brakuje base64 dla %d plików:",
            len(missing_attachments),
        )
        for att in missing_attachments[:10]:  # Pokaż pierwsze 10
            logger.warning(
                "  - %s (sekcja: %s, pole: %s)",
                att["filename"],
                att["section"],
                att["field"],
            )
        if len(missing_attachments) > 10:
            logger.warning("  ... i %d więcej", len(missing_attachments) - 10)

    return result
