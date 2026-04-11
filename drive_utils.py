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

Render → Environment → dodaj powyższe zmienne, a także DRIVE_FOLDER_ID / HISTORY_SHEET_ID / SMIERC_HISTORY_SHEET_ID.
"""
import os
import io
import base64
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account

# Scopes dla Google Drive i Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/spreadsheets'
]


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
    """Ładuje credentials z osobnych zmiennych środowiskowych Render."""
    sa_info = _load_google_service_account_info()
    if not sa_info:
        raise RuntimeError(
            "Brak konfiguracji Google Service Account. "
            "Ustaw zmienne środowiskowe Render: GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY, GOOGLE_SERVICE_ACCOUNT_CLIENT_EMAIL, "
            "oraz inne powiązane klucze Service Account."
        )
    return service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)


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
        # Jeśli file_data jest base64, dekoduj
        if isinstance(file_data, str):
            file_data = base64.b64decode(file_data)

        # Przygotuj media
        media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime_type, resumable=True)

        # Metadata pliku
        file_metadata = {'name': filename}
        if folder_id:
            file_metadata['parents'] = [folder_id]

        # Upload
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,webViewLink'
        ).execute()

        # Ustaw publiczny dostęp
        service.permissions().create(
            fileId=file['id'],
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()

        return {
            'id': file['id'],
            'url': file.get('webViewLink', f"https://drive.google.com/file/d/{file['id']}/view")
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


def save_to_history_sheet(sheet_id, sender, subject, body):
    """
    Zapisuje wiadomość do arkusza historii.

    Args:
        sheet_id: str (ID arkusza historii)
        sender: str
        subject: str
        body: str

    Returns:
        bool: True jeśli sukces
    """
    if not sheet_id:
        print("Brak HISTORY_SHEET_ID — nie zapisuję historii")
        return False

    try:
        credentials = _get_credentials()
        sheets_service = build('sheets', 'v4', credentials=credentials)

        # Pobierz ostatni wiersz
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range='Sheet1!A:A'
        ).execute()

        last_row = len(result.get('values', [])) + 1
        range_name = f'Sheet1!A{last_row}:D{last_row}'
        values = [[datetime.now().isoformat(), sender, subject or "", (body or "")[:2000]]]

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        print(f"Zapisano historię dla {sender}")
        return True
    except Exception as e:
        print(f"Błąd zapisu historii: {e}")
        return False
