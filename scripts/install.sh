#!/bin/sh
# Jhin installer for Linux and macOS.
#
#   curl -fsSL https://get.jhin.ai | sh
#
# What it does, in order: checks for git and Docker (Compose v2), picks the
# right Docker-socket mode for this machine (rootful Linux socket, rootless
# UID-10001 daemon, or Docker Desktop), clones the repository, writes .env
# with a random sandbox-runner token, generates the secret-store master key,
# builds and starts the full stack, and applies database migrations. It never
# uses sudo, never changes socket permissions, and asks for nothing
# interactively. Environment overrides: JHIN_DIR (install location, default
# ~/jhin), JHIN_REPO (git URL).
#
# The production deployment contract, including the strict operator
# environment for servers, lives in the README and docs/deployment.md.
set -eu

REPO="${JHIN_REPO:-https://github.com/jhinhq/Jhin.git}"

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

on_exit() {
  code=$?
  if [ "$code" -ne 0 ]; then
    printf '\n\033[1;31mThe install did not finish.\033[0m The message above says why; fix it and re-run —\nthe installer is idempotent and picks up where it left off.\n' >&2
  fi
}
trap on_exit EXIT

# --- 1. Prerequisites ------------------------------------------------------

step "Checking prerequisites"

case "$(uname -s)" in
  Linux|Darwin) ;;
  MINGW*|MSYS*|CYGWIN*)
    fail "this is the Linux/macOS installer. On Windows, run:
  powershell -ExecutionPolicy Bypass -c \"irm https://get.jhin.ai/install.ps1 | iex\"" ;;
  *) fail "unsupported platform: $(uname -s)" ;;
esac

command -v git >/dev/null 2>&1 || fail "git is required. Install it from https://git-scm.com and re-run."
command -v docker >/dev/null 2>&1 || fail "Docker is required. Install it from https://docs.docker.com/engine/install/ and re-run."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (the 'docker compose' plugin, not legacy docker-compose)."
docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable. Start Docker (or add your user to the docker group and re-login) and re-run."
say "git and Docker (Compose v2) found; daemon is up."

# --- 2. Pick the Docker-socket mode for the sandbox runner -----------------
# Jhin's CLI sandbox runs agent jobs in ephemeral containers, so the
# sandbox-runner service needs a verified path to a Docker daemon. Three
# mutually exclusive modes exist; we detect the right one and never relax
# permissions to force a fit.

step "Detecting Docker-socket mode"

daemon_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
security_options="$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)"

MODE=""
OVERLAY=""
case "$daemon_os" in
  *"Docker Desktop"*)
    # Docker Desktop (macOS, or Desktop on Linux): development-only mode.
    MODE=desktop
    OVERLAY=compose.desktop.yaml
    sock=/var/run/docker.sock
    resolved="$(readlink -f "$sock" 2>/dev/null || true)"
    [ -S "${resolved:-$sock}" ] || fail "cannot find the Docker Desktop socket at $sock."
    SANDBOX_DOCKER_SOCKET_HOST="${resolved:-$sock}"
    export SANDBOX_DOCKER_SOCKET_HOST
    say "Docker Desktop daemon detected: using desktop mode (local development only, never on a server)."
    ;;
  *)
    case "$security_options" in
      *name=rootless*)
        MODE=rootless
        OVERLAY=compose.rootless.yaml
        sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
        [ -S "$sock" ] || fail "the daemon reports rootless mode but no socket was found at $sock."
        owner="$(stat -c %u "$sock" 2>/dev/null || echo '?')"
        if [ "$owner" != "10001" ]; then
          fail "Jhin's rootless contract requires the daemon socket to be owned by host UID 10001 (found UID $owner).
Either run the dedicated rootless daemon as that user, or use a standard rootful Docker install.
Details: the 'Rootless Docker socket (Linux)' section of the README."
        fi
        PHASE10_ROOTLESS_DOCKER_SOCKET="$sock"
        export PHASE10_ROOTLESS_DOCKER_SOCKET
        say "Rootless Docker daemon (UID 10001) detected: using rootless mode."
        ;;
      *)
        MODE=rootful
        OVERLAY=compose.rootful.yaml
        sock=/var/run/docker.sock
        [ -S "$sock" ] || fail "expected the Docker socket at $sock, but it is missing or not a socket."
        [ -L "$sock" ] && fail "$sock is a symlink; the rootful contract requires the real socket path. See the README."
        gid="$(stat -c %g "$sock" 2>/dev/null || echo 0)"
        [ "$gid" -gt 0 ] 2>/dev/null || fail "$sock must have a positive numeric group (found gid $gid). Repair the Docker install; do not chmod the socket."
        SANDBOX_DOCKER_SOCKET_HOST="$sock"
        SANDBOX_DOCKER_GID="$gid"
        export SANDBOX_DOCKER_SOCKET_HOST SANDBOX_DOCKER_GID
        say "Standard root-owned Docker socket detected (docker group $gid): using rootful mode."
        ;;
    esac
    ;;
esac

# --- 3. Get the source -----------------------------------------------------

if [ -f compose.yaml ] && [ -f .env.example ] && [ -d .git ]; then
  JHIN_DIR="$(pwd)"
  step "Using the existing checkout at $JHIN_DIR"
else
  JHIN_DIR="${JHIN_DIR:-$HOME/jhin}"
  if [ -f "$JHIN_DIR/compose.yaml" ]; then
    step "Updating the existing install at $JHIN_DIR"
    git -C "$JHIN_DIR" pull --ff-only || say "note: could not fast-forward; continuing with the current version."
  else
    step "Cloning Jhin into $JHIN_DIR"
    git clone --depth 1 "$REPO" "$JHIN_DIR"
  fi
fi
cd "$JHIN_DIR"

# --- 4. Configuration and secrets ------------------------------------------

step "Writing configuration"

random_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else od -vN 32 -An -tx1 /dev/urandom | tr -d ' \n'; fi
}

if [ ! -f .env ]; then
  cp .env.example .env
  say "created .env from .env.example"
fi
# The example ships a well-known dev token; a fresh install gets a random one.
if grep -q '^SANDBOX_RUNNER_TOKEN=dev-sandbox-runner-token$' .env; then
  token="$(random_hex)"
  sed "s|^SANDBOX_RUNNER_TOKEN=.*|SANDBOX_RUNNER_TOKEN=$token|" .env > .env.tmp && mv .env.tmp .env
  say "set a random SANDBOX_RUNNER_TOKEN"
fi

KEY_FILE=secrets/dev/jhin_master_key
if [ ! -f "$KEY_FILE" ]; then
  mkdir -p "$(dirname "$KEY_FILE")"
  umask_saved="$(umask)"
  umask 077
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 > "$KEY_FILE"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())' > "$KEY_FILE"
  else
    fail "need openssl or python3 to generate the master key."
  fi
  umask "$umask_saved"
  say "generated the secret-store master key at $KEY_FILE"
fi

# Inherited Compose/Docker targeting variables could silently point the build
# somewhere else; scrub them. APP_ENV stays unset so the stack starts in its
# production shape (no fake services, first-run setup screen enabled).
unset APP_ENV COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES \
  COMPOSE_REMOVE_ORPHANS COMPOSE_IGNORE_ORPHANS DOCKER_DEFAULT_PLATFORM || true
COMPOSE_PROJECT_NAME=jhin
export COMPOSE_PROJECT_NAME

compose() { docker compose -f compose.yaml -f "$OVERLAY" "$@"; }

# --- 5. Build and start ----------------------------------------------------

step "Building images (the first build takes a few minutes)"
if [ "$MODE" = rootless ]; then
  # The rootless transport adapter has pull_policy: never and must share the
  # locally built runner image, so that image is built explicitly first.
  compose build sandbox-runner
fi
compose --profile build build sandbox-image

step "Starting the stack"
compose up -d --build --wait --wait-timeout 300

step "Applying database migrations"
compose run --rm --no-deps api jhin-db-migrate

# --- 6. The jhin launcher --------------------------------------------------
# Every operation needs the install directory and the one socket overlay this
# host was set up with. Recording them here is what lets `jhin ...` stand in
# for a full `docker compose -f ... -f ...` line afterwards.

step "Installing the jhin command"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jhin"
mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_DIR/config" <<CONFIG
# Written by the Jhin installer. Read by the jhin launcher.
JHIN_DIR=$JHIN_DIR
JHIN_OVERLAY=$OVERLAY
CONFIG

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
if cp "$JHIN_DIR/scripts/jhin" "$BIN_DIR/jhin" 2>/dev/null && chmod +x "$BIN_DIR/jhin"; then
  LAUNCHER="$BIN_DIR/jhin"
  say "installed $LAUNCHER"
  case ":$PATH:" in
    *":$BIN_DIR:"*) LAUNCHER_ON_PATH=1 ;;
    *) LAUNCHER_ON_PATH=0 ;;
  esac
else
  LAUNCHER=""
  LAUNCHER_ON_PATH=0
  say "could not install the launcher into $BIN_DIR; use $JHIN_DIR/scripts/jhin directly."
fi

# --- 7. Done ---------------------------------------------------------------

printf '\n\033[1;32mJhin is running.\033[0m\n\n'
say "  Open        http://localhost:3000"
say "              (first visit walks you through creating the owner account)"
say "  Installed   $JHIN_DIR"
say "  Master key  $JHIN_DIR/$KEY_FILE"
say "              Back this file up. Losing it makes every stored credential unreadable."
say "  Commands    jhin status | jhin logs | jhin down | jhin admin --help"
if [ "$LAUNCHER_ON_PATH" -eq 0 ] && [ -n "$LAUNCHER" ]; then
  say ""
  say "  $BIN_DIR is not on your PATH yet. Add it to use \"jhin\" by name:"
  say "      export PATH=\"\$HOME/.local/bin:\$PATH\""
  say "  Until then the full path works: $LAUNCHER status"
fi
if [ "$MODE" = desktop ]; then
  say ""
  say "  Reminder: desktop mode is for a developer's own machine only. For a"
  say "  server, use a Linux host and see the README's socket-mode sections."
fi
say ""
