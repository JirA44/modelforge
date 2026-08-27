[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw 'Environnement absent. Exécutez scripts\Setup.ps1.'
}

& $Python -m pytest
if ($LASTEXITCODE -ne 0) {
    throw "Les tests ont échoué (code $LASTEXITCODE)."
}

& $Python -m compileall -q modelforge tests
if ($LASTEXITCODE -ne 0) {
    throw "La compilation Python a échoué (code $LASTEXITCODE)."
}

Write-Host 'Tests et compilation ModelForge V1.06 : OK' -ForegroundColor Green
