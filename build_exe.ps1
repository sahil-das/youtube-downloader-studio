<#
.SYNOPSIS
    Builds the YouTube Downloader Studio Windows executable using PyInstaller.

.DESCRIPTION
    Creates a repeatable Windows desktop build in dist\YouTubeDownloaderStudio\ (one-folder mode by default)
    or dist\YouTubeDownloaderStudio.exe (when -OneFile is specified).
    Bundles CustomTkinter themes/assets and local application assets while excluding test suites.
    Writable runtime state (settings, SQLite database, logs) is stored in %LOCALAPPDATA%\YouTubeDownloaderStudio.

.PARAMETER Clean
    Remove existing build\ and dist\ directories before packaging (default: $true).

.PARAMETER OneFile
    Build a single-file executable instead of the recommended one-folder bundle (default: $false).

.EXAMPLE
    .\build_exe.ps1
    .\build_exe.ps1 -OneFile
#>
[CmdletBinding()]
param(
    [switch]$Clean = $true,
    [switch]$OneFile = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectRoot

try {
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " YouTube Downloader Studio -- Windows Executable Build" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan

    # Resolve Python executable (prefer project .venv)
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonExe = (Get-Command python).Source
    } else {
        throw "Python executable not found. Activate your virtual environment or install Python 3.11+."
    }

    Write-Host "[1/4] Using Python runtime: $PythonExe" -ForegroundColor Gray

    # Verify PyInstaller is installed
    & $PythonExe -m PyInstaller --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in the active Python environment. Run: pip install -r requirements-dev.txt"
    }

    # Clean previous build outputs if requested
    if ($Clean) {
        Write-Host "[2/4] Cleaning previous build\ and dist\ artifacts..." -ForegroundColor Gray
        foreach ($TargetDir in @("build", "dist")) {
            $FullPath = Join-Path $ProjectRoot $TargetDir
            if (Test-Path $FullPath) {
                Remove-Item -Recurse -Force $FullPath
            }
        }
        $SpecFile = Join-Path $ProjectRoot "YouTubeDownloaderStudio.spec"
        if (Test-Path $SpecFile) {
            Remove-Item -Force $SpecFile
        }
    }

    # Construct PyInstaller argument list
    $PyInstallerArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", "YouTubeDownloaderStudio",
        "--windowed",
        "--collect-all", "customtkinter",
        "--collect-all", "yt_dlp_ejs",
        "--hidden-import", "yt_dlp",
        "--hidden-import", "PIL._tkinter_finder",
        "--exclude-module", "pytest",
        "--exclude-module", "tests",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide6",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pandas",
        "--exclude-module", "scipy",
        "--exclude-module", "playwright",
        "--exclude-module", "selenium",
        "--exclude-module", "gradio",
        "--exclude-module", "nicegui",
        "--exclude-module", "mitmproxy"
    )

    if ($OneFile) {
        $PyInstallerArgs += "--onefile"
        Write-Host "[3/4] Running PyInstaller in ONE-FILE mode..." -ForegroundColor Yellow
    } else {
        $PyInstallerArgs += "--onedir"
        Write-Host "[3/4] Running PyInstaller in ONE-FOLDER mode (recommended)..." -ForegroundColor Gray
    }

    $IconPath = Join-Path $ProjectRoot "assets\app_icon.ico"
    if (Test-Path $IconPath) {
        $PyInstallerArgs += @("--icon", $IconPath)
    }

    $AssetsDir = Join-Path $ProjectRoot "assets"
    if (Test-Path $AssetsDir) {
        $PyInstallerArgs += @("--add-data", "assets;assets")
    }

    $PyInstallerArgs += "run.py"

    & $PythonExe @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited with error code $LASTEXITCODE."
    }

    # Verify the resulting executable exists
    if ($OneFile) {
        $ExpectedExe = Join-Path $ProjectRoot "dist\YouTubeDownloaderStudio.exe"
    } else {
        $ExpectedExe = Join-Path $ProjectRoot "dist\YouTubeDownloaderStudio\YouTubeDownloaderStudio.exe"
    }

    if (-not (Test-Path $ExpectedExe)) {
        throw "Build completed, but expected executable was not found at: $ExpectedExe"
    }

    $ExeItem = Get-Item $ExpectedExe
    $SizeMB = [Math]::Round($ExeItem.Length / 1MB, 2)
    Write-Host "[4/4] Build verified successfully!" -ForegroundColor Green
    Write-Host "      Executable : $ExpectedExe ($($SizeMB) MB)" -ForegroundColor Green
    Write-Host "      User Data  : $($env:LOCALAPPDATA)\YouTubeDownloaderStudio" -ForegroundColor Gray
}
finally {
    Pop-Location
}
