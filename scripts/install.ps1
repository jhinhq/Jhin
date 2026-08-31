# Jhin installer for Windows (Docker Desktop).
#
#   powershell -ExecutionPolicy Bypass -c "irm https://get.jhin.ai/install.ps1 | iex"
#
# What it does, in order: checks for git and Docker Desktop (Compose v2),
# clones the repository, writes .env with a random sandbox-runner token,
# generates the secret-store master key, builds and starts the full stack in
# desktop socket mode (local development only — never on a server), and
# applies database migrations. It asks for nothing interactively.
# Environment overrides: JHIN_DIR (install location, default $HOME\jhin),
# JHIN_REPO (git URL).
#
# Works in Windows PowerShell 5.1 and PowerShell 7+.

$ErrorActionPreference = "Stop"

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Say([string]$Message) { Write-Host $Message }
function Fail([string]$Message) { Write-Host "`nerror: $Message" -ForegroundColor Red; exit 1 }
function Assert-LastExit([string]$What) {
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." }
}

$repo = if ($env:JHIN_REPO) { $env:JHIN_REPO } else { "https://github.com/jhinhq/Jhin.git" }

# --- 1. Prerequisites ------------------------------------------------------

Step "Checking prerequisites"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is required. Install it from https://git-scm.com and re-run."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop is required. Install it from https://docs.docker.com/desktop/setup/install/windows-install/ and re-run."
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is required (ships with Docker Desktop)." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "the Docker daemon is not reachable. Start Docker Desktop and re-run." }

$daemonOs = docker info --format "{{.OperatingSystem}}"
Assert-LastExit "docker info"
if ($daemonOs -notmatch "Docker Desktop") {
    Fail "on Windows, Jhin's sandbox needs Docker Desktop (found daemon: $daemonOs).
If your Docker lives inside WSL, run the Linux installer there instead:
  curl -fsSL https://get.jhin.ai | sh"
}
Say "git and Docker Desktop (Compose v2) found; daemon is up."
Say "Desktop socket mode is for a developer's own machine only, never a server."

# --- 2. Get the source -----------------------------------------------------

if ((Test-Path compose.yaml) -and (Test-Path .env.example) -and (Test-Path .git)) {
    $jhinDir = (Get-Location).Path
    Step "Using the existing checkout at $jhinDir"
} else {
    $jhinDir = if ($env:JHIN_DIR) { $env:JHIN_DIR } else { Join-Path $env:USERPROFILE "jhin" }
    if (Test-Path (Join-Path $jhinDir "compose.yaml")) {
        Step "Updating the existing install at $jhinDir"
        git -C $jhinDir pull --ff-only
        if ($LASTEXITCODE -ne 0) { Say "note: could not fast-forward; continuing with the current version." }
    } else {
        Step "Cloning Jhin into $jhinDir"
        git clone --depth 1 $repo $jhinDir
        Assert-LastExit "git clone"
    }
    Set-Location $jhinDir
}

# --- 3. Configuration and secrets ------------------------------------------

Step "Writing configuration"

# UTF-8 without BOM: Compose and the containers read these files as plain
# ASCII/UTF-8, and Windows PowerShell 5.1 would otherwise stamp a BOM.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path .env)) {
    $envText = [IO.File]::ReadAllText((Join-Path $jhinDir ".env.example"))
    [IO.File]::WriteAllText((Join-Path $jhinDir ".env"), $envText, $utf8NoBom)
    Say "created .env from .env.example"
}
# The example ships a well-known dev token; a fresh install gets a random one.
$envPath = Join-Path $jhinDir ".env"
$envText = [IO.File]::ReadAllText($envPath)
if ($envText -match "(?m)^SANDBOX_RUNNER_TOKEN=dev-sandbox-runner-token$") {
    $tokenBytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($tokenBytes)
    $token = -join ($tokenBytes | ForEach-Object { $_.ToString("x2") })
    $envText = $envText -replace "(?m)^SANDBOX_RUNNER_TOKEN=.*$", "SANDBOX_RUNNER_TOKEN=$token"
    [IO.File]::WriteAllText($envPath, $envText, $utf8NoBom)
    Say "set a random SANDBOX_RUNNER_TOKEN"
}

$keyFile = Join-Path $jhinDir "secrets\dev\jhin_master_key"
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Force (Split-Path $keyFile) | Out-Null
    $keyBytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($keyBytes)
    [IO.File]::WriteAllText($keyFile, ([Convert]::ToBase64String($keyBytes) + "`n"), $utf8NoBom)
    Say "generated the secret-store master key at $keyFile"
}

# Inherited Compose/Docker targeting variables could silently point the build
# somewhere else; scrub them. APP_ENV stays unset so the stack starts in its
# production shape (no fake services, first-run setup screen enabled).
foreach ($name in @("APP_ENV", "COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES",
        "COMPOSE_REMOVE_ORPHANS", "COMPOSE_IGNORE_ORPHANS", "DOCKER_DEFAULT_PLATFORM")) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$env:COMPOSE_PROJECT_NAME = "jhin"
# Docker Desktop maps this well-known path to its Linux VM's daemon socket.
$env:SANDBOX_DOCKER_SOCKET_HOST = "/var/run/docker.sock"

# --- 4. Build and start ----------------------------------------------------

Step "Building images (the first build takes a few minutes)"
docker compose -f compose.yaml -f compose.desktop.yaml --profile build build sandbox-image
Assert-LastExit "sandbox image build"

Step "Starting the stack"
docker compose -f compose.yaml -f compose.desktop.yaml up -d --build --wait --wait-timeout 300
Assert-LastExit "docker compose up"

Step "Applying database migrations"
docker compose -f compose.yaml -f compose.desktop.yaml run --rm --no-deps api jhin-db-migrate
Assert-LastExit "database migration"

# --- 5. The jhin launcher --------------------------------------------------
# Every operation needs the install directory and the socket overlay this host
# was set up with. Recording them here is what lets `jhin ...` stand in for a
# full `docker compose -f ... -f ...` line afterwards.

Step "Installing the jhin command"

$configDir = Join-Path $env:APPDATA "Jhin"
New-Item -ItemType Directory -Force $configDir | Out-Null
$configText = "# Written by the Jhin installer. Read by the jhin launcher.`nJHIN_DIR=$jhinDir`nJHIN_OVERLAY=compose.desktop.yaml`n"
[IO.File]::WriteAllText((Join-Path $configDir "config"), $configText, $utf8NoBom)

$binDir = Join-Path $env:LOCALAPPDATA "Jhin\bin"
New-Item -ItemType Directory -Force $binDir | Out-Null
Copy-Item (Join-Path $jhinDir "scripts\jhin.ps1") (Join-Path $binDir "jhin.ps1") -Force
Copy-Item (Join-Path $jhinDir "scripts\jhin.cmd") (Join-Path $binDir "jhin.cmd") -Force
Say "installed $binDir\jhin.cmd"

# PATH is per-user here: a machine-wide change would need elevation, and this
# installer never asks for it.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $binDir), "User")
    $pathWasAdded = $true
} else {
    $pathWasAdded = $false
}
$env:Path = $env:Path + ";" + $binDir

# --- 6. Done ---------------------------------------------------------------

Write-Host "`nJhin is running." -ForegroundColor Green
Say ""
Say "  Open        http://localhost:3000"
Say "              (first visit walks you through creating the owner account)"
Say "  Installed   $jhinDir"
Say "  Master key  $keyFile"
Say "              Back this file up. Losing it makes every stored credential unreadable."
Say "  Commands    jhin status | jhin logs | jhin down | jhin admin --help"
if ($pathWasAdded) {
    Say ""
    Say "  Added $binDir to your PATH. Open a new terminal for \"jhin\" to resolve."
}
Say ""
Start-Process "http://localhost:3000"
