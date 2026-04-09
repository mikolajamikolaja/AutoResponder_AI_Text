# analyze_semantic.ps1
# Analiza semantyczna plikow .py (pomija backup i .git)
# ZAPISZ PLIK W UTF-8

$excludeDirs = @("__pycache__","media","data","backup",".git")
$reviewDir = Join-Path $PWD "reviews"
$fixedDir  = Join-Path $PWD "fixed"

if (-not (Test-Path $reviewDir)) { New-Item -ItemType Directory -Path $reviewDir | Out-Null }
if (-not (Test-Path $fixedDir))  { New-Item -ItemType Directory -Path $fixedDir  | Out-Null }

Get-ChildItem -Recurse -File -Filter *.py | Where-Object {
    $full = $_.FullName.ToLower()
    foreach ($ex in $excludeDirs) {
        if ($full -like "*\$ex\*") { return $false }
    }
    return $true
} | ForEach-Object {
    $file = $_.FullName
    $rel  = $file.Substring($PWD.Path.Length+1) -replace '\\','/'
    Write-Host "Analysing: $rel"

    $content = Get-Content -Raw -Encoding UTF8 $file

    $promptTemplate = @'
Analizuj poniższy plik Pythona pod katem bledow merytorycznych i logicznych:
- Wyszukaj bledy logiczne, nieobsluzone przypadki brzegowe, nieprawidlowe zalozenia, race conditions, bledy w obsludze wyjatkow, niebezpieczne operacje na plikach/danych.
- Dla kazdego problemu podaj: 1) krotki opis, 2) dlaczego to problem (przyklady), 3) propozycje poprawki (kod/pseudokod), 4) priorytet (High/Medium/Low).
- Na koncu wygeneruj poprawiona wersje pliku miedzy znacznikami:
### FIXED FILE START
<tu caly poprawiony kod>
### FIXED FILE END
Odpowiadaj po polsku.

Plik: __FILE__
Kod:
__CODE__
'@

    $prompt = $promptTemplate -replace "__FILE__",$rel
    $prompt = $prompt -replace "__CODE__",$content

    $safeName = ($rel -replace '[\\/:]','_') -replace '\s','_'
    $outputFile = Join-Path $reviewDir ($safeName + ".review.txt")

    # zapisz prompt do pliku tymczasowego
    $tmp = Join-Path $env:TEMP ("prompt_{0}.txt" -f ([System.Guid]::NewGuid().ToString()))
    $prompt | Out-File -FilePath $tmp -Encoding UTF8

    # W PowerShell: przekazujemy zawartosc pliku jako stdin przez potok
    Get-Content -Raw -Encoding UTF8 $tmp | & ollama run phi3 > $outputFile

    Write-Host "Saved review: $outputFile"

    # Wyodrebnij fragment FIXED FILE i zapisz do fixed
    $reviewText = Get-Content -Raw -Encoding UTF8 $outputFile
    $pattern = '(?s)### FIXED FILE START\s*(.*?)\s*### FIXED FILE END'
    $m = [regex]::Match($reviewText, $pattern)
    if ($m.Success) {
        $fixedCode = $m.Groups[1].Value.TrimEnd("`r","`n")
        $fixedPath = Join-Path $fixedDir ($safeName + ".fixed.py")
        $fixedCode | Out-File -FilePath $fixedPath -Encoding UTF8
        Write-Host "Saved fixed file: $fixedPath"
    } else {
        Write-Host "No FIXED FILE in review for: $rel"
    }

    Remove-Item -Force $tmp
}

Write-Host "Analysis finished. Reviews in: $reviewDir  Fixed files in: $fixedDir"
