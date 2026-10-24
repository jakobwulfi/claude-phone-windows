<#
.SYNOPSIS
Installs claude-phone from this clone, then hands over to `phone setup`.

.DESCRIPTION
Run it in PowerShell from the clone:  .\install.ps1
Or without a clone:  irm https://raw.githubusercontent.com/jakobwulfi/claude-phone-windows/main/install.ps1 | iex
(that clones into %USERPROFILE%\claude-phone-windows, or $env:CLAUDE_PHONE_DIR, and runs the clone's copy of this).
It needs Python 3.12 or newer (it offers to install it with winget), makes .venv, installs claude-phone into it, and
runs `phone setup`, which asks about your settings and wires up Claude Code. Safe to run again: when .venv already has
phone.exe it runs `phone update` (pulls a newer claude-phone, but not while Claude sessions run) and then
`phone setup` (-All asks about everything again, declined parts too).
It never runs pip over an existing phone.exe itself: Claude sessions hold that file open, and replacing it breaks
`phone` and every hook. `phone update` does that safely, and only when the libraries changed.
#>
[CmdletBinding()]
param([switch]$All)

# Run from the web (`irm <raw url of this file> | iex`) there is no clone and no file yet: clone the repo, then run the
# clone's own install.ps1. This part runs in a scope of its own and never calls `exit`: under iex everything shares the
# user's PowerShell session, and `exit` would close their window. CLAUDE_PHONE_DIR and CLAUDE_PHONE_REPO change where
# the clone goes and what it clones.
if (-not $PSScriptRoot) {
    & {
        $repo = if ($env:CLAUDE_PHONE_REPO) { $env:CLAUDE_PHONE_REPO } else { 'https://github.com/jakobwulfi/claude-phone-windows' }
        $dir = if ($env:CLAUDE_PHONE_DIR) { $env:CLAUDE_PHONE_DIR } else { Join-Path $HOME 'claude-phone-windows' }
        $script = Join-Path $dir 'install.ps1'
        if (-not (Test-Path -LiteralPath $script)) {
            if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
                $answer = Read-Host 'claude-phone is installed and updated with Git, which is missing. Install Git with winget? [Y/n]'
                if ($answer -eq '' -or $answer -match '^[yY]') {
                    winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
                    # This window's PATH is from before the install: read it again.
                    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                                [Environment]::GetEnvironmentVariable('Path', 'User')
                }
            }
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                Write-Host 'Install Git (https://git-scm.com/download/win), then run this again.'
                return
            }
            Write-Host "Cloning claude-phone into $dir ..."
            git clone $repo $dir
            if ($LASTEXITCODE -ne 0) {
                Write-Host 'git clone failed (see above).'
                return
            }
        }
        # A child PowerShell with Bypass: running the file directly would meet the execution policy that iex sidesteps.
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script
    }
    return
}

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$phone = Join-Path $PSScriptRoot '.venv\Scripts\phone.exe'
$setupArgs = @('setup')
if ($All) { $setupArgs += '--all' }

function Test-Python312 {
    param([string]$Exe, [string[]]$Pre)
    try {
        $out = & $Exe @Pre -c 'import sys; print(sys.version_info >= (3, 12))' 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq 'True')
    } catch {
        return $false  # e.g. the Microsoft Store's python.exe stub
    }
}

function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Pre = @('-3') },
        @{ Exe = 'python'; Pre = @() },
        @{ Exe = 'python3'; Pre = @() },
        # where winget puts it, before this window's PATH knows
        @{ Exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'); Pre = @() }
    )
    foreach ($candidate in $candidates) {
        if (Test-Python312 -Exe $candidate.Exe -Pre $candidate.Pre) { return $candidate }
    }
    return $null
}

function Stop-Setup {
    param([string]$Message)
    Write-Host $Message
    exit 1
}

if (Test-Path -LiteralPath $phone) {
    & $phone update
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $phone @setupArgs
    exit $LASTEXITCODE
}

$python = Find-Python
if (-not $python) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host 'claude-phone needs Python 3.12 or newer. Install it with winget? [Y/n]'
        if ($answer -eq '' -or $answer -match '^[yY]') {
            winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
            $python = Find-Python
            if (-not $python) {
                Stop-Setup "Python is installed, but this window doesn't see it yet. Open a new PowerShell window and run .\install.ps1 again."
            }
        }
    }
    if (-not $python) {
        Stop-Setup 'Install Python 3.12 or newer from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run .\install.ps1 again.'
    }
}

Write-Host 'Making the virtual environment (.venv)...'
& $python.Exe @($python.Pre) -m venv .venv
if ($LASTEXITCODE -ne 0) { Stop-Setup 'Could not make .venv.' }

Write-Host 'Installing claude-phone...'
& $venvPython -m pip install --disable-pip-version-check -e .
if ($LASTEXITCODE -ne 0) { Stop-Setup 'pip could not install claude-phone (see above).' }

& $phone @setupArgs
exit $LASTEXITCODE
