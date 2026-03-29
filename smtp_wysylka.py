"""
smtp_wysylka.py
===============
Moduł wysyłki e-mail przez Gmail SMTP z App Password.

Konfiguracja (zmienne środowiskowe):
  SMTP_USER          — adres Gmail, z którego wysyłamy, np. moj.bot@gmail.com
  SMTP_APP_PASSWORD  — 16-znakowe App Password z panelu konta Google
  SMTP_FROM_NAME     — wyświetlana nazwa nadawcy, np. "Bot Tylera"

Użycie w app.py:
  from smtp_wysylka import wyslij_odpowiedz

  wyslij_odpowiedz(
      to_email   = "odbiorca@example.com",
      to_name    = "Jan Kowalski",
      subject    = "Re: Twój e-mail",
      html_body  = "<p>Treść odpowiedzi</p>",
      zalaczniki = [
          # Każdy element to dict z polami jak zwracają respondery:
          # {"base64": "...", "content_type": "image/png", "filename": "obraz.png"}
          response_data["zwykly"].get("emoticon"),
          response_data["zwykly"].get("cv_pdf"),
          # ... etc.
      ],
  )

Funkcja ignoruje None-y i elementy bez pola "base64" w liście załączników,
więc możesz bezpiecznie przekazywać wyniki responderów bez sprawdzania.

Strategia dwóch wiadomości (split):
  Wywołaj wyslij_odpowiedz dwukrotnie — raz po Fali 1, raz po Fali 2.
  Patrz przykład w komentarzu na dole pliku.
"""

import os
import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.base      import MIMEBase
from email               import encoders
from email.utils         import formataddr
from typing              import List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURACJA — odczyt ze środowiska
# ─────────────────────────────────────────────────────────────────────────────
SMTP_HOST         = "smtp.gmail.com"
SMTP_PORT         = 587
SMTP_USER         = os.getenv("SMTP_USER", "")           # Twój Gmail
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")   # 16-znakowy App Password
SMTP_FROM_NAME    = os.getenv("SMTP_FROM_NAME", "Bot")   # Wyświetlana nazwa


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA
# ─────────────────────────────────────────────────────────────────────────────
def wyslij_odpowiedz(
    to_email:    str,
    to_name:     str,
    subject:     str,
    html_body:   str,
    zalaczniki:  Optional[List[Optional[dict]]] = None,
    reply_to:    Optional[str] = None,
) -> bool:
    """
    Wysyła wiadomość HTML z opcjonalnymi załącznikami przez Gmail SMTP.

    Parametry:
        to_email    — adres odbiorcy
        to_name     — imię/nazwa odbiorcy (do nagłówka To:)
        subject     — temat wiadomości
        html_body   — treść HTML
        zalaczniki  — lista dictów {"base64": str, "content_type": str, "filename": str}
                      None-y i elementy bez base64 są ignorowane
        reply_to    — opcjonalny Reply-To (domyślnie = SMTP_USER)

    Zwraca True gdy wysłano, False gdy błąd.
    """
    if not SMTP_USER or not SMTP_APP_PASSWORD:
        logger.error("[smtp] Brak SMTP_USER lub SMTP_APP_PASSWORD — pomijam wysyłkę.")
        return False

    if not to_email:
        logger.error("[smtp] Brak adresu odbiorcy — pomijam wysyłkę.")
        return False

    # ── Buduj wiadomość ────────────────────────────────────────────────────────
    msg = MIMEMultipart("mixed")
    msg["From"]    = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"]      = formataddr((to_name, to_email))
    msg["Subject"] = subject
    msg["Reply-To"]= reply_to or SMTP_USER

    # Część HTML
    msg.attach(MIMEText(html_body or "<p>(brak treści)</p>", "html", "utf-8"))

    # ── Załączniki ─────────────────────────────────────────────────────────────
    attached_count = 0
    for item in (zalaczniki or []):
        if not item:
            continue
        b64  = item.get("base64")
        ct   = item.get("content_type", "application/octet-stream")
        name = item.get("filename", "zalacznik")
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
            part = MIMEBase(*ct.split("/", 1))
            part.set_payload(raw)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=name)
            msg.attach(part)
            attached_count += 1
        except Exception as e:
            logger.warning("[smtp] Nie udało się dodać załącznika '%s': %s", name, e)

    # ── Wysyłka ────────────────────────────────────────────────────────────────
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_bytes())
        logger.info(
            "[smtp] ✅ Wysłano do %s | temat: %s | załączników: %d",
            to_email, subject, attached_count,
        )
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("[smtp] ❌ Błąd autoryzacji — sprawdź SMTP_USER i SMTP_APP_PASSWORD.")
    except smtplib.SMTPException as e:
        logger.error("[smtp] ❌ Błąd SMTP: %s", e)
    except Exception as e:
        logger.error("[smtp] ❌ Nieoczekiwany błąd: %s", e)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# POMOCNIK: zbiera załączniki z danych respondenta w jedną listę
# ─────────────────────────────────────────────────────────────────────────────
def zbierz_zalaczniki_z_response(response_data: dict) -> List[dict]:
    """
    Przeczesuje wyniki responderów i zwraca płaską listę załączników.
    Obsługuje pola będące dict-ami {"base64":...} lub listami takich dictów.

    Przykład pól obsługiwanych:
      emoticon, cv_pdf, raport_pdf, psych_photo_1, psych_photo_2,
      pdf (biznes/zwykly), images (smierc), triptych, debug_txt ...
    """
    result = []

    def _dodaj(item):
        if isinstance(item, dict) and item.get("base64"):
            result.append(item)
        elif isinstance(item, list):
            for el in item:
                _dodaj(el)

    for responder_key, responder_val in response_data.items():
        if not isinstance(responder_val, dict):
            continue

        # Pola, które mogą zawierać załączniki bezpośrednio w responderze
        POLA_ZAŁĄCZNIKI = [
            "pdf", "emoticon", "cv_pdf", "raport_pdf",
            "psych_photo_1", "psych_photo_2", "debug_txt",
            "ankieta_pdf", "horoskop_pdf", "karta_rpg_pdf",
            "gra_html", "explanation_txt",
        ]
        for pole in POLA_ZAŁĄCZNIKI:
            _dodaj(responder_val.get(pole))

        # Pola będące listami (tryptyk, images)
        for pole_list in ["triptych", "images", "videos"]:
            _dodaj(responder_val.get(pole_list, []))

    return result
