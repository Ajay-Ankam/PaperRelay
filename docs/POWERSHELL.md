# Windows setup
Extract the ZIP into a temporary folder, then run Setup-Project.ps1 with a selected slug. Setup never overwrites an existing folder and creates no GitHub repo. All instructions are entered through PowerShell; Claude may use Git Bash internally on Windows.

```powershell
Set-Location 'C:\Users\ajay ankam'
$Zip = Join-Path $HOME 'Downloads\research_assistant_handoff.zip'
$Stage = Join-Path $HOME ('handoff-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Expand-Archive -LiteralPath $Zip -DestinationPath $Stage
& (Join-Path $Stage 'Setup-Project.ps1') -ProjectName 'evidenceflow' -ParentPath 'C:\Users\ajay ankam'
Set-Location 'C:\Users\ajay ankam\evidenceflow'
.\scripts\bootstrap.ps1
Get-Content .\docs\FIRST_PROMPT.txt -Raw | Set-Clipboard
claude
```
If downloaded scripts are blocked, inspect them, then Unblock-File the exact trusted scripts. Do not weaken machine-wide execution policy. If necessary run the inspected bootstrap in a one-process PowerShell -ExecutionPolicy Bypass invocation.
Do not type a key in terminal history. Locally edit .env using your editor after bootstrap; Claude must never display it. App code loads it without reading the secret into conversation. No activation needed: use .\.venv\Scripts\python.exe directly.

## Git identity and remote
```powershell
git config user.name
git config user.email
```
If missing, set your own real chosen identity with repository-local git config. Never use a generated/assistant identity. Create a private GitHub repository without README/license/gitignore to avoid initial conflicts, using GitHub CLI if installed:
```powershell
gh auth status
gh repo create evidenceflow --private --source=. --remote=origin
```
Or create an empty repo on GitHub and set its exact URL:
```powershell
git remote add origin https://github.com/YOUR-HANDLE/evidenceflow.git
```
After Claude creates a tested initial baseline on main and then feature work, push branches with git push -u origin BRANCH. First establish the baseline commit on main before switching to feat/bootstrap; baseline is setup only, not feature implementation. Both messages must satisfy hook. Never put a token in the remote URL.

## Milestone integration
After a reviewed feature passes checks, main session performs:
```powershell
git switch main
git merge --no-ff feat/bootstrap -m "Merge validated bootstrap foundation into the main project branch"
git push origin main
```
Future features start from updated main. Review git log -1 --format=full before pushing. No force push. Keep remote branch pushes at every verified checkpoint; no untested success claims.

## History
Start Claude after .claude files exist. Check /agents and /skills (or /help for installed-version equivalents). Save exact initial prompt before sending; /export private/conversations/session-01.txt if supported. Export each session before closing, and retain native/subagent history where available. Do not rely solely on PowerShell Start-Transcript for full interactive output. Preserve this ChatGPT planning conversation too. Keep raw histories private; sanitize only secrets/private identifiers for submission, with a redaction note.
