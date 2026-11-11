# Installs speakeasy on Windows 10/11. Safe to re-run. No admin rights needed.
#
#   .\install.cmd                       # defaults
#   .\install.cmd -From <path|URL>      # also install your personal config.toml
#
# (install.cmd runs this script with -ExecutionPolicy Bypass, so it works
# without changing the system's execution policy.)
param([string]$From = "")

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  ok $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !! $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

function Test-Python($exe) {
    # True if $exe runs Python 3.11 or newer (the code uses tomllib).
    try {
        & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-Python {
    $candidates = @()
    foreach ($v in "3.13", "3.12", "3.11") {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            # PowerShell 5.1 turns a native command's stderr into an error under "Stop".
            try {
                $exe = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null)
                if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += "$exe".Trim() }
            } catch { }
        }
    }
    foreach ($v in "313", "312", "311") {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        $candidates += Join-Path $env:ProgramFiles "Python$v\python.exe"
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    foreach ($exe in $candidates) {
        # Skip the Microsoft Store stub, which opens the Store instead of running.
        if ($exe -and (Test-Path $exe) -and ($exe -notlike "*WindowsApps*") -and (Test-Python $exe)) {
            return $exe
        }
    }
    return $null
}

function Find-Ffmpeg {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $linked = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\ffmpeg.exe"
    if (Test-Path $linked) { return $linked }
    return $null
}

function Require-Winget($what) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "$what is missing and winget isn't available to install it. Install $what yourself and re-run."
    }
}

Info "Python 3.11 or newer"
$Python = Find-Python
if (-not $Python) {
    Require-Winget "Python 3.11+"
    winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) { Die "Python was installed but can't be found. Open a new terminal and re-run." }
}
Ok $Python

Info "ffmpeg"
$Ffmpeg = Find-Ffmpeg
if (-not $Ffmpeg) {
    Require-Winget "ffmpeg"
    winget install --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    $Ffmpeg = Find-Ffmpeg
    if (-not $Ffmpeg) { Die "ffmpeg was installed but can't be found. Open a new terminal and re-run." }
}
Ok $Ffmpeg

Info "Setting up the Python environment"
$VenvPython = Join-Path $Here "venv\Scripts\python.exe"
if ((Test-Path $VenvPython) -and -not (Test-Python $VenvPython)) {
    Warn "existing venv is older than Python 3.11, recreating it"
    Remove-Item -Recurse -Force (Join-Path $Here "venv")
}
if (-not (Test-Path $VenvPython)) {
    & $Python -m venv (Join-Path $Here "venv")
    if ($LASTEXITCODE -ne 0) { Die "couldn't create the venv" }
}
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet faster-whisper numpy "onnx-asr[cpu,hub]"
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
Ok "venv ready"

$Launcher = Join-Path $Here "speakeasy.cmd"

Info "Downloading the Whisper and Parakeet models (about 1.3 GB the first time)"
& $Launcher --download
if ($LASTEXITCODE -ne 0) { Die "model download failed" }

Info "Personal config, mic and hotkeys"
if ($From) { & $Launcher --setup $From } else { & $Launcher --setup }
if ($LASTEXITCODE -ne 0) { Die "setup failed" }

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host @"

Test:
  1. Click into any text field.
  2. Press the toggle hotkey (Win+/ by default), speak, press it again.
     The cancel hotkey (Win+') throws the recording away instead.
  3. The text is typed at the cursor and also copied to the clipboard.

A grey dot in the notification area (it may be under the ^ arrow) shows
Speakeasy is running; it turns red while recording. It starts at login.
"@
