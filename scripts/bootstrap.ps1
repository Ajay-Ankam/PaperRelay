param()
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" } }
Get-Command git -ErrorAction Stop | Out-Null
Get-Command python -ErrorAction Stop | Out-Null
python --version
Check-Exit
git --version
Check-Exit
if (-not (Test-Path '.git')) {
    git init -b main
    Check-Exit
}
git config core.hooksPath .githooks
Check-Exit
if (-not (Test-Path '.venv')) {
    python -m venv .venv
    Check-Exit
}
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
New-Item -ItemType Directory -Force 'private/conversations' | Out-Null
git check-ignore .env assignment_description.md .venv
Check-Exit
Write-Host 'Bootstrap complete. No dependencies installed, commits created, or remotes changed.'
Write-Host 'Verify git config user.name and user.email before your first commit.'
Write-Host 'Next: claude, then paste docs/FIRST_PROMPT.txt.'
