# semantic_fix_all.ps1
# Analiza i przygotowanie poprawek dla plikow tekstowych w repozytorium
# ZAPISZ PLIK W UTF-8

Set-StrictMode -Version Latest

$excludeDirs = @(".git","backup","__pycache__","node_modules",".venv",".env")
$includeExtensions = @("*.py","*.json","*.yml","*.yaml","*.js","*.ts","*.md","*.txt","*.html","*.css","*.ini","*.cfg","*.xml")
$reviewsDir = Join-Path $PWD "reviews"
$timestamp = (Get-Date).ToString("HHmmss")
$resultDir = Join-Path $PWD ("Poprawki_" + $timestamp)
$logFile = Join-Path $resultDir "fix_log.txt"

if (-not (Test-Path $reviewsDir)) { New-Item -ItemType Directory -Path $reviewsDir | Out-Null }
if (-not (Test-Path $resultDir))  { New-Item -ItemType Directory -Path $resultDir  | Out-Null }

try {
    $me = Get-Process -Id $PID
    $me.PriorityClass = 'BelowNormal'
} catch {
    Write-Host "Nie zmieniono priorytetu procesu: $($_.Exception.Message)"
}

function IsExcludedPath($fullPath, $excludeList) {
    $low = $fullPath.ToLower()
    foreach ($ex in $excludeList) {
        if ($low -like "*\$ex\*") { return $true }
    }
    return $false
}

function Build-Prompt($relPath, $content) {
    $ext = [System.IO.Path]::GetExtension($relPath).ToLower()
    switch ($ext) {
        ".py" {
            $header = "Analizuj plik Pythona: znajdz bledy logiczne, bezpieczenstwo, obsluge wyjatkow, wydajnosc. Podaj liste problemow (opis, dlaczego, propozycja poprawki, priorytet). Na koncu wygeneruj poprawiona wersje miedzy znacznikami."
        }
        ".json" {
            $header = "Zweryfikuj JSON: skladnia, typy wartosci, brakujace pola. Jesli poprawiasz, wygeneruj poprawiony JSON miedzy znacznikami."
        }
        ".yml" { $header = "Zweryfikuj YAML: skladnia i struktura. Wygeneruj poprawiona wersje miedzy znacznikami." }
        ".md" { $header = "Sprawdz dokumentacje: jasnosc i bledy jezykowe. Zaproponuj poprawki i wygeneruj poprawiona wersje miedzy znacznikami." }
        default {
            $header = "Przejrzyj plik tekstowy pod katem bledow i niespelnionych zalozen. Opisz problemy i wygeneruj poprawiona wersje miedzy znacznikami."
        }
    }

    $prompt = @'
{HEADER}
### FIXED FILE START
<tu caly poprawiony kod/treść>
### FIXED FILE END

Plik: {REL}

Kod:
{CODE}
'@ -replace '{HEADER}',$header -replace '{REL}',$relPath -replace '{CODE}',$content

    return $prompt
}

# Zbierz pliki
$files = @()
foreach ($ext in $includeExtensions) {
    $files += Get-ChildItem -Recurse -File -Filter $ext -ErrorAction SilentlyContinue
}
$files = $files | Sort-Object FullName -Unique

foreach ($f in $files) {
    $full = $f.FullName
    if (IsExcludedPath $full $excludeDirs) {
        Write-Host "Pominięto (excluded): $full"
        continue
    }

    $rel = $full.Substring($PWD.Path.Length+1) -replace '\\','/'
    Write-Host "Analysing: $rel"

    try {
        $content = Get-Content -Raw -Encoding UTF8 $full -ErrorAction Stop
    } catch {
        try {
            $content = Get-Content -Raw -Encoding Unicode $full -ErrorAction Stop
        } catch {
            Add-Content -Path $logFile -Value "ERROR: Nie mozna wczytac pliku: $rel - $($_.Exception.Message)"
            Write-Host "ERROR: Nie mozna wczytac pliku: $rel"
            continue
        }
    }

    $prompt = Build-Prompt $rel $content

    $tmp = Join-Path $env:TEMP ("prompt_{0}.txt" -f ([System.Guid]::NewGuid().ToString()))
    $prompt | Out-File -FilePath $tmp -Encoding UTF8

    $safeName = ($rel -replace '[\\/:]','_')
    $reviewPath = Join-Path $reviewsDir ($safeName + ".review.txt")

    try {
        Get-Content -Raw -Encoding UTF8 $tmp | & ollama run phi3:mini > $reviewPath
        Write-Host "Saved review: $reviewPath"
    } catch {
        Add-Content -Path $logFile -Value "ERROR: Model failed for $rel - $($_.Exception.Message)"
        Write-Host "ERROR: Model failed for $rel"
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        continue
    }

    try {
        $reviewText = Get-Content -Raw -Encoding UTF8 $reviewPath
        $pattern = '(?s)### FIXED FILE START\s*(.*?)\s*### FIXED FILE END'
        $m = [regex]::Match($reviewText, $pattern)
        if ($m.Success) {
            $fixedCode = $m.Groups[1].Value.TrimEnd("`r","`n")
            $targetRelPath = $rel
            $targetFullPath = Join-Path $resultDir $targetRelPath
            $targetDir = Split-Path $targetFullPath -Parent
            if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
            $fixedCode | Out-File -FilePath $targetFullPath -Encoding UTF8
            Add-Content -Path $logFile -Value "FIXED: $rel -> $targetRelPath"
            Write-Host "Saved fixed file: $targetFullPath"
        } else {
            Add-Content -Path $logFile -Value "NOFIX: $rel"
            Write-Host "No FIXED FILE in review for: $rel"
        }
    } catch {
        Add-Content -Path $logFile -Value "ERROR: Extraction failed for $rel - $($_.Exception.Message)"
        Write-Host "ERROR: Extraction failed for: $rel"
    } finally {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    }
}

Write-Host "Processing finished. Reviews: $reviewsDir  Results: $resultDir"
Write-Host "Log: $logFile"
