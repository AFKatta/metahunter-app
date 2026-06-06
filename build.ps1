#requires -Version 5
<#
.SYNOPSIS
    One-shot Windows build of Metahunter.exe + shareable zip.

.DESCRIPTION
    1. Builds the React frontend (web/).
    2. Runs PyInstaller against metahunter.spec.
    3. Drops a friend-facing README.txt into dist/Metahunter/.
    4. Zips dist/Metahunter/ into dist/Metahunter-<date>.zip.

.NOTES
    Run from the project root with the .venv activated.
        .\.venv\Scripts\Activate.ps1
        .\build.ps1
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "==> 1/4  Building React frontend"
Push-Location web
try {
    if (-not (Test-Path node_modules)) {
        npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "==> 2/4  Cleaning previous build"

# Kill any Metahunter.exe lingering from a previous smoke-test. If we
# don't, the running process holds its DLLs open and Windows refuses
# to delete the dist folder (WinError 5 / "Access is denied").
$running = Get-Process -Name "Metahunter" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "  Stopping $($running.Count) running Metahunter process(es)..."
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# Retry the directory delete a few times — OneDrive can briefly lock
# files even after the process is gone.
$dirs = @("build", "dist")
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { continue }
    $attempts = 0
    while (Test-Path $d) {
        try {
            Remove-Item -Recurse -Force $d -ErrorAction Stop
        } catch {
            $attempts++
            if ($attempts -ge 5) { throw }
            Start-Sleep -Seconds 1
        }
    }
}

Write-Host ""
Write-Host "==> 3/4  Running PyInstaller"
pyinstaller metahunter.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Drop a friend-facing readme alongside the .exe.
$readme = @"
Metahunter
==========

WHAT IT IS
  A personal MTGO Legacy match analyser. It reads the game logs MTGO
  saves on your computer and shows you stats, decks, matchups in a
  local web dashboard. Nothing leaves your machine.

HOW TO RUN
  1. Double-click Metahunter.exe.
  2. A console window opens with status — leave it open.
  3. Your default browser opens automatically at
       http://metahunter.localhost:8765
  4. First run: a consent dialog appears. Read it, tick the box,
     click Continue. (One-time. You can revoke from Settings.)
  5. Close the console window to stop.

WHAT GETS SHARED
  After consent: match metadata (your archetype, opponent's
  archetype, cards observed, on-play, who won) is sent to
  metahunter-api.fly.dev. Your MTGO username goes up plaintext;
  every opponent's username is HMAC-hashed against a 32-byte
  secret that is generated on your machine on first run and never
  leaves your PC. See the community meta at
    https://metahunter-web.vercel.app

WHAT STAYS LOCAL
  All raw match data + your install secret live at
    %LOCALAPPDATA%\Metahunter\
  Don't want your name on the leaderboard? Flip "Show me on the
  leaderboard" off in Settings — your matches still count toward
  aggregate stats, the name just stays hidden.

WIPE EVERYTHING
  Settings -> "Wipe my server data" deletes your install row +
  every match this install uploaded, then re-prompts the consent
  dialog on next launch.

WINDOWS SMARTSCREEN WARNING
  The .exe is not code-signed yet, so Windows may say
    "Windows protected your PC."
  Click "More info" -> "Run anyway." It's safe — this is just the
  default warning for un-signed downloaded binaries.

REPORT ISSUES
  https://github.com/AFKatta/metahunter-app/issues
"@
$readme | Set-Content -Encoding UTF8 -Path "dist\Metahunter\README.txt"

Write-Host ""
Write-Host "==> 4/4  Zipping distributable"
$ts = Get-Date -Format "yyyy-MM-dd"
$zip = "dist\Metahunter-$ts.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path "dist\Metahunter" -DestinationPath $zip -CompressionLevel Optimal

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "  Executable: dist\Metahunter\Metahunter.exe"
Write-Host "  Shareable : $zip"
Write-Host ""
Write-Host "Test it locally with:"
Write-Host "    .\dist\Metahunter\Metahunter.exe"
