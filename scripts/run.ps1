# Bootstrap and run GrokBot on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Creating virtual environment in .venv ..."
    python -m venv $venv
}

& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $root "requirements.txt")

Write-Host "GrokBot is starting at http://127.0.0.1:8000"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
