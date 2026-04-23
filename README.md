# AutoResponder AI Text

System automatycznego odpowiadania na emaile z wykorzystaniem AI. Wysyła spersonalizowane odpowiedzi w zależności od słów kluczowych w treści emaila.

## Architektura

### Komponenty główne:
- **app.py**: Główny webhook Flask obsługujący przychodzące emaile
- **responders/**: Moduły generujące odpowiedzi dla różnych typów zapytań
- **core/**: Moduły wspólne (AI, logging, zarządzanie zasobami)
- **prompts/**: Pliki JSON z promptami dla AI
- **config_responders.json**: Centralna konfiguracja responderów i mapowań

### Przepływ danych:
1. Email przychodzi przez GAS (Google Apps Script)
2. GAS wykrywa keywords i wysyła POST do `/webhook`
3. Flask waliduje i buduje pipeline responderów
4. Respondery wykonują się sekwencyjnie w tle
5. Wyniki są wysyłane emailem i zapisywane w Google Drive/Sheets

## Konfiguracja

### Wymagane zmienne środowiskowe:
```bash
ADMIN_EMAIL=admin@example.com
DRIVE_FOLDER_ID=your_drive_folder_id
HISTORY_SHEET_ID=your_history_sheet_id
SMIERC_HISTORY_SHEET_ID=your_death_sheet_id
GOOGLE_SERVICE_ACCOUNT_KEY={"type": "service_account", ...}
RENDER_INSTANCE_ID=instance_id
```

### config_responders.json
Centralny plik konfiguracyjny zawierający:
- **responders**: Opisy i ustawienia każdego respondera
- **keyword_mappings**: Mapowanie keywords na respondery
- **section_order**: Kolejność wykonania sekcji
- **conditions**: Warunki specjalne
- **validation**: Reguły walidacji
- **performance**: Limity wydajności

## Respondery

| Responder | Opis | Wymaga FLUX | Fala |
|-----------|------|-------------|------|
| nawiazanie | Kontynuacja rozmowy | Nie | 1 |
| analiza | Analiza załączników | Nie | 1 |
| zwykly | Odpowiedź Tyler Durden + Sokrates | Tak | 1 |
| smierc | Sekwencja śmierci | Tak | 2 |
| generator_pdf | Generowanie PDF | Tak | 2 |
| biznes | Odpowiedź biznesowa | Nie | 2 |
| scrabble | Gra Scrabble | Nie | 2 |
| emocje | Odpowiedź emocjonalna | Tak | 2 |

## Keywords

Keywords są konfigurowane w Google Apps Script Script Properties:

| Keyword | Responder | Opis |
|---------|-----------|------|
| KEYWORDS | zwykly | Standardowa odpowiedź AI |
| KEYWORDS1 | biznes | Biznesowa odpowiedź |
| KEYWORDS2 | scrabble | Gra Scrabble |
| KEYWORDS3 | analiza | Analiza załączników |
| KEYWORDS4 | emocje | Emocjonalna odpowiedź |
| KEYWORDS_GENERATOR_PDF | generator_pdf | Generowanie PDF |
| KEYWORDS_JOKER | wszystkie | Wszystkie respondery |
| KEYWORDS_SMIERC | smierc | Sekwencja śmierci |

## Instalacja i uruchomienie

1. **Zainstaluj zależności:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Skonfiguruj Google Services:**
   - Utwórz Service Account w Google Cloud
   - Włącz Google Drive API i Sheets API
   - Skonfiguruj GAS webhook

3. **Uruchom aplikację:**
   ```bash
   python app.py
   ```

## API

### POST /webhook
Główny endpoint dla przychodzących emaili.

**Request Body:**
```json
{
  "body": "Treść emaila",
  "sender": "nadawca@example.com",
  "sender_name": "Jan Kowalski",
  "subject": "Temat",
  "msg_id": "message_id",
  "contains_keyword": true,
  "wants_biznes": false,
  ...
}
```

**Response:**
```json
{"status": "accepted"}
```

## Bezpieczeństwo

- Walidacja wszystkich wejść
- Ochrona przed pętlami (admin email)
- Limity zasobów systemowych
- Sanityzacja danych

## Monitorowanie

- Logi zapisywane w Google Drive
- Metryki pamięci i CPU
- Status aktywnych pipeline'ów
- Cache dla sprawdzania użytkowników

## Rozwój

### Dodanie nowego respondera:
1. Utwórz moduł w `responders/nazwa.py`
2. Dodaj konfigurację w `config_responders.json`
3. Dodaj mapowanie keyword w GAS
4. Zaktualizuj `SECTION_ORDER` jeśli potrzebne

### Testowanie:
```bash
python -m pytest tests/
```

## Troubleshooting

### Problemy z pamięcią:
- Sprawdź `resource_manager.monitor_resources()`
- Zwiększ `memory_threshold_mb` w config

### Błędy AI:
- Retry automatycznie po błędach
- Sprawdź logi dla szczegółów

### Keywords nie działają:
- Sprawdź Script Properties w GAS
- Zweryfikuj mapowanie w `config_responders.json`