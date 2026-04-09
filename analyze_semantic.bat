@echo off
REM analyze_semantic.bat - analiza semantyczna plikow .py (pomija backup i .git)

setlocal enabledelayedexpansion

if not exist reviews mkdir reviews
if not exist fixed  mkdir fixed

REM Wykluczone fragmenty (case-insensitive)
set EX1=\backup\
set EX2=\.git\
set EX3=\__pycache__\

REM Iteruj rekurencyjnie po plikach .py
for /R %%F in (*.py) do (
  set "full=%%~fF"
  echo !full! | findstr /I /C:"%EX1%" /C:"%EX2%" /C:"%EX3%" >nul
  if errorlevel 1 (
    REM przygotuj bezpieczna nazwe pliku
    set "safe=%%~pnxF"
    set "safe=!safe:\=_!"
    set "safe=!safe:/=_!"
    set "out=reviews\!safe!.review.txt"
    echo Analysing: %%~pnxF

    REM stworz tymczasowy plik promptu poza zagniezdzeniem echo (uzyj > zamiast ( ) aby uniknac problemow)
    set "tmp=%TEMP%\prompt_!RANDOM!.txt"
    > "%tmp%" echo Analizuj poniższy plik Pythona pod katem bledow merytorycznych i logicznych:
    >> "%tmp%" echo - Wyszukaj bledy logiczne, nieobsluzone przypadki brzegowe, nieprawidlowe zalozenia, race conditions, bledy w obsludze wyjatkow, niebezpieczne operacje na plikach/danych.
    >> "%tmp%" echo - Dla kazdego problemu podaj: 1) krotki opis, 2) dlaczego to problem (przyklady), 3) propozycje poprawki (kod/pseudokod), 4) priorytet (High/Medium/Low).
    >> "%tmp%" echo - Na koncu wygeneruj poprawiona wersje pliku miedzy znacznikami:
    >> "%tmp%" echo ### FIXED FILE START
    >> "%tmp%" echo <tu caly poprawiony kod>
    >> "%tmp%" echo ### FIXED FILE END
    >> "%tmp%" echo Odpowiadaj po polsku.
    >> "%tmp%" echo.
    >> "%tmp%" echo Plik: %%~pnxF
    >> "%tmp%" echo.
    type "%%~fF" >> "%tmp%"

    REM Wywolaj model i zapisz recenzje
    ollama run phi3 < "%tmp%" > "%out%"

    REM Wyodrebnij fragment FIXED FILE przy pomocy PowerShell i zapisz do fixed
    powershell -NoProfile -Command ^
      "$t = Get-Content -Raw -Encoding UTF8 '%out%';" ^
      "if ($t -match '(?s)### FIXED FILE START\s*(.*?)\s*### FIXED FILE END') { $m=$matches[1]; $m.TrimEnd() | Out-File -FilePath 'fixed\!safe!.fixed.py' -Encoding UTF8; Write-Host 'Saved fixed: fixed\!safe!.fixed.py' } else { Write-Host 'No FIXED FILE in: %out%' }"

    del "%tmp%"
  ) else (
    echo Skipped (excluded): %%~fF
  )
)

echo Analiza zakonczona. Recenzje w: reviews\  Poprawione pliki w: fixed\
pause
endlocal
