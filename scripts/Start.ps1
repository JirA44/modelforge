[CmdletBinding()]
param(
    [string]$HostAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw 'Environnement absent. Exécutez scripts\Setup.ps1.'
}

& $Python -m uvicorn modelforge.main:app --host $HostAddress --port $Port
