# Builds dist\windows\file-transferer\file-transferer.exe and a ZIP for local testing on Windows.
# The Mac app has to be built on a Mac or via the GitHub workflow.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -SkipTests   # CI: tests ran before
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q --upgrade pip
.\.venv\Scripts\python -m pip install -q -e ".[dev]"

if (-not $SkipTests) {
    .\.venv\Scripts\python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
}

.\.venv\Scripts\python -m PyInstaller --noconfirm --clean `
    --distpath dist\windows --workpath build\windows packaging\file-transferer.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "dist\windows\file-transferer\file-transferer.exe"
if (-not (Test-Path $exe)) { throw "Build output missing: $exe" }
# A windowed exe returns to the shell at once, so wait for it and check its exit code.
# A virus scanner may lock the fresh exe for a moment (PermissionError at start), so retry.
$ok = $false
foreach ($attempt in 1..6) {
    $test = Start-Process -FilePath $exe -ArgumentList "--version" -Wait -PassThru
    if ($test.ExitCode -eq 0) { $ok = $true; break }
    Write-Host "Self-test attempt $attempt failed (exit code $($test.ExitCode)), retrying ..."
    Start-Sleep -Seconds 5
}
if (-not $ok) { throw "App self-test failed" }

$zip = "dist\file-transferer-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\windows\file-transferer" -DestinationPath $zip
Write-Host "Done: $exe"
Write-Host "      $zip"
