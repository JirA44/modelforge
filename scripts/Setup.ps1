[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path '.venv')) {
    py -3.10 -m venv .venv
}

& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
Write-Host 'ModelForge 1.0.6 est installé.' -ForegroundColor Green
