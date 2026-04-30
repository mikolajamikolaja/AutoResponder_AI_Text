#!/usr/bin/env python3
"""
core/job_runner.py
Asynchroniczny pipeline — każda sekcja: wykonaj → wyślij → drive → sheets → del.

OPTYMALIZACJE PAMIĘCI (512 MB):
  - log.txt generowany strumieniowo i natychmiast zapisywany, bez trzymania w RAM
  - log_svg usunięty całkowicie (największy pożeracz pamięci)
  - del + gc.collect() po każdej sekcji (było, wzmocnione)
  - base64 plików kasowane natychmiast po uploadzie do Drive
  - logger.entries czyszczone po wygenerowaniu log.txt
  - Brak importów na poziomie modułu — lazy import wewnątrz funkcji
"""
import gc
import os
import traceback

from drive_utils import (
    upload_file_to_drive,
    update_sheet_with_data,
    save_to_history_sheet,
)
from core.sheets_logger import log_wyslano
from core.retry_manager import retry_on_failure

SECTION_ORDER = [
    "nawiazanie",
    "analiza",
    "zwykly",
    "smierc",
    "generator_pdf",
    "biznes",
    "scrabble",
    "emocje",
]


def build_section_order(requested: list) -> list:
    return [s for s in SECTION_ORDER if s in requested]


def _file_exists_in_dir(dir_path: str, filename: str) -> bool:
    """Sprawdza rekursywnie, czy plik istnieje w katalogu."""
    for root, dirs, files in os.walk(dir_path):
        if filename in files:
            return True
    return False


def _upload_drive_item(item: dict, folder_id: str) -> bool:
    if not isinstance(item, dict) or not item.get("base64") or not item.get("filename"):
        return False

    # Użyj kopii podczas uploadu, żeby nie usunąć base64 z oryginalnego obiektu
    # który może być później użyty do wysyłki emaila.
    item_copy = dict(item)
    filename = item_copy["filename"]

    # ── Pomiń pliki z katalogów media/ i images/ (duplikacja) ──────────────────
    if _file_exists_in_dir("media", filename) or _file_exists_in_dir(
        "images", filename
    ):
        return True  # Symuluj sukces, żeby nie blokować pipeline

    # Nie zapisuj obrazków zastępczych na dysku Google — unikaj powielania
    if "zastepczy" in filename.lower():
        return True  # Symuluj sukces, żeby nie blokować pipeline

    result = upload_file_to_drive(
        item_copy["base64"],
        item_copy["filename"],
        item_copy.get("content_type", "application/octet-stream"),
        folder_id,
    )
    if not result:
        return False
    item["drive_url"] = result.get("url", "")
    return True


def _upload_drive_section_files(section_data: dict, folder_id: str) -> list:
    uploads = []
    if not isinstance(section_data, dict):
        return uploads

    single_fields = [
        "pdf",
        "emoticon",
        "cv_pdf",
        "log_psych",
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


def run_pipeline_async(
    flask_app,
    data: dict,
    message_id: str,
    tasks: dict,
    sender: str,
    sender_name: str,
    previous_subject: str,
    drive_folder_id: str,
    history_sheet_id: str,
    smierc_sheet_id: str,
    save_to_drive: bool,
    skip_save_to_history: bool,
    logger,
    wyslij_fn,
    zbierz_zalaczniki_fn,
    get_token_fn,
    on_section_start=None,
    on_section_done=None,
    on_section_error=None,
    on_section_empty=None,
    on_pipeline_done=None,
):
    """
    Wykonuje sekcje sekwencyjnie w tle (daemon thread).
    Po każdej sekcji: zapisz Drive → zapisz Sheets → del → gc.
    Na końcu: wyślij JEDEN zbiorczy email.

    WAŻNE: log_wyslano zapisywany po każdej próbie wysyłki (sukces lub porażka).
    """
    # Lazy import — nie ładuj modułów smtp przy starcie serwera
    from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

    with flask_app.app_context():
        ordered_keys = build_section_order(list(tasks.keys()))
        sections_done = []
        combined_results = {}  # Łączymy wszystkie wyniki sekcji
        emails_sent = 0  # Licznik wysłanych emaili

        for section_key in ordered_keys:
            fn = tasks.get(section_key)
            if not fn:
                continue

            result = None
            try:
                flask_app.logger.info("[async] START: %s", section_key)
                if on_section_start:
                    on_section_start(section_key)
                import time as _time

                _t0 = _time.time()
                result = fn()
                _duration = _time.time() - _t0
                flask_app.logger.info("[async] OK:    %s", section_key)
                logger.log_section_result(section_key, success=True)
                if on_section_done and result:
                    on_section_done(section_key, result, _duration)
                elif on_section_empty:
                    on_section_empty(section_key)
            except Exception as e:
                flask_app.logger.error(
                    "[async] BŁĄD '%s': %s\n%s", section_key, e, traceback.format_exc()
                )
                logger.log_section_result(section_key, success=False)
                if on_section_error:
                    on_section_error(section_key, e)
                if history_sheet_id and message_id:
                    try:
                        log_wyslano(
                            history_sheet_id,
                            message_id,
                            f"ERROR:{section_key}",
                            str(e)[:200],
                        )
                    except Exception:
                        pass
                gc.collect()
                continue

            if not result:
                logger.log_section_result(section_key, success=False)
                if history_sheet_id and message_id:
                    try:
                        log_wyslano(
                            history_sheet_id, message_id, f"EMPTY:{section_key}", ""
                        )
                    except Exception:
                        pass
                continue

            # ── Wyślij osobny email dla tej sekcji ────────────────────────────
            if isinstance(result, dict):
                combined_results[section_key] = result
            sections_done.append(section_key)

            # Sekcje obsługiwane wewnętrznie przez zwykly — nie wysyłaj osobnego emaila
            # (zwykly już zawiera emocje/scrabble/analiza w swoim reply_html)
            _SUBSEKCJE_ZWYKLEGO = {"emocje", "scrabble", "analiza"}
            _skip_send = section_key in _SUBSEKCJE_ZWYKLEGO and "zwykly" in ordered_keys

            if _skip_send:
                flask_app.logger.info(
                    "[async] '%s' — pomijam osobny email (zawarty już w zwykly)",
                    section_key,
                )
            else:
                try:
                    _token_refresh(get_token_fn, flask_app, section_key)
                    _send_section_email(
                        section_key=section_key,
                        result=result,
                        sender=sender,
                        sender_name=sender_name,
                        previous_subject=previous_subject,
                        wyslij_odpowiedz_fn=wyslij_odpowiedz,
                        zbierz_fn=zbierz_zalaczniki_z_response,
                        flask_app=flask_app,
                        logger=logger,
                    )
                    emails_sent += 1
                except Exception as e:
                    flask_app.logger.error(
                        "[async] Błąd wysyłki '%s': %s", section_key, e
                    )

            # ── Drive — zapisujemy każdą sekcję osobno ──────────────────────────
            if save_to_drive and drive_folder_id:
                try:
                    _upload_drive_section_files(result, drive_folder_id)
                except Exception as e:
                    flask_app.logger.error(
                        "[async] Błąd Drive '%s': %s", section_key, e
                    )

            # ── Sheets — logujemy każdą sekcję osobno ───────────────────────────
            if history_sheet_id and message_id:
                try:
                    # Pobierz reply_html przed del result, ale skróć do 500 znaków
                    reply_html = ""
                    if isinstance(result, dict):
                        raw_html = result.get("reply_html", "")
                        reply_html = raw_html[:500] if raw_html else ""
                    log_wyslano(history_sheet_id, message_id, section_key, reply_html)
                except Exception as e:
                    flask_app.logger.error(
                        "[async] Błąd Sheets log '%s': %s", section_key, e
                    )

            # ── Specjalny zapis dla śmierci ─────────────────────────────────────
            if section_key == "smierc" and smierc_sheet_id and isinstance(result, dict):
                try:
                    _update_smierc_sheet(smierc_sheet_id, sender, data, result)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd smierc sheet: %s", e)

            # ── Zbieramy wyniki — nie wysyłamy osobnych maili per sekcja ────────
            # Wszystkie sekcje zostaną połączone w jeden email na końcu.
            pass

        if on_pipeline_done:
            on_pipeline_done("", emails_sent)

            # ── Zwolnij pamięć natychmiast ──────────────────────────────────────
            if result is not None:
                del result
            gc.collect()

        # ── Historia nadawcy (raz na końcu) ─────────────────────────────────────
        if history_sheet_id and not skip_save_to_history:
            try:
                body = data.get("body", "")
                subject = data.get("subject", "")
                # Nie zapisuj pustej historii
                if not body.strip() and not subject.strip():
                    flask_app.logger.warning(
                        "[async] Puste dane historii — pomijam zapis"
                    )
                else:
                    save_to_history_sheet(history_sheet_id, sender, subject, body)
                # Zwolnij referencje do dużych danych
                del body, subject
            except Exception as e:
                flask_app.logger.error("[async] Błąd zapisu historii: %s", e)

        # ── log.txt — generuj NA KOŃCU i wyślij na Drive przez logger.finalize() ─
        try:
            logger.finalize()
        except Exception as e:
            flask_app.logger.error("[async] Błąd finalize loggera: %s", e)

        flask_app.logger.info(
            "[async] Pipeline zakończony dla %s | sekcje: %s",
            sender,
            ", ".join(sections_done) if sections_done else "brak",
        )


def _token_refresh(get_token_fn, flask_app, section_key):
    try:
        get_token_fn()
    except RuntimeError as e:
        flask_app.logger.error("[async] Brak tokenu dla '%s': %s", section_key, e)
        raise


def _send_section_email(
    section_key,
    result,
    sender,
    sender_name,
    previous_subject,
    wyslij_odpowiedz_fn,
    zbierz_fn,
    flask_app,
    logger,
) -> bool:
    if not isinstance(result, dict):
        flask_app.logger.warning(
            "[async] '%s' — result nie jest dict: %s", section_key, type(result)
        )
        return False

    email_html = result.get("reply_html", "")
    zal = zbierz_fn({section_key: result})

    flask_app.logger.info(
        "[async] '%s' — email_html len: %d, zalączników: %d",
        section_key,
        len(email_html),
        len(zal),
    )

    if not email_html.strip() and not zal:
        flask_app.logger.info(
            "[async] '%s' — brak treści i załączników, pomijam", section_key
        )
        return False

    # WYJĄTEK: Dla sekcji 'zwykly' zawsze wysyłaj, nawet jeśli brak załączników
    # (bo może zawierać ważne informacje tekstowe)
    if section_key == "zwykly" and not email_html.strip():
        flask_app.logger.info(
            "[async] '%s' — zwykly bez treści, ale wysyłamy dla kompletności",
            section_key,
        )
        email_html = "<p>Odpowiedź w trakcie przetwarzania.</p>"

    subject_line = f"Re: {previous_subject or 'Twoja wiadomość'}"
    if section_key == "smierc" and result.get("subject"):
        subject_line = result["subject"]

    success = wyslij_odpowiedz_fn(
        to_email=sender,
        to_name=sender_name,
        subject=subject_line,
        html_body=email_html or "<p>Załączniki w osobnych plikach.</p>",
        zalaczniki=zal,
    )
    # Zwolnij listę załączników od razu
    del zal
    if success:
        flask_app.logger.info("[async] ✓ Wysłano: %s → %s", section_key, sender)
    else:
        flask_app.logger.warning("[async] ✗ Nie wysłano: %s", section_key)
    return success


def _build_attachment_warning(combined_results: dict, actual_attachments: int) -> str:
    """
    Buduje ostrzeżenie HTML o brakujących załącznikach, jeśli sekcje sugerują
    że powinny były wygenerować pliki.
    """
    expected_fields = [
        "pdf",
        "image",
        "images",
        "raport_pdf",
        "gra_html",
        "docx_list",
        "analizier_pdf",
        "bienes_pdf",
        "plakat_svg",
        "video",
        "videos",
    ]

    expected_count = 0
    for key, value in combined_results.items():
        if key == "reply_html" or key.startswith("log_"):
            continue
        if isinstance(value, dict):
            for field in expected_fields:
                if field in value and value[field] is not None:
                    # Sprawdzenie czy ma base64
                    if isinstance(value[field], dict) and not value[field].get(
                        "base64"
                    ):
                        expected_count += 1
                    elif isinstance(value[field], str):
                        expected_count += 1
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and not item.get("base64"):
                    expected_count += 1

    if expected_count > 0 and actual_attachments < expected_count:
        warning_html = f"""
<div style="background-color:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:12px;margin:10px 0;font-size:13px;color:#856404;">
  <strong>⚠️ Uwaga:</strong> Nie udało się wygenerować {expected_count - actual_attachments} załącznika(ów). 
  Otrzymujesz odpowiedź tekstową. Jeśli to problem, spróbuj ponownie.
</div>
"""
        return warning_html
    return ""


def _send_combined_email(
    combined_results,
    sender,
    sender_name,
    previous_subject,
    wyslij_odpowiedz_fn,
    zbierz_fn,
    flask_app,
    logger,
) -> bool:
    if not isinstance(combined_results, dict):
        flask_app.logger.warning(
            "[async] combined_results nie jest dict: %s", type(combined_results)
        )
        return False

    email_html = combined_results.get("reply_html", "")
    zal = zbierz_fn(combined_results)  # Przekazujemy cały combined_results

    # Dodaj ostrzeżenie o brakujących załącznikach jeśli trzeba
    attachment_warning = _build_attachment_warning(combined_results, len(zal))
    if attachment_warning:
        email_html = attachment_warning + email_html

    flask_app.logger.info(
        "[async] COMBINED — email_html len: %d, załączników: %d",
        len(email_html),
        len(zal),
    )

    if not email_html.strip() and not zal:
        flask_app.logger.info("[async] COMBINED — brak treści i załączników, pomijam")
        return False

    subject_line = f"Re: {previous_subject or 'Twoja wiadomość'}"

    success = wyslij_odpowiedz_fn(
        to_email=sender,
        to_name=sender_name,
        subject=subject_line,
        html_body=email_html or "<p>Załączniki w osobnych plikach.</p>",
        zalaczniki=zal,
    )
    # Zwolnij listę załączników od razu
    del zal
    if success:
        flask_app.logger.info("[async] ✓ Wysłano COMBINED email → %s", sender)
    else:
        flask_app.logger.warning("[async] ✗ Nie wysłano COMBINED email")
    return success


def _update_smierc_sheet(smierc_sheet_id, sender, data, smierc_result):
    """
    Zapisuje wynik sekcji smierc do arkusza Google Sheets.

    Struktura arkusza (zakładka = email z @ i . zamienionymi na _):
      A = nr_etapu
      B = data_smierci  (kopiowana z wiersza 2, nie nadpisujemy)
      C = mail_od_osoby (treść wiadomości od nadawcy)
      D = odpowiedz_pawla (plain text odpowiedzi — czytelny dla użytkownika)
      E = last_msg_id

    Wiersz bieżący  = etap_który_właśnie_obsłużyliśmy + 1
      np. etap 1 → wiersz 2, etap 2 → wiersz 3

    Wiersz następny = nowy_etap + 1
      Wpisujemy tam nowy_etap w kolumnie A żeby script wiedział
      na jakim etapie jest korespondencja przy kolejnym mailu.
    """
    import re
    import html as html_lib

    def strip_html(h):
        if not h:
            return ""
        text = re.sub(r"<style[\s\S]*?</style>", "", h, flags=re.IGNORECASE)
        text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        return html_lib.unescape(text).strip()

    if "nowy_etap" not in smierc_result:
        return

    nowy_etap = smierc_result["nowy_etap"]
    # Etap który właśnie obsłużyliśmy = nowy_etap - 1 (bo smierc.py już inkrementuje)
    etap_biezacy = nowy_etap - 1
    sheet_tab = sender.replace("@", "_").replace(".", "_")

    # Treść odpowiedzi — preferujemy reply_text (plain), fallback do strip_html(reply_html)
    odpowiedz = (
        smierc_result.get("reply_text")
        or strip_html(smierc_result.get("reply_html", ""))
    )[:5000]

    body_text = data.get("body", "")[:2000]
    msg_id = data.get("message_id", data.get("msg_id", ""))[:100]

    # ── Wiersz bieżący: zapisz odpowiedź i treść wiadomości ───────────────────
    # Wiersz = etap_biezacy + 1 (etap 1 → wiersz 2)
    current_row = etap_biezacy + 1
    if current_row < 2:
        current_row = 2  # Minimum wiersz 2 (wiersz 1 to nagłówki)

    range_current = f"{sheet_tab}!A{current_row}:E{current_row}"
    values_current = [[
        etap_biezacy,   # A: nr_etapu (bieżący)
        "",             # B: data_smierci (GAS ustawia, nie nadpisujemy)
        body_text,      # C: mail_od_osoby
        odpowiedz,      # D: odpowiedz_pawla — PLAIN TEXT, czytelny
        msg_id,         # E: last_msg_id
    ]]
    update_sheet_with_data(smierc_sheet_id, range_current, values_current)

    # ── Wiersz następny: zapisz nowy_etap w kolumnie A ────────────────────────
    # Dzięki temu GAS przy kolejnym mailu odczyta właściwy etap z lastRow
    next_row = nowy_etap + 1
    range_next = f"{sheet_tab}!A{next_row}"
    values_next = [[nowy_etap]]
    update_sheet_with_data(smierc_sheet_id, range_next, values_next)
