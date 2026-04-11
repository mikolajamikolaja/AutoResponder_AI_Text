#!/usr/bin/env python3
"""
drive_utils.py
Utility functions for Google Drive integration.
"""
import os
import io
import base64
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account

# Ścieżka do pliku credentials (service account key JSON)
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_KEY_PATH', 'service_account.json')

# Scopes dla Google Drive API
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def get_drive_service():
    """Zwraca uwierzytelnioną usługę Google Drive."""
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
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
        file_metadata = {
            'name': filename,
        }
        if folder_id:
            file_metadata['parents'] = [folder_id]

        # Upload
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,webViewLink'
        ).execute()

        # Ustaw publiczny dostęp (opcjonalne, dla shareable link)
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        service.permissions().create(
            fileId=file['id'],
            body=permission
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
    service = get_drive_service()
    if not service:
        return False

    try:
        # Użyj Sheets API zamiast Drive
        sheets_service = build('sheets', 'v4', credentials=service.credentials)

        body = {
            'values': values
        }
        result = sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()

        return True
    except Exception as e:
        print(f"Błąd aktualizacji arkusza: {e}")
        return False

def save_to_history_sheet(sheet_id, sender, subject, body):
    """
    Zapisuje wiadomość do arkusza historii (jak w GAS).

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
        # Znajdź ostatni wiersz i dodaj nowy
        drive_service = get_drive_service()
        if not drive_service:
            print("Błąd: nie można połączyć z Google Drive")
            return False
            
        sheets_service = build('sheets', 'v4', credentials=drive_service.credentials)
        
        # Pobierz ostatni wiersz
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range='Sheet1!A:A'
        ).execute()
        
        last_row = len(result.get('values', [])) + 1
        
        # Dodaj nowy wiersz
        range_name = f'Sheet1!A{last_row}:D{last_row}'
        values = [[datetime.now().isoformat(), sender, subject or "", (body or "")[:2000]]]
        
        body_update = {
            'values': values
        }
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body=body_update
        ).execute()
        
        print(f"Zapisano historię dla {sender}")
        return True
    except Exception as e:
        print(f"Błąd zapisu historii: {e}")
        return False