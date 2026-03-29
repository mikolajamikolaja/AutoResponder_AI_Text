import os
import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from typing import List, Optional

logger = logging.getLogger(__name__)

# KONFIGURACJA
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465 # Zmieniono z 587 na 465 dla lepszej przepuszczalności
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Bot")

def wyslij_odpowiedz(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    zalaczniki: Optional[List[Optional[dict]]] = None,
    reply_to: Optional[str] = None,
) -> bool:
    if not SMTP_USER or not SMTP_APP_PASSWORD:
        logger.error("[smtp] Brak danych logowania SMTP.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"] = formataddr((to_name, to_email))
    msg["Subject"] = subject
    msg["Reply-To"] = reply_to or SMTP_USER

    msg.attach(MIMEText(html_body or "<p>Brak treści</p>", "html", "utf-8"))

    attached_count = 0
    for item in (zalaczniki or []):
        if not item or not item.get("base64"):
            continue
        try:
            raw = base64.b64decode(item["base64"])
            part = MIMEBase(*(item.get("content_type", "application/octet-stream").split("/", 1)))
            part.set_payload(raw)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=item.get("filename", "zalacznik"))
            msg.attach(part)
            attached_count += 1
        except Exception as e:
            logger.warning(f"[smtp] Błąd załącznika: {e}")

    # ── KLUCZOWA POPRAWKA WYSYŁKI ──
    try:
        # Używamy SMTP_SSL dla portu 465
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_bytes())
        
        logger.info(f"[smtp] ✅ Wysłano do {to_email} | Załączników: {attached_count}")
        return True

    except Exception as e:
        logger.error(f"[smtp] ❌ Błąd krytyczny wysyłki: {e}")
        return False

def zbierz_zalaczniki_z_response(response_data: dict) -> List[dict]:
    result = []
    def _dodaj(item):
        if isinstance(item, dict) and item.get("base64"):
            result.append(item)
        elif isinstance(item, list):
            for el in item: _dodaj(el)

    for res_val in response_data.values():
        if not isinstance(res_val, dict): continue
        pola = ["pdf", "emoticon", "cv_pdf", "raport_pdf", "psych_photo_1", "psych_photo_2"]
        for p in pola: _dodaj(res_val.get(p))
        for p_list in ["triptych", "images"]: _dodaj(res_val.get(p_list, []))
    return result