#requires -Version 5
<#
.SYNOPSIS
    One-shot Windows build of Metahunter -- produces both the
    plain-zip distribution AND an Inno Setup installer for auto-
    update support.

.DESCRIPTION
    1. Builds the React frontend (web/).
    2. Runs PyInstaller against metahunter.spec.
    3. Drops a friend-facing README.txt into dist/Metahunter/.
    4. Zips dist/Metahunter/ into dist/Metahunter-v<ver>-windows.zip
       (plus a stable-named alias Metahunter-windows.zip for the
       in-app updater).
    5. Invokes Inno Setup (iscc) against installer.iss to produce
       dist/Metahunter-Setup-v<ver>.exe (plus a stable-named alias
       Metahunter-Setup.exe).

.PARAMETER Sign
    Optional. When passed AND the env vars below are set, code-signs
    both Metahunter.exe and the installer with signtool. Default
    (unsigned) builds skip this. Required env vars:
        $env:SIGN_CERT_THUMBPRINT  -- hash of the cert in CurrentUser/My
        $env:SIGN_TIMESTAMP_URL    -- e.g. http://timestamp.digicert.com

.PARAMETER NoInstaller
    Skip the Inno Setup step (e.g. when iscc isn't on PATH). Just
    produces the zip.

.NOTES
    Run from the project root with the .venv activated.
        .\.venv\Scripts\Activate.ps1
        .\build.ps1

    Prereqs:
        * Node.js  (for the frontend build)
        * Python   (with PyInstaller installed in the venv)
        * Inno Setup 6+ (for the installer step;
          https://jrsoftware.org/isdl.php -- adds iscc to PATH)
#>

[CmdletBinding()]
param(
    [switch]$Sign,
    [switch]$NoInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Pull the canonical version out of __init__.py -- same source of
# truth the runtime uses. Single point of truth for every artifact
# name and the installer's VS_VERSIONINFO.
$versionMatch = (Get-Content "src\mtgo_meta\__init__.py" -Raw) -match '__version__\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw "Couldn't read __version__ from src\mtgo_meta\__init__.py" }
$Version = $Matches[1]
Write-Host "Building Metahunter v$Version" -ForegroundColor Cyan
Write-Host ""

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

# Stop a Metahunter.exe left running from THIS build's own output — a
# smoke test of dist\Metahunter — or it holds its DLLs open and Windows
# refuses to delete the folder (WinError 5 / "Access is denied").
#
# Only processes whose executable sits inside this repo's build or dist
# folders. Matching on the name alone also killed the player's installed
# Metahunter every time a release was built.
$ownDirs = @("build", "dist") | ForEach-Object { (Join-Path $PSScriptRoot $_) + "\" }
$running = @(Get-Process -Name "Metahunter" -ErrorAction SilentlyContinue |
    Where-Object {
        $exe = $_.Path
        $exe -and ($ownDirs | Where-Object {
            $exe.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase)
        })
    })
if ($running.Count -gt 0) {
    Write-Host "  Stopping $($running.Count) Metahunter process(es) running from this build..."
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# Retry the directory delete a few times -- OneDrive can briefly lock
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
  2. A console window opens with status -- leave it open.
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
  leaderboard" off in Settings -- your matches still count toward
  aggregate stats, the name just stays hidden.

WIPE EVERYTHING
  Settings -> "Wipe my server data" deletes your install row +
  every match this install uploaded, then re-prompts the consent
  dialog on next launch.

WINDOWS SMARTSCREEN WARNING
  The .exe is not code-signed yet, so Windows may say
    "Windows protected your PC."
  Click "More info" -> "Run anyway." It's safe -- this is just the
  default warning for un-signed downloaded binaries.

REPORT ISSUES
  https://github.com/AFKatta/metahunter-app/issues
"@
$readme | Set-Content -Encoding UTF8 -Path "dist\Metahunter\README.txt"

Write-Host ""
Write-Host "==> 4/5  Optionally signing Metahunter.exe"
if ($Sign) {
    if (-not $env:SIGN_CERT_THUMBPRINT -or -not $env:SIGN_TIMESTAMP_URL) {
        throw "-Sign requires `$env:SIGN_CERT_THUMBPRINT and `$env:SIGN_TIMESTAMP_URL"
    }
    & signtool sign /sha1 $env:SIGN_CERT_THUMBPRINT /fd sha256 /td sha256 `
        /tr $env:SIGN_TIMESTAMP_URL /d "Metahunter" /du "https://metahunter-web.vercel.app" `
        "dist\Metahunter\Metahunter.exe"
    if ($LASTEXITCODE -ne 0) { throw "signtool failed on Metahunter.exe" }
    Write-Host "  signed Metahunter.exe"
} else {
    Write-Host "  -Sign not passed; skipping"
}

Write-Host ""
Write-Host "==> 5/5  Packaging"

# Versioned zip -- kept for archival downloads.
$zip = "dist\Metahunter-v$Version-windows.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path "dist\Metahunter" -DestinationPath $zip -CompressionLevel Optimal
Write-Host "  zipped : $zip"

# Stable-name copy. The landing page's Download button + the in-app
# updater both fetch via /releases/latest/download/Metahunter-windows.zip
# (and Metahunter-Setup.exe below).
$zipStable = "dist\Metahunter-windows.zip"
if (Test-Path $zipStable) { Remove-Item -Force $zipStable }
Copy-Item -Path $zip -Destination $zipStable
Write-Host "  alias  : $zipStable"

# Inno Setup installer. Stable AppId in installer.iss means a subsequent
# install upgrades over the previous one rather than installing a second
# copy. The in-app updater always downloads the stable-named installer.
if ($NoInstaller) {
    Write-Host "  -NoInstaller passed; skipping Inno Setup step"
} else {
    $iscc = Get-Command iscc -ErrorAction SilentlyContinue
    if (-not $iscc) {
        Write-Warning "Inno Setup (iscc) not on PATH -- skipping installer."
        Write-Warning "Install from https://jrsoftware.org/isdl.php to enable."
    } else {
        $signFlag = if ($Sign) { "/DSIGN=1" } else { "" }
        if ($Sign) {
            # Register signtool with iscc so SignTool=metahunter inside
            # installer.iss invokes signtool with the right thumbprint.
            $signCmd = "signtool.exe sign /sha1 $env:SIGN_CERT_THUMBPRINT /fd sha256 /td sha256 /tr $env:SIGN_TIMESTAMP_URL /d Metahunter `$f"
            & iscc /Smetahunter="$signCmd" $signFlag installer.iss
        } else {
            & iscc installer.iss
        }
        if ($LASTEXITCODE -ne 0) { throw "iscc failed" }

        $installer = "dist\Metahunter-Setup-v$Version.exe"
        $installerStable = "dist\Metahunter-Setup.exe"
        if (Test-Path $installerStable) { Remove-Item -Force $installerStable }
        Copy-Item -Path $installer -Destination $installerStable
        Write-Host "  setup  : $installer"
        Write-Host "  alias  : $installerStable"
    }
}

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "  Executable     : dist\Metahunter\Metahunter.exe"
Write-Host "  Versioned zip  : $zip"
Write-Host "  Stable zip     : dist\Metahunter-windows.zip"
if (-not $NoInstaller -and (Get-Command iscc -ErrorAction SilentlyContinue)) {
    Write-Host "  Installer      : dist\Metahunter-Setup-v$Version.exe"
    Write-Host "  Stable inst.   : dist\Metahunter-Setup.exe"
}
Write-Host ""
Write-Host "Test the .exe locally with:"
Write-Host "    .\dist\Metahunter\Metahunter.exe"
if (-not $NoInstaller -and (Get-Command iscc -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Test the installer locally with:"
    Write-Host "    .\dist\Metahunter-Setup-v$Version.exe"
}
