#!/usr/bin/env python3
"""
drive_utils.py
Utility functions for Google Drive integration.

Autoryzacja przez Service Account z osobnych zmiennych środowiskowych Render:
  GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY
  GOOGLE_SERVICE_ACCOUNT_CLIENT_EMAIL
  GOOGLE_SERVICE_ACCOUNT_PROJECT_ID
  GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY_ID
  GOOGLE_SERVICE_ACCOUNT_CLIENT_ID
  GOOGLE_SERVICE_ACCOUNT_AUTH_URI
  GOOGLE_SERVICE_ACCOUNT_TOKEN_URI
  GOOGLE_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL
  GOOGLE_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL

Alternatywnie: OAuth 2.0 z refresh token (dla osobistego dysku Google):
  GMAIL_CLIENT_ID (lub DRIVE_CLIENT_ID)
  GMAIL_CLIENT_SECRET (lub DRIVE_CLIENT_SECRET)
  GMAIL_REFRESH_TOKEN (lub DRIVE_REFRESH_TOKEN)

Render → Environment → dodaj powyższe zmienne, a także DRIVE_FOLDER_ID / HISTORY_SHEET_ID / SMIERC_HISTORY_SHEET_ID.
"""
import os
import io
import base64
import requests
import logging
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials

# Setup logging
logger = logging.getLogger(__name__)

# Scopes dla Google Drive i Sheets API
DRIVE_SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',
]

# OAuth 2.0 zmienne środowiskowe (te same co dla Gmail API)
DRIVE_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
DRIVE_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
DRIVE_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")


def _load_oauth_credentials():
    """Tworzy OAuth credentials z refresh token."""
    if not DRIVE_CLIENT_ID or not DRIVE_CLIENT_SECRET or not DRIVE_REFRESH_TOKEN:
        return None
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": DRIVE_CLIENT_ID,
                "client_secret": DRIVE_CLIENT_SECRET,
                "refresh_token": DRIVE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        token_info = resp.json()
        access_token = token_info["access_token"]
        credentials = OAuthCredentials(
            access_token,
            refresh_token=DRIVE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=DRIVE_CLIENT_ID,
            client_secret=DRIVE_CLIENT_SECRET,
            scopes=DRIVE_SCOPES,
        )
        return credentials
    except Exception as e:
        print(f"Błąd OAuth credentials: {e}")
        return None


def _load_google_service_account_info():
    """Ładuje dane Service Account z osobnych zmiennych środowiskowych Render."""
    private_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    client_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_CLIENT_EMAIL", "").strip()
    if not private_key or not client_email:
        return None

    return {
        "type": os.getenv("GOOGLE_SERVICE_ACCOUNT_TYPE", "service_account"),
        "project_id": os.getenv("GOOGLE_SERVICE_ACCOUNT_PROJECT_ID", ""),
        "private_key_id": os.getenv("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY_ID", ""),
        "private_key": private_key,
        "client_email": client_email,
        "client_id": os.getenv("GOOGLE_SERVICE_ACCOUNT_CLIENT_ID", ""),
        "auth_uri": os.getenv("GOOGLE_SERVICE_ACCOUNT_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": os.getenv("GOOGLE_SERVICE_ACCOUNT_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": os.getenv("GOOGLE_SERVICE_ACCOUNT_AUTH_PROVIDER_X509_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
        "client_x509_cert_url": os.getenv("GOOGLE_SERVICE_ACCOUNT_CLIENT_X509_CERT_URL", ""),
    }


def _get_credentials():
    """Ładuje credentials — najpierw próbuje OAuth, potem Service Account."""
    oauth_creds = _load_oauth_credentials()
    if oauth_creds:
        print("Używam OAuth 2.0 credentials dla Google Drive")
        return oauth_creds

    sa_info = _load_google_service_account_info()
    if sa_info:
        print("Używam Service Account credentials dla Google Drive")
        return service_account.Credentials.from_service_account_info(sa_info, scopes=DRIVE_SCOPES)

    raise RuntimeError(
        "Brak konfiguracji Google Drive. "
        "Ustaw zmienne środowiskowe Render: "
        "GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN (OAuth) LUB "
        "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY, GOOGLE_SERVICE_ACCOUNT_CLIENT_EMAIL (Service Account)."
    )


def get_drive_service():
    """Zwraca uwierzytelnioną usługę Google Drive."""
    try:
        credentials = _get_credentials()
        service = build('drive', 'v3', credentials=credentials)
        return service
    except Exception as e:
        print(f"Błąd inicjalizacji Google Drive: {e}")
        return None


def upload_file_to_drive(file_data, filename, mime_type, folder_id=None):
    """
    Uploads a file to Google Drive.

    Args:
        file_data: bytes or base64 string
        filename: str
        mime_type: str (e.g., 'image/png', 'application/pdf')
        folder_id: str (optional, ID folderu w Drive)

    Returns:
        dict: {'id': file_id, 'url': shareable_link} or None on error
    """
    service = get_drive_service()
    if not service:
        return None

    try:
        if isinstance(file_data, str):
            file_data = base64.b64decode(file_data)

        media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime_type, resumable=True)

        file_metadata = {'name': filename}
        if folder_id:
            file_metadata['parents'] = [folder_id]

        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,webViewLink,webContentLink',
            supportsAllDrives=True,
            supportsTeamDrives=True
        ).execute()

        try:
            service.permissions().create(
                fileId=file['id'],
                body={'type': 'anyone', 'role': 'reader'},
                supportsAllDrives=True,
                supportsTeamDrives=True
            ).execute()
        except HttpError as e:
            if e.resp.status == 403:
                print(f"Błąd ustawiania uprawnień Drive (403) dla pliku {file['id']} — ignoruję")
            else:
                raise

        # Dla HTML używamy webContentLink (pobieranie) — przeglądarka otworzy plik lokalnie
        download_url = file.get('webContentLink') or f"https://drive.google.com/uc?export=download&id={file['id']}"
        return {
            'id': file['id'],
            'url': download_url,
            'view_url': file.get('webViewLink', f"https://drive.google.com/file/d/{file['id']}/view")
        }
    except Exception as e:
        print(f"Błąd uploadu do Drive: {e}")
        return None


def update_sheet_with_data(sheet_id, range_name, values):
    """
    Aktualizuje arkusz Google Sheets z danymi.

    Args:
        sheet_id: str (ID arkusza)
        range_name: str (np. 'Sheet1!A1:B2')
        values: list of lists (dane do wpisania)

    Returns:
        bool: True jeśli sukces
    """
    try:
        credentials = _get_credentials()
        sheets_service = build('sheets', 'v4', credentials=credentials)

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body={'values': values}
        ).execute()
        return True
    except Exception as e:
        print(f"Błąd aktualizacji arkusza: {e}")
        return False


def update_message_status(sheet_id: str, message_id: str, responder: str,
                          status: str, tresc: str) -> bool:
    """
    Dopisuje wiersz statusu dla danego message_id i respondera.

    Args:
        sheet_id: ID arkusza historii
        message_id: Gmail message.getId()
        responder: nazwa sekcji np. 'zwykly', 'smierc'
        status: 'ODEBRANO' lub 'WYSŁANO'
        tresc: treść emaila wejściowego lub odpowiedzi

    Returns:
        bool: True jeśli sukces
    """
    if not sheet_id:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        credentials = _get_credentials()
        sheets_service = build('sheets', 'v4', credentials=credentials)

        warsaw_tz = timezone(timedelta(hours=2))
        timestamp = datetime.now(tz=warsaw_tz).isoformat()

        clean_tresc = _strip_html_to_text_sheets(tresc or "")[:1000]
        values = [[message_id, "", timestamp, "", "", status, responder, clean_tresc]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range='Historia',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': values}
        ).execute()

        print(f"[drive_utils] update_message_status: {status} / {responder} / {message_id}")
        return True
    except Exception as e:
        print(f"Błąd update_message_status: {e}")
        return False


def _strip_html_to_text_sheets(html_text: str) -> str:
    """Konwertuje HTML na zwykły tekst, usuwając tagi i CSS."""
    import re
    if not html_text:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<')
    text = text.replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def save_to_history_sheet(sheet_id, sender, subject, body, is_response=False):
    """
    Zapisuje wiadomość do arkusza historii.

    Args:
        sheet_id: str (ID arkusza historii)
        sender: str
        subject: str
        body: str
        is_response: bool (czy to odpowiedź czy wiadomość wejścia)

    Returns:
        bool: True jeśli sukces
    """
    if not sheet_id:
        print("Brak HISTORY_SHEET_ID — nie zapisuję historii")
        return False

    try:
        from datetime import datetime, timezone, timedelta
        credentials = _get_credentials()
        sheets_service = build('sheets', 'v4', credentials=credentials)

        warsaw_tz = timezone(timedelta(hours=2))
        timestamp = datetime.now(tz=warsaw_tz).isoformat()

        msg_type = "ODPOWIEDŹ" if is_response else "WEJŚCIE"
        clean_body = _strip_html_to_text_sheets(body or "")[:1000]
        values = [[timestamp, sender, msg_type, subject or "", clean_body]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range='Historia',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': values}
        ).execute()

        print(f"Zapisano {msg_type} historii dla {sender} ({timestamp})")
        return True
    except Exception as e:
        print(f"Błąd zapisu historii: {e}")
        return False


def check_user_in_sheet(sheet_id, email, sheet_name='Historia'):
    """
    Sprawdza czy użytkownik (email) znajduje się w arkuszu.

    Args:
        sheet_id: str (ID arkusza)
        email: str (email użytkownika)
        sheet_name: str (nazwa arkusza, domyślnie 'Historia')

    Returns:
        bool: True jeśli użytkownik jest w arkuszu
    """
    if not sheet_id or not email:
        return False

    try:
        credentials = _get_credentials()
        sheets_service = build('sheets', 'v4', credentials=credentials)

        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f'{sheet_name}!B1:B10000'
            ).execute()
        except Exception:
            spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
            first_sheet = spreadsheet['sheets'][0]['properties']['title']
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f'{first_sheet}!B1:B10000'
            ).execute()

        values = result.get('values', [])
        if not values:
            return False

        for row in values:
            if row and row[0].strip().lower() == email.strip().lower():
                return True

        return False
    except Exception as e:
        print(f"Błąd sprawdzania użytkownika w arkuszu: {e}")
        return False
