# ROZBUDOWANE LOGOWANIE DLA PROGRAMISTY

## Co zostało dodane:

### 1. **Google Drive Integration**
- Logi są automatycznie wysyłane na Google Drive do folderu "AutoResponder_Logs"
- Każdy plik loga ma nazwę: `log_YYYYMMDD_HHMMSS.txt`
- Wymaga skonfigurowania `GOOGLE_SERVICE_ACCOUNT_KEY` w zmiennych środowiskowych

### 2. **Nowe metody logowania w ExecutionLogger:**

#### `log_ai_response(ai_name, prompt, response, tokens_used, duration_sec)`
- Loguje **pełne odpowiedzi AI** z promptami i czasami wykonania
- Przydatne do debugowania problemów z AI

#### `log_pipeline_step(step_name, input_data, output_data, metadata)`
- Loguje każdy krok pipeline'u z danymi wejściowymi/wyjściowymi
- Pełne śledzenie przepływu danych

#### `log_memory_usage()`
- Loguje użycie pamięci RAM (RSS, VMS, procent)
- Wykrywa wycieki pamięci

#### `log_file_operation(operation, file_path, success, size_bytes, error)`
- Loguje wszystkie operacje na plikach
- Rozmiary plików, sukcesy/porażki

#### `log_debug_info(category, data, level)`
- Ogólne logowanie informacji debugowania
- Kategoryzacja dla łatwiejszego filtrowania

### 3. **Rozszerzone logowanie w responderach:**

#### **dociekliwy.py (Eryk Responder):**
```
[ERYK_START] ════════════════════════════════════════════════
[ERYK] Rozpoczęto build_dociekliwy_section dla sender=...
[ERYK] Body length: XXX | Attachments: N
[ERYK] Krok 1: Generowanie struktury gry za pomocą DeepSeek...
[ERYK_JSON] Struktura gry do HTML: {pełny JSON gry}
[ERYK] ✓ JSON POPRAWNIE interpolowany w HTML
[ERYK_END] ════════════════════════════════════════════════
```

#### **zwykly.py (Tyler Durden):**
```
[zwykly_dociekliwy_check] skip_dociekliwy=FALSE
[zwykly_dociekliwy_EXECUTING] ✓ Uruchamiam build_dociekliwy_section()
[zwykly_dociekliwy_SUCCESS] ✓ Dociekliwy zwrócił wynik
[zwykly_dociekliwy_PROCESSED] ✓ Przetworzony JPG diagram
```

### 4. **Konfiguracja Google Drive:**

#### Opcja 1: Zmienna środowiskowa
```bash
export GOOGLE_SERVICE_ACCOUNT_KEY='{"type": "service_account", ...}'
```

#### Opcja 2: Plik service_account.json
Umieść plik `service_account.json` w głównym katalogu projektu.

### 5. **Jak czytać logi:**

Logi zawierają teraz:
- ✅ **Pełne odpowiedzi AI** (prompt + response)
- ✅ **Czasy wykonania** wszystkich operacji
- ✅ **Użycie pamięci** w kluczowych momentach
- ✅ **Operacje na plikach** (rozmiary, sukcesy)
- ✅ **Pipeline steps** z pełnymi danymi
- ✅ **Błędy z traceback** dla debugowania

### 6. **Automatyczne wysyłanie:**
- Na koniec każdej sesji log jest wysyłany na Google Drive
- Link do pliku jest logowany w konsoli
- Jeśli Google Drive nie działa - log zostaje lokalnie

### 7. **Wymagania:**
Dodano do `requirements.txt`:
```
psutil>=5.9.0  # Monitorowanie pamięci
```

## Jak używać:

1. **Uruchom program** - logowanie działa automatycznie
2. **Sprawdź Google Drive** - pliki logów w folderze "AutoResponder_Logs"
3. **Debuguj problemy** - pełne odpowiedzi AI, błędy, czasy wykonania
4. **Monitoruj wydajność** - użycie pamięci, czasy operacji

To powinno dać Ci pełny obraz tego, co dzieje się w programie! 🚀</content>
<parameter name="filePath">c:\python\httpsgithub.comlegionowopawel_AutoResponder_AI_Text\LOGOWANIE_README.md