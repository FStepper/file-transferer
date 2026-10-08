# Baut dist\windows\VidTransfer\VidTransfer.exe – zum lokalen Testen unter Windows.
# Die Mac-App muss auf einem Mac bzw. per GitHub-Workflow gebaut werden.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q --upgrade pip
.\.venv\Scripts\python -m pip install -q -e ".[dev]"

.\.venv\Scripts\python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests fehlgeschlagen" }

.\.venv\Scripts\python -m PyInstaller --noconfirm --clean `
    --distpath dist\windows --workpath build\windows packaging\VidTransfer.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fehlgeschlagen" }

Write-Host "Fertig: dist\windows\VidTransfer\VidTransfer.exe"
