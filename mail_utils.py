import os
import re
import imaplib
import smtplib
import socket
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from config import (
    IMAP_HOST,
    SMTP_HOST,
    SMTP_PORT,
    MAIL_USER,
    MAIL_PASS,
    ALLOWED_EMAILS_FILE,
)


def load_allowed_emails():
    emails = set()
    try:
        with open(ALLOWED_EMAILS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line:
                    emails.add(line)
    except Exception as e:
        print("Błąd ładowania dozwolonych emaili:", e)
    return emails


ALLOWED_EMAILS = load_allowed_emails()


def fetch_unseen_allowed():
    """
    Pobiera nieprzeczytane wiadomości z INBOX i zwraca listę email.message.Message
    tylko z adresów znajdujących się w ALLOWED_EMAILS.
    W razie błędu zwraca pustą listę.
    """
    messages = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(MAIL_USER, MAIL_PASS)
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return []
        ids = data[0].split()
        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
                if from_addr in ALLOWED_EMAILS:
                    messages.append(msg)
            except Exception as e:
                print("Błąd pobierania wiadomości:", e)
                continue
    except imaplib.IMAP4.error as e:
        print("IMAP error:", e)
    except socket.gaierror as e:
        print("Błąd sieci (IMAP):", e)
    except Exception as e:
        print("Nieoczekiwany błąd IMAP:", e)
    finally:
        try:
            if mail:
                mail.logout()
        except Exception:
            pass
    return messages


def extract_body(msg):
    """
    Zwraca treść wiadomości jako tekst (najpierw najlepszy text/plain, potem text/html).
    W razie błędu zwraca pusty string.
    """
    try:
        if msg.is_multipart():
            plain_parts = []
            html_part = None
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    text = payload.decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    ).strip()
                    if text:
                        plain_parts.append(text)
                elif ctype == "text/html" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if payload and html_part is None:
                        html_part = payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore",
                        )
            if plain_parts:
                return max(plain_parts, key=len)
            if html_part:
                return re.sub(r"<[^>]+>", " ", html_part).strip()
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(
                    msg.get_content_charset() or "utf-8", errors="ignore"
                )
    except Exception as e:
        print("Błąd extract_body:", e)
    return ""


def send_reply_with_attachments(
    to_addr, subject, html_body, inline_images=None, attachments=None, from_name=None
):
    """
    Wysyła mail HTML z opcjonalnymi inline images i załącznikami.
    inline_images: dict cid->blob_bytes_tuple (content_type, filename, bytes)
      but for simplicity we accept blobs as (bytes, content_type, filename)
    attachments: list of tuples (bytes, mime_type, filename)
    """
    try:
        msg = MIMEMultipart()
        msg["From"] = MAIL_USER
        msg["To"] = to_addr
        msg["Subject"] = f"Re: {subject}"

        # Główna część HTML
        msg.attach(MIMEText(html_body, "html", _charset="utf-8"))

        # Inline images: attach as MIMEImage with Content-ID
        if inline_images:
            for cid, blob in inline_images.items():
                try:
                    data_bytes, content_type, filename = blob
                    maintype, subtype = content_type.split("/", 1)
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(data_bytes)
                    encoders.encode_base64(part)
                    part.add_header("Content-ID", f"<{cid}>")
                    part.add_header(
                        "Content-Disposition", f'inline; filename="{filename}"'
                    )
                    msg.attach(part)
                except Exception as e:
                    print("Błąd dołączania inline image:", e)

        # Załączniki
        if attachments:
            for att in attachments:
                try:
                    data_bytes, mime_type, filename = att
                    maintype, subtype = mime_type.split("/", 1)
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(data_bytes)
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition", f'attachment; filename="{filename}"'
                    )
                    msg.attach(part)
                except Exception as e:
                    print("Błąd dołączania załącznika:", e)

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(MAIL_USER, MAIL_PASS)
            server.send_message(msg)
    except Exception as e:
        print("Błąd wysyłania maila:", e)


def send_error_email(error_text):
    try:
        msg = MIMEText(error_text, "plain", _charset="utf-8")
        msg["From"] = MAIL_USER
        msg["To"] = MAIL_USER
        msg["Subject"] = "PC_super – BŁĄD"

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(MAIL_USER, MAIL_PASS)
            server.send_message(msg)
    except Exception as e:
        print("Nie można wysłać maila z błędem:", e)
