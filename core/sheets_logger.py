#!/usr/bin/env python3
"""
core/sheets_logger.py
Zapis do Sheets — ODEBRANO (GAS) i WYSŁANO (Render) per responder.

Struktura wierszy:
  message_id | sender | data | temat | status_gas | status_render | responder | treść
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

WARSAW_TZ = timezone(timedelta(hours=2))  # UTC+2 CEST


def _get_sheets_service():
    """Zwraca uwierzytelniony klient Sheets API."""
    from drive_utils import _get_credentials
    from googleapiclient.discovery import build
    creds = _get_credentials()
    return build("sheets", "v4", credentials=creds)


def _append_row(sheet_id: str, values: list) -> bool:
    try:
        svc = _get_sheets_service()
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Historia",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
        return True
    except Exception as e:
        logger.error("[sheets_logger] Błąd append: %s", e)
        return False


def log_odebrano(sheet_id: str, message_id: str, sender: str, subject: str, body: str) -> bool:
    """
    Wpisuje wiersz ODEBRANO — wywoływane przez GAS (lub Render jako potwierdzenie).
    """
    if not sheet_id:
        return False
    ts = datetime.now(tz=WARSAW_TZ).isoformat()
    clean_body = (body or "")[:1000]
    values = [message_id, sender, ts, subject or "", "ODEBRANO", "", "", clean_body]
    ok = _append_row(sheet_id, values)
    if ok:
        logger.info("[sheets_logger] ODEBRANO: %s / %s", message_id, sender)
    return ok


def log_wyslano(sheet_id: str, message_id: str, responder: str, tresc_html: str) -> bool:
    """
    Dopisuje osobny wiersz WYSŁANO dla każdego respondera.
    """
    if not sheet_id:
        return False
    ts = datetime.now(tz=WARSAW_TZ).isoformat()
    # Ogranicz i stripuj HTML
    tresc = _strip_html(tresc_html)[:1000]
    values = [message_id, "", ts, "", "", "WYSŁANO", responder, tresc]
    ok = _append_row(sheet_id, values)
    if ok:
        logger.info("[sheets_logger] WYSŁANO: %s / responder=%s", message_id, responder)
    return ok


def _strip_html(text: str) -> str:
    import re
    if not text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    import re as _re
    text = _re.sub(r"\s+", " ", text).strip()
    return text
