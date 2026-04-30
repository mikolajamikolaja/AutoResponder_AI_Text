#!/usr/bin/env python3
"""
core/sheets_logger.py
Zapis do Sheets — ODEBRANO (GAS) i WYSŁANO (Render) per responder.

Struktura wierszy w zakładce "Historia":
  Col A: message_id
  Col B: sender
  Col C: timestamp (ISO)
  Col D: temat
  Col E: status_gas    (ODEBRANO / WEJŚCIE / ODPOWIEDŹ / PRZYJETO)
  Col F: status_render (WYSŁANO)
  Col G: responder     (zwykly / smierc / etc.)
  Col H: treść (skrócona)

ARCHITEKTURA RETRY (v13):
  GAS zapisuje ODEBRANO → wysyła do Render → wraca.
  Render odbiera → NATYCHMIAST zapisuje PRZYJETO (col E) → odpal pipeline w tle → zapisuje WYSŁANO (col F).
  GAS przy następnym uruchomieniu sprawdza: jeśli ODEBRANO bez PRZYJETO → retry (max 3×).
  Jeśli jest PRZYJETO — GAS nie dotyka wiadomości, Render sam skończy i wpisze WYSŁANO.
  Dzięki temu wiadomość nigdy nie jest przetwarzana 2×, nawet gdy pipeline trwa kilka minut.
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


def log_przyjeto(sheet_id: str, message_id: str) -> bool:
    """
    Render potwierdza odbiór zadania — wpisuje PRZYJETO w kolumnie E (status_gas).
    Ten wpis blokuje retry w GAS: GAS widzi PRZYJETO i nie wysyła wiadomości ponownie,
    nawet jeśli pipeline trwa kilka minut i WYSŁANO jeszcze nie ma.

    Wywoływane NATYCHMIAST po odebraniu webhooka, przed odpaleniem wątku pipeline.
    """
    if not sheet_id or not message_id:
        return False
    ts = datetime.now(tz=WARSAW_TZ).isoformat()
    # Col E = PRZYJETO (status_gas), col F puste (WYSŁANO doda job_runner po zakończeniu)
    values = [message_id, "", ts, "", "PRZYJETO", "", "render", ""]
    ok = _append_row(sheet_id, values)
    if ok:
        logger.info("[sheets_logger] PRZYJETO: %s", message_id)
    return ok


def log_wyslano(sheet_id: str, message_id: str, responder: str, tresc_html: str) -> bool:
    """
    Dopisuje osobny wiersz WYSŁANO dla każdego respondera.
    Kolumna F = WYSŁANO, kolumna G = nazwa respondera.
    """
    if not sheet_id:
        return False
    ts = datetime.now(tz=WARSAW_TZ).isoformat()
    tresc = _strip_html(tresc_html)[:1000]
    values = [message_id, "", ts, "", "", "WYSŁANO", responder, tresc]
    ok = _append_row(sheet_id, values)
    if ok:
        logger.info("[sheets_logger] WYSŁANO: %s / responder=%s", message_id, responder)
    return ok


def get_unprocessed_message_ids(sheet_id: str, max_age_hours: int = 24) -> list:
    """
    Zwraca listę message_id które mają wiersz ODEBRANO ale NIE mają PRZYJETO ani WYSŁANO.
    Przeszukuje wiersze nie starsze niż max_age_hours.

    LOGIKA RETRY (v13):
      - ODEBRANO bez PRZYJETO → Render nie odebrał w ogóle → GAS powinien wysłać retry
      - ODEBRANO z PRZYJETO (ale bez WYSŁANO) → Render odebrał i przetwarza → GAS NIE retryuje
      - ODEBRANO z WYSŁANO → gotowe → GAS NIE retryuje

    Używane przez GAS przy starcie do wykrywania wiadomości które Render
    nie obsłużył (np. z powodu 502 / cold start).

    Zwraca: lista dict { message_id, sender, subject, ts }
    """
    if not sheet_id:
        return []
    try:
        svc = _get_sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Historia!A:H",
        ).execute()
        rows = result.get("values", [])
        if len(rows) < 2:
            return []

        cutoff = datetime.now(tz=WARSAW_TZ) - timedelta(hours=max_age_hours)

        odebrano: dict = {}    # message_id → {sender, subject, ts}
        przyjeto_ids: set = set()  # message_id które Render już potwierdził odbiór

        for row in rows[1:]:  # pomiń nagłówek
            if len(row) < 5:
                continue
            msg_id      = (row[0] or "").strip()
            sender      = (row[1] or "").strip()
            ts_str      = (row[2] or "").strip()
            subject     = (row[3] or "").strip()
            status_gas  = (row[4] or "").strip().upper()
            status_rend = (row[5] or "").strip().upper() if len(row) > 5 else ""

            if not msg_id:
                continue

            # Sprawdź wiek wiersza
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=WARSAW_TZ)
                if ts < cutoff:
                    continue
            except Exception:
                continue

            if status_gas == "ODEBRANO" and msg_id not in odebrano:
                odebrano[msg_id] = {"sender": sender, "subject": subject, "ts": ts_str}

            # PRZYJETO = Render potwierdził odbiór (wpisał zaraz po odebraniu webhooka)
            # WYSŁANO = Render skończył (wpisał po zakończeniu pipeline)
            # GAS_FALLBACK = GAS wymuszony fallback po wyczerpaniu retryków
            # ERROR:* / EMPTY:* = Render próbował, coś poszło nie tak — nie retryujemy
            if (status_gas == "PRZYJETO" or
                    status_rend == "WYSŁANO" or
                    status_rend.upper().find("GAS_FALLBACK") != -1 or
                    status_rend.upper().startswith("ERROR:") or
                    status_rend.upper().startswith("EMPTY:")):
                przyjeto_ids.add(msg_id)

        # Zwróć tylko te które mają ODEBRANO ale NIE mają PRZYJETO ani WYSŁANO
        unprocessed = []
        for msg_id, info in odebrano.items():
            if msg_id not in przyjeto_ids:
                unprocessed.append({
                    "message_id": msg_id,
                    "sender":     info["sender"],
                    "subject":    info["subject"],
                    "ts":         info["ts"],
                })

        logger.info(
            "[sheets_logger] Nieobsłużone wiadomości (brak PRZYJETO): %d z %d ODEBRANO",
            len(unprocessed), len(odebrano)
        )
        return unprocessed

    except Exception as e:
        logger.error("[sheets_logger] Błąd get_unprocessed_message_ids: %s", e)
        return []


def _strip_html(text: str) -> str:
    import re
    if not text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text
