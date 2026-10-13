@echo off
setlocal
set "NEWS_REPO_ROOT=%~dp0"
set "NEWS_NO_BROWSER="
if /I "%~1"=="--no-browser" set "NEWS_NO_BROWSER=1"
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $taskRoot=$env:NEWS_REPO_ROOT; $taskCandidates=@((Join-Path $taskRoot '.venv\Scripts\python.exe'),(Join-Path $taskRoot 'venv\Scripts\python.exe')); $taskCandidates+=@(Get-Command python.exe,py.exe -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*\WindowsApps\*' } | ForEach-Object { $_.Source }); $taskPython=$null; foreach ($taskCandidate in $taskCandidates) { if (!(Test-Path -LiteralPath $taskCandidate)) { continue }; $taskArgs=@('-c','import sys; print(sys.executable if sys.version_info >= (3, 10) else str())'); if ([IO.Path]::GetFileName($taskCandidate) -eq 'py.exe') { $taskArgs=@('-3')+$taskArgs }; $taskProbe=& $taskCandidate @taskArgs; if ($LASTEXITCODE -eq 0 -and $taskProbe -and (Test-Path -LiteralPath ($taskProbe | Select-Object -Last 1))) { $taskPython=$taskProbe | Select-Object -Last 1; break } }; if (!$taskPython) { Write-Host 'Python 3.10 or newer is required. See README.md.'; exit 1 }; $taskArgs=@((Join-Path $taskRoot 'scripts\start_news.py')); if ($env:NEWS_NO_BROWSER) { $taskArgs+='--no-browser' }; & $taskPython @taskArgs; exit $LASTEXITCODE"
if errorlevel 1 (
  echo News could not start. Follow the message above or see README.md.
  pause
  exit /b 1
)
exit /b 0
