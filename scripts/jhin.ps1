# jhin - one command for a self-hosted Jhin install (Windows).
#
# The PowerShell twin of scripts/jhin. Same idea: the install directory and the
# Docker socket overlay are remembered from install time rather than retyped,
# because getting the overlay wrong is a different security posture, not a typo.
#
# Install location is resolved in this order:
#   1. $env:JHIN_DIR
#   2. the config the installer wrote
#   3. the current directory, if it looks like a Jhin checkout
#   4. $HOME\jhin

$ErrorActionPreference = "Stop"

$config = if ($env:JHIN_CONFIG) { $env:JHIN_CONFIG } else { Join-Path $env:APPDATA "Jhin\config" }

function Say([string]$Message) { Write-Host $Message }
function Fail([string]$Message) { Write-Host "error: $Message" -ForegroundColor Red; exit 1 }

function Show-Usage {
    @'
jhin - run and administer a self-hosted Jhin install

  jhin up                 Start the stack (builds what changed)
  jhin down               Stop the stack
  jhin restart [service]  Restart everything, or one service
  jhin status             What is running, and is it healthy
  jhin logs [service]     Follow logs, all services or one
  jhin open               Open the web UI in a browser
  jhin update             Pull the latest version, rebuild, migrate
  jhin migrate            Apply pending database migrations
  jhin doctor             Check this install's health
  jhin admin ...          Administer accounts and workspaces
  jhin compose ...        Run a raw Compose command with the right files
  jhin version            Versions of Jhin and this launcher

Common admin commands:
  jhin admin user create --email you@example.com --name "Your Name" `
      --workspace jhin-hq --role owner
  jhin admin user set-password --email you@example.com
  jhin admin invite create --email them@example.com --workspace jhin-hq --role member
  jhin admin --help

A password is never passed as an argument - the command asks for it, or reads
it from stdin with --password-stdin. Arguments are visible to every process on
the host and land in your shell history; passwords should not.
'@ | Write-Host
}

function Test-JhinDir([string]$Path) {
    if (-not $Path) { return $false }
    return (Test-Path (Join-Path $Path "compose.yaml")) -and (Test-Path (Join-Path $Path "apps\api"))
}

# --- Locate the install ----------------------------------------------------

$jhinDir = $env:JHIN_DIR
$overlay = $env:JHIN_OVERLAY

if (-not $jhinDir) {
    if (Test-Path $config) {
        foreach ($line in (Get-Content $config)) {
            if ($line -match "^JHIN_DIR=(.+)$") { $jhinDir = $Matches[1].Trim() }
            if ((-not $overlay) -and $line -match "^JHIN_OVERLAY=(.+)$") { $overlay = $Matches[1].Trim() }
        }
    }
}
if (-not $jhinDir) {
    if (Test-JhinDir (Get-Location).Path) { $jhinDir = (Get-Location).Path }
    else { $jhinDir = Join-Path $env:USERPROFILE "jhin" }
}

if (-not (Test-JhinDir $jhinDir)) {
    Fail "no Jhin install at $jhinDir.
Set JHIN_DIR, or re-run the installer:
  powershell -ExecutionPolicy Bypass -c `"irm https://get.jhin.ai/install.ps1 | iex`""
}

# The overlay is a security decision, so an unknown one is fatal rather than
# quietly replaced with a default.
if (-not $overlay) {
    Fail "no Docker socket overlay recorded for $jhinDir.
Set JHIN_OVERLAY (compose.desktop.yaml on Windows), or re-run the installer."
}
if (-not (Test-Path (Join-Path $jhinDir $overlay))) {
    Fail "overlay $overlay does not exist in $jhinDir."
}

Set-Location $jhinDir
if (-not $env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME = "jhin" }
# Docker Desktop maps this well-known path to its Linux VM's daemon socket.
if (-not $env:SANDBOX_DOCKER_SOCKET_HOST) { $env:SANDBOX_DOCKER_SOCKET_HOST = "/var/run/docker.sock" }

function Invoke-Compose { docker compose -f compose.yaml -f $overlay @args }

# Run a command in the api container. Prefer the running one: it starts in
# milliseconds instead of standing up a throwaway container.
function Invoke-Api {
    $running = @(Invoke-Compose ps --services --status running 2>$null)
    if ($running -contains "api") { Invoke-Compose exec api @args }
    else {
        Say "The stack is not running; starting a temporary container."
        Invoke-Compose run --rm --no-deps api @args
    }
}

# --- Commands --------------------------------------------------------------

if ($args.Count -eq 0) { Show-Usage; exit 0 }
$command = $args[0]
$rest = @()
if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }

switch ($command) {
    { $_ -in @("-h", "--help", "help") } { Show-Usage }

    { $_ -in @("up", "start") } {
        Invoke-Compose up -d --build --wait --wait-timeout 300 @rest
        Say ""
        Say "Jhin is up: http://localhost:3000"
    }

    { $_ -in @("down", "stop") } { Invoke-Compose down @rest }

    "restart" { Invoke-Compose restart @rest }

    { $_ -in @("status", "ps") } { Invoke-Compose ps @rest }

    "logs" {
        if ($rest.Count -eq 0) { Invoke-Compose logs --follow --tail 100 }
        else { Invoke-Compose logs --follow --tail 100 @rest }
    }

    "open" { Start-Process "http://localhost:3000" }

    "update" {
        git pull --ff-only
        Invoke-Compose --profile build build sandbox-image
        Invoke-Compose up -d --build --wait --wait-timeout 300
        Invoke-Api jhin-db-migrate
        Say ""
        Say "Updated. http://localhost:3000"
    }

    "migrate" { Invoke-Api jhin-db-migrate }

    "doctor" { Invoke-Api jhin-admin doctor @rest }

    "admin" { Invoke-Api jhin-admin @rest }

    "compose" { Invoke-Compose @rest }

    "version" {
        if (Test-Path "VERSION") { Say ("Jhin " + (Get-Content "VERSION" -Raw).Trim()) }
        Say "install   $jhinDir"
        Say "overlay   $overlay"
        $sha = git -C $jhinDir rev-parse --short HEAD 2>$null
        if ($sha) { Say "revision  $sha" }
    }

    default {
        Write-Host "error: unknown command `"$command`"" -ForegroundColor Red
        Say ""
        Show-Usage
        exit 2
    }
}
