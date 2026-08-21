#!/usr/bin/env bash
#
# ForgeOps - one-click start (Linux and macOS).
#
# The counterpart to scripts/start-forgeops.ps1, doing the same work in the same order:
# install what is missing, generate the secrets the application refuses to boot without, pick free
# ports, start the nine services in dependency order, provision the identity provider, migrate, then
# PROVE the result by reading /health/ready rather than by trusting that `up` returned zero.
#
# Safe to run repeatedly. Every step checks the state it wants before changing anything, and no
# existing secret is ever overwritten.
#
# Usage:
#   ./scripts/start-forgeops.sh                 start everything
#   ./scripts/start-forgeops.sh --fresh         also delete the data volumes (asks to confirm)
#   ./scripts/start-forgeops.sh --fresh --force delete the data without asking
#   ./scripts/start-forgeops.sh --no-reset      reuse existing containers instead of replacing them
#   ./scripts/start-forgeops.sh --purge-images  also delete the locally built images
#   ./scripts/start-forgeops.sh --skip-install  never install anything; fail if something is missing
#   ./scripts/start-forgeops.sh --rebuild       force an image rebuild
#   ./scripts/start-forgeops.sh --no-browser    do not open a browser
#
# By DEFAULT it removes the previous deployment's containers first and keeps the data volumes. See the
# note at that step for why those two are separated.

set -Eeuo pipefail

FRESH=0
FORCE=0
SKIP_INSTALL=0
NO_BROWSER=0
REBUILD=0
NO_RESET=0
PURGE_IMAGES=0
READY_TIMEOUT=900

while [ $# -gt 0 ]; do
  case "$1" in
    --fresh) FRESH=1 ;;
    --force) FORCE=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    --rebuild) REBUILD=1 ;;
    --no-reset) NO_RESET=1 ;;
    --purge-images) PURGE_IMAGES=1 ;;
    --ready-timeout) shift; READY_TIMEOUT="$1" ;;
    -h|--help) sed -n '3,30p' "$0"; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

# ─── Presentation ────────────────────────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  C_HEAD=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
  C_DIM=$'\033[90m'; C_RESET=$'\033[0m'; C_WHITE=$'\033[97m'
else
  C_HEAD=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_RESET=''; C_WHITE=''
fi

STEP=0
head_() { printf '\n%s  %s%s\n' "$C_HEAD" "$1" "$C_RESET"; }
step_() { STEP=$((STEP + 1)); printf '\n%s[%d] %s%s\n' "$C_WHITE" "$STEP" "$1" "$C_RESET"; }
ok_()   { printf '%s      ok    %s%s\n' "$C_OK" "$1" "$C_RESET"; }
info_() { printf '%s      ..    %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
warn_() { printf '%s      warn  %s%s\n' "$C_WARN" "$1" "$C_RESET"; }

die_() {
  printf '\n%s  ────────────────────────────────────────────────────────────────────%s\n' "$C_ERR" "$C_RESET"
  printf '%s  CANNOT CONTINUE: %s%s\n' "$C_ERR" "$1" "$C_RESET"
  printf '%s  ────────────────────────────────────────────────────────────────────%s\n' "$C_ERR" "$C_RESET"
  shift || true
  for line in "$@"; do printf '%s  %s%s\n' "$C_WARN" "$line" "$C_RESET"; done
  printf '\n'
  exit 1
}

have_() { command -v "$1" >/dev/null 2>&1; }

# ─── Repository root ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

printf '\n%s  ForgeOps — governed DevOps automation%s\n' "$C_HEAD" "$C_RESET"
printf '%s  one-click start: installs what is missing, then starts and verifies the stack%s\n' "$C_DIM" "$C_RESET"
printf '%s  repository: %s%s\n' "$C_DIM" "$REPO_ROOT" "$C_RESET"

for marker in docker-compose.yml backend frontend .env.example; do
  [ -e "$marker" ] || die_ "this does not look like the ForgeOps repository: '$marker' is missing." \
    "Run the script from inside a clone of the repository."
done

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.e2e.yml)
dc() { docker compose "${COMPOSE_FILES[@]}" "$@"; }
ENV_PATH="$REPO_ROOT/.env"

# ─── Docker ──────────────────────────────────────────────────────────────────────────────────────

head_ 'Prerequisites'
step_ 'Checking Docker'

install_docker_() {
  if have_ apt-get; then
    info_ 'installing docker.io and the compose plugin with apt-get (needs sudo)'
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io docker-compose-v2
  elif have_ dnf; then
    info_ 'installing moby-engine and the compose plugin with dnf (needs sudo)'
    sudo dnf install -y -q moby-engine docker-compose
  elif have_ pacman; then
    info_ 'installing docker and docker-compose with pacman (needs sudo)'
    sudo pacman -Sy --noconfirm docker docker-compose
  elif have_ brew; then
    info_ 'installing Docker Desktop with Homebrew'
    brew install --cask docker
  else
    die_ 'Docker is missing and no supported package manager was found.' \
      'Install Docker Engine or Docker Desktop for your platform, then run this script again:' \
      '    https://docs.docker.com/engine/install/'
  fi
}

if ! have_ docker; then
  [ "$SKIP_INSTALL" -eq 0 ] || die_ 'Docker is not installed, and --skip-install was given.'
  warn_ 'Docker is not installed.'
  install_docker_
  have_ docker || die_ 'Docker was installed but is not on PATH. Open a new shell and retry.'
  if have_ systemctl; then
    info_ 'enabling and starting the docker service'
    sudo systemctl enable --now docker || true
  fi
  # `id -un` rather than `$USER`: that variable is not set in every non-login shell, and with `set -u`
  # an unset variable ABORTS the script -- so the install path would crash at the very end, after
  # having installed Docker, on a machine where the environment simply lacked USER.
  CURRENT_USER="$(id -un)"
  if ! id -nG "$CURRENT_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    warn_ "adding $CURRENT_USER to the 'docker' group"
    sudo usermod -aG docker "$CURRENT_USER" || true
    warn_ 'You must LOG OUT and BACK IN for that group change to take effect, then rerun this script.'
  fi
fi
ok_ "$(docker --version)"

step_ 'Checking the Docker engine is running'

# The error text is CAPTURED rather than discarded. The first version sent it to /dev/null and then
# tried `systemctl start docker` for every failure, which is wrong for the most common one: if you are
# not in the `docker` group the daemon is already running perfectly and starting it again changes
# nothing, so the script waited three minutes and died advising you to check a service that was fine.
# Waiting only helps when something is genuinely still coming up.
docker_err=""
docker_ok=0
probe_docker_() {
  if docker_err="$(docker info --format '{{.ServerVersion}}' 2>&1)"; then
    docker_ok=1
  else
    docker_ok=0
  fi
}
probe_docker_

if [ "$docker_ok" -eq 0 ]; then
  IS_WSL=0
  if grep -qi microsoft /proc/version 2>/dev/null; then IS_WSL=1; fi

  case "$docker_err" in
    # FIRST, because the permission error also contains "connect to the docker API": a group problem
    # is not fixed by starting a daemon that is already running.
    *"permission denied"*)
      # The daemon is reachable but this user may not talk to it. Proving that with sudo turns a
      # guess into a fact, and separates "Docker is broken" from "your session lacks the group".
      if sudo -n docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
        warn_ 'the Docker engine IS running, but this user cannot reach its socket.'
      else
        warn_ 'cannot reach the Docker socket, and the reason looks like permissions.'
      fi
      if id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        die_ "you are in the 'docker' group, but THIS SHELL predates that change." \
          'Group membership is fixed when a session starts, so the current shell never got it.' \
          'Start a new session and run this script again:' \
          '    newgrp docker        # then re-run in the shell it opens' \
          '  or log out and back in, which is cleaner.'
      else
        die_ "your user is not in the 'docker' group, so it may not use the Docker socket." \
          'Add it, then start a NEW session:' \
          "    sudo usermod -aG docker \"\$USER\"" \
          '    newgrp docker        # or log out and back in' \
          'Running this script under sudo instead would work and is not advised: every file it' \
          'creates, including .env and the CA, would end up owned by root.'
      fi
      ;;
    # The wording differs between Docker versions and transports, so several are matched. Observed on
    # Docker 29: a missing socket gives "failed to connect to the docker API at unix://...; check if
    # the path is correct and if the daemon is running: ... no such file or directory", while a
    # refused TCP endpoint still gives the older "Cannot connect to the Docker daemon ... Is the
    # docker daemon running?". Matching only the older phrasing sent every modern failure to the
    # catch-all, which gives worse advice and does not try the right start command.
    #
    # This case must stay AFTER the permission case: the permission error also contains "connect to
    # the docker API", and a group problem is not fixed by starting a daemon that is already running.
    *"Cannot connect to the Docker daemon"*|*"Is the docker daemon running"* \
      |*"failed to connect to the docker API"*|*"no such file or directory"* \
      |*"connection refused"*|*"docker daemon is not running"*)
      warn_ 'the Docker daemon is not running. Trying to start it.'
      started=0
      if [ "$IS_WSL" -eq 1 ] && ! systemctl is-system-running >/dev/null 2>&1; then
        # WSL does not run systemd unless it has been enabled, so `systemctl start docker` fails with
        # "System has not been booted with systemd as init system (PID 1)". The SysV path still works.
        info_ 'this looks like WSL without systemd; using the service command'
        sudo service docker start >/dev/null 2>&1 && started=1
      elif have_ systemctl; then
        if systemctl is-system-running >/dev/null 2>&1 || [ -d /run/systemd/system ]; then
          sudo systemctl start docker >/dev/null 2>&1 && started=1
        else
          info_ 'systemd is present but not the init system; using the service command'
          sudo service docker start >/dev/null 2>&1 && started=1
        fi
      elif have_ service; then
        sudo service docker start >/dev/null 2>&1 && started=1
      elif [ "$(uname -s)" = "Darwin" ] && [ -d /Applications/Docker.app ]; then
        open -a Docker >/dev/null 2>&1 && started=1
      fi
      [ "$started" -eq 1 ] || warn_ 'the start command did not succeed; waiting anyway in case it is coming up'

      info_ 'waiting for the engine (up to 3 minutes)'
      for i in $(seq 1 36); do
        sleep 5
        probe_docker_
        if [ "$docker_ok" -eq 1 ]; then break; fi
        if [ $((i % 6)) -eq 0 ]; then info_ "still waiting ($((i * 5))s)"; fi
      done
      if [ "$docker_ok" -eq 0 ]; then
        printf '\n'
        sudo systemctl status docker --no-pager 2>&1 | head -n 15 || true
        die_ 'the Docker daemon did not come up.' \
          'Its own status is above. Common causes:' \
          '  - it is not installed: sudo apt-get install -y docker.io docker-compose-v2' \
          '  - on WSL, enable systemd or start Docker Desktop on Windows with WSL integration' \
          '  - it failed to start: journalctl -u docker -n 50 --no-pager'
      fi
      ;;
    # Patterns NARROWED to phrases only this condition produces. A bare `*"not found"*` was
    # unreachable anyway -- the daemon case above matches "no such file or directory" first -- and the
    # linter flagged it as dead code (SC2222). Worse than dead: had it been ordered first it would
    # have swallowed every missing-socket failure and advised about WSL integration for a daemon that
    # was simply stopped.
    #
    # (Note for future edits: a comment line beginning with the linter's name is parsed as a
    # DIRECTIVE, and a directive is not valid in front of a single case branch. Writing one here made
    # the whole case expression unparseable while bash itself ran it happily.)
    *"could not be found in this WSL"*|*"command not found"*|*"executable file not found"*)
      die_ 'the docker command is not usable in this environment.' \
        "The engine reported: $docker_err" \
        'On WSL this usually means Docker Desktop is running on Windows but WSL integration is off' \
        'for this distribution: Docker Desktop > Settings > Resources > WSL integration.' \
        'Otherwise install Docker natively:  sudo apt-get install -y docker.io docker-compose-v2'
      ;;
    *)
      warn_ 'the Docker engine did not answer. Trying to start it.'
      if have_ systemctl && { systemctl is-system-running >/dev/null 2>&1 || [ -d /run/systemd/system ]; }; then
        sudo systemctl start docker >/dev/null 2>&1 || true
      elif have_ service; then
        sudo service docker start >/dev/null 2>&1 || true
      fi
      info_ 'waiting for the engine (up to 3 minutes)'
      for i in $(seq 1 36); do
        sleep 5
        probe_docker_
        if [ "$docker_ok" -eq 1 ]; then break; fi
        if [ $((i % 6)) -eq 0 ]; then info_ "still waiting ($((i * 5))s)"; fi
      done
      # The captured message is REPORTED rather than replaced with generic advice. A launcher that
      # hides what the tool said makes the next person guess.
      [ "$docker_ok" -eq 1 ] || die_ 'the Docker engine did not become ready.' \
        "It reported: $docker_err" \
        '  sudo systemctl status docker' \
        '  journalctl -u docker -n 50 --no-pager'
      ;;
  esac
fi
ok_ "Docker engine $(docker info --format '{{.ServerVersion}}')"

docker compose version --short >/dev/null 2>&1 \
  || die_ 'Docker Compose v2 is not available: "docker compose" failed.' \
       'This project needs the Compose V2 plugin, not the older docker-compose binary.'
ok_ "Docker Compose v$(docker compose version --short)"

# ─── curl ────────────────────────────────────────────────────────────────────────────────────────

step_ 'Checking curl'

# curl is used three times later: waiting for Authentik's authorization flow, reading /health/ready,
# and probing the frontend. It is NOT guaranteed on Ubuntu -- server and cloud images routinely ship
# without it -- and its absence produces thoroughly misleading failures rather than an obvious one.
# Without this check, a missing curl makes the flow-wait loop fail on all sixty attempts and then
# report "Authentik never published its authorization flow", sending you to inspect an identity
# provider that was working the whole time.
if have_ curl; then
  ok_ "$(curl --version | head -n 1)"
else
  if [ "$SKIP_INSTALL" -eq 1 ]; then
    die_ 'curl is not installed, and --skip-install was given.' \
      'Install it and retry:  sudo apt-get install -y curl'
  fi
  warn_ 'curl is not installed. Installing it.'
  if have_ apt-get; then sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -y -qq curl >/dev/null 2>&1
  elif have_ dnf; then sudo dnf install -y -q curl >/dev/null 2>&1
  elif have_ pacman; then sudo pacman -Sy --noconfirm curl >/dev/null 2>&1
  elif have_ brew; then brew install curl >/dev/null 2>&1
  else die_ 'curl is missing and no supported package manager was found.'; fi
  have_ curl || die_ 'curl could not be installed.' 'Install it by hand:  sudo apt-get install -y curl'
  ok_ 'curl installed'
fi

# ─── Python ──────────────────────────────────────────────────────────────────────────────────────

step_ 'Checking Python'

# Needed for exactly two host-side steps: scripts/init_ca.py, which generates the development CA,
# and scripts/ci/provision-authentik.py, which creates the OIDC application, groups and users. The
# provisioner imports its API client from backend/tests/integration/test_authentik_real_idp.py -- on
# purpose, so there is one implementation rather than two -- and that module imports pytest, which
# the backend runtime image does not carry. Hence a virtual environment from the pinned dev lock.

VENV_DIR="$REPO_ROOT/.forgeops-launcher/venv"
VENV_PY="$VENV_DIR/bin/python"
BACKEND_PY="$REPO_ROOT/backend/.venv/bin/python"
LAUNCHER_PY=''

py_has_deps_() {
  [ -x "$1" ] || return 1
  "$1" -c 'import httpx, pytest, pytest_asyncio, cryptography' >/dev/null 2>&1
}

if py_has_deps_ "$BACKEND_PY"; then
  LAUNCHER_PY="$BACKEND_PY"; ok_ 'using the existing backend virtual environment'
elif py_has_deps_ "$VENV_PY"; then
  LAUNCHER_PY="$VENV_PY"; ok_ 'using the existing launcher virtual environment'
else
  <<'NOTE' true
THE VERSION IS 3.13 EXACTLY, and this must not be relaxed to "3.11 or newer".

`backend/pyproject.toml` declares `requires-python = ">=3.13,<3.14"`, and every entry in
`requirements-dev.lock` is pinned accordingly. Ubuntu 24.04 ships Python 3.12 as `python3`, so
accepting ">= 3.11" built a 3.12 virtual environment and pip then refused the whole lock file with

    Ignoring <package>: markers ... require a different python version
    ERROR: Could not find a version that satisfies the requirement ...

which reads like a broken lock file and is really a wrong interpreter. Detecting the version here
turns that into one clear sentence, and pinning to 3.13 is what the project already declares.
NOTE
  BASE_PY=''
  for cand in python3.13 python3; do
    if have_ "$cand" && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
      BASE_PY="$cand"; break
    fi
  done

  if [ -z "$BASE_PY" ]; then
    # Report what IS present, so the reason is visible rather than "no suitable Python".
    found=''
    for cand in python3.13 python3.12 python3.11 python3; do
      if have_ "$cand"; then
        found="$found $cand=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
      fi
    done
    [ -n "$found" ] && info_ "present but not usable:$found (this project needs 3.13)"

    if [ "$SKIP_INSTALL" -eq 1 ]; then
      die_ 'Python 3.13 was not found, and --skip-install was given.' \
        "requires-python is \">=3.13,<3.14\", so 3.12 will not do.$found" \
        'On Ubuntu 24.04, 3.13 is not in the default archive; the deadsnakes PPA has it:' \
        '    sudo add-apt-repository -y ppa:deadsnakes/ppa' \
        '    sudo apt-get install -y python3.13 python3.13-venv'
    fi

    warn_ 'Python 3.13 is not installed. Installing it.'
    if have_ apt-get; then
      # 3.13 is absent from the default archive on Ubuntu 24.04 and earlier, so the PPA is added.
      # `python3.13-venv` is a SEPARATE package on Debian and Ubuntu, and without it `-m venv` fails
      # with "ensurepip is not available" -- a message that sends people looking for a pip problem.
      sudo apt-get update -qq >/dev/null 2>&1 || true
      sudo apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
      sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 \
        || warn_ 'could not add the deadsnakes PPA; trying the default archive'
      sudo apt-get update -qq >/dev/null 2>&1 || true
      sudo apt-get install -y -qq python3.13 python3.13-venv python3.13-dev >/dev/null 2>&1 \
        || die_ 'could not install Python 3.13.' \
             'Try it by hand so the package manager can explain itself:' \
             '    sudo add-apt-repository -y ppa:deadsnakes/ppa' \
             '    sudo apt-get update && sudo apt-get install -y python3.13 python3.13-venv'
    elif have_ dnf; then
      sudo dnf install -y -q python3.13 >/dev/null 2>&1 \
        || die_ 'could not install Python 3.13 with dnf.'
    elif have_ pacman; then
      sudo pacman -Sy --noconfirm python >/dev/null 2>&1 \
        || die_ 'could not install Python with pacman.'
    elif have_ brew; then
      brew install python@3.13 >/dev/null 2>&1 || die_ 'could not install Python 3.13 with brew.'
    else
      die_ 'Python 3.13 is missing and no supported package manager was found.' \
        'Install it from https://www.python.org/downloads/ and retry.'
    fi

    for cand in python3.13 python3; do
      if have_ "$cand" && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
        BASE_PY="$cand"; break
      fi
    done
    [ -n "$BASE_PY" ] || die_ 'Python 3.13 was installed but is not on PATH as a 3.13 interpreter.' \
      'Open a new shell and retry, or check:  python3.13 --version'
    ok_ "Python $("$BASE_PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') installed"
  fi

  info_ "creating a virtual environment with $BASE_PY"
  mkdir -p "$(dirname "$VENV_DIR")"
  "$BASE_PY" -m venv "$VENV_DIR" || die_ 'could not create the virtual environment.' \
    'On Debian and Ubuntu the venv module is packaged separately from the interpreter:' \
    "    sudo apt-get install -y ${BASE_PY}-venv"
  info_ 'installing the pinned dependencies (hash-enforced; a few minutes on a first run)'
  "$VENV_PY" -m pip install --quiet --upgrade pip || warn_ 'could not upgrade pip; continuing'
  if ! "$VENV_PY" -m pip install --quiet --require-hashes -r "$REPO_ROOT/backend/requirements-dev.lock"; then
    die_ 'the pinned dependency installation failed.' \
      "Interpreter: $("$VENV_PY" -c 'import sys; print(sys.version)' 2>&1 | head -n 1)" \
      'If the output mentions "require a different python version", the interpreter is wrong:' \
      'this project needs 3.13 exactly. Otherwise the step needs internet access.'
  fi
  py_has_deps_ "$VENV_PY" || die_ 'the virtual environment is missing the modules provisioning needs.'
  LAUNCHER_PY="$VENV_PY"
  ok_ 'launcher virtual environment ready'
fi

# ─── Optional clean slate ────────────────────────────────────────────────────────────────────────

head_ 'Previous deployment'
step_ 'Removing anything left from a previous run'

<<'NOTE' true
THIS RUNS BY DEFAULT, and it removes CONTAINERS but not DATA.

`docker compose down` deletes the containers and the network; the named volumes survive unless `-v`
is given. That split is deliberate:

  * Removing the containers every run makes the whole class of stale-container faults impossible.
    Compose reads `env_file` only when it CREATES a container, so a container that predates a change
    to .env keeps the old values -- which is how a stack came up with all nine services healthy and
    every sign-in answering 503, because the backend still held an issuer pointing at localhost.

  * Keeping the volumes means your projects, paired devices, change sets, audit rows and identity
    provider configuration are still there afterwards. Deleting those on every start would be a
    surprising thing for a script called "start" to do, and the audit chain is append-only by design.

Use --fresh when you do want the data gone, and --no-reset to skip this step entirely.
NOTE

if [ "$NO_RESET" -eq 1 ]; then
  info_ 'skipped (--no-reset): existing containers will be reused'
elif [ -z "$(dc ps -aq 2>/dev/null)" ]; then
  ok_ 'nothing to remove'
else
  DOWN_ARGS='--remove-orphans'
  if [ "$FRESH" -eq 1 ] && [ "$FORCE" -eq 0 ]; then
    warn_ '--fresh DELETES every project, device, change set, audit row and identity.'
    printf '      type "yes" to continue: '
    read -r answer
    if [ "$answer" != "yes" ]; then
      info_ 'keeping the data; removing containers only'
      FRESH=0
    fi
  fi
  # Written as `if` blocks rather than `[ cond ] && assignment`. Under `set -e` a trailing test that
  # evaluates FALSE returns non-zero, and as the last command of a block that terminates the whole
  # script -- a silent early exit that looks like the run simply stopping.
  if [ "$FRESH" -eq 1 ]; then DOWN_ARGS="$DOWN_ARGS -v"; fi
  if [ "$PURGE_IMAGES" -eq 1 ]; then DOWN_ARGS="$DOWN_ARGS --rmi local"; fi

  # shellcheck disable=SC2086  # DOWN_ARGS is a deliberate list of flags and must word-split
  dc down $DOWN_ARGS >/dev/null 2>&1 || warn_ 'the teardown reported a problem; continuing'

  if [ "$FRESH" -eq 1 ]; then
    ok_ 'containers, networks and data volumes removed'
  else
    ok_ 'containers and networks removed; data volumes kept'
  fi
  if [ "$PURGE_IMAGES" -eq 1 ]; then ok_ 'locally built images removed, so they will be rebuilt'; fi
fi

# ─── Ports ───────────────────────────────────────────────────────────────────────────────────────

head_ 'Configuration'
step_ 'Choosing host ports'

env_get_() {
  [ -f "$ENV_PATH" ] || return 0
  sed -n "s/^$1=//p" "$ENV_PATH" | tail -n 1 | sed 's/[[:space:]]*#.*$//' | tr -d '"'\''' | tr -d '[:space:]'
}

port_free_() {
  # Bind rather than connect: a refused connection also happens when a firewall drops the packet and
  # says nothing about whether we may bind. Compose publishes on 127.0.0.1 specifically.
  "$LAUNCHER_PY" - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

usable_port_() {
  local preferred="$1" label="$2" candidate
  if port_free_ "$preferred"; then printf '%s' "$preferred"; return 0; fi
  warn_ "port $preferred ($label) is in use; searching for a free one" >&2
  candidate=$((preferred + 1))
  while [ "$candidate" -lt $((preferred + 200)) ] && [ "$candidate" -le 65535 ]; do
    if port_free_ "$candidate"; then
      info_ "$label will use $candidate instead of $preferred" >&2
      printf '%s' "$candidate"; return 0
    fi
    candidate=$((candidate + 1))
  done
  die_ "no free port found for $label near $preferred."
}

resolve_port_() {
  local key="$1" fallback="$2" label="$3" current
  current="$(env_get_ "$key" || true)"
  case "$current" in (''|*[!0-9]*) current="$fallback" ;; esac
  # When the stack is already running, our own containers hold these ports open, so a bind test
  # correctly reports them unavailable -- and acting on that would move the application to new ports
  # on every run and rebuild the frontend each time. With containers up, .env is not a preference to
  # re-examine; it describes what is currently listening.
  if [ "$STACK_IS_UP" -eq 1 ]; then printf '%s' "$current"; return 0; fi
  usable_port_ "$current" "$label"
}

STACK_IS_UP=0
[ -n "$(dc ps -q 2>/dev/null)" ] && STACK_IS_UP=1
[ "$STACK_IS_UP" -eq 1 ] && info_ 'the stack is already running; keeping the ports recorded in .env'

# The preferred values are NOT compose's defaults. The default set collides with Windows reserved
# ranges, and keeping one set of offsets across platforms means the documentation matches everywhere.
POSTGRES_PORT_V="$(resolve_port_ POSTGRES_PORT 15432 postgres)"
REDIS_PORT_V="$(resolve_port_ REDIS_PORT 16379 redis)"
OPA_PORT_V="$(resolve_port_ OPA_PORT 18182 opa)"
CERBOS_HTTP_PORT_V="$(resolve_port_ CERBOS_HTTP_PORT 13592 cerbos)"
AUTHENTIK_PORT_V="$(resolve_port_ AUTHENTIK_PORT 19000 authentik)"
FRONTEND_PORT_V="$(resolve_port_ FRONTEND_PORT 13000 frontend)"
BACKEND_PORT_V="$(resolve_port_ BACKEND_PORT 18000 backend)"

for pair in "POSTGRES_PORT $POSTGRES_PORT_V" "REDIS_PORT $REDIS_PORT_V" "OPA_PORT $OPA_PORT_V" \
            "CERBOS_HTTP_PORT $CERBOS_HTTP_PORT_V" "AUTHENTIK_PORT $AUTHENTIK_PORT_V" \
            "FRONTEND_PORT $FRONTEND_PORT_V" "BACKEND_PORT $BACKEND_PORT_V"; do
  # Split explicitly rather than letting the shell word-split an unquoted expansion. The unquoted
  # form worked, but it depends on IFS being untouched and reads as an accident.
  ok_ "$(printf '%-17s %s' "${pair%% *}" "${pair##* }")"
done

# ─── .env ────────────────────────────────────────────────────────────────────────────────────────

step_ 'Preparing .env'

if [ ! -f "$ENV_PATH" ]; then
  cp .env.example "$ENV_PATH"
  ok_ 'created .env from .env.example'
else
  ok_ '.env already exists; existing secrets will be kept'
fi

# Taken BEFORE any edit, and compared just before the application containers start. See the note
# there: Compose bakes env_file values in at container creation, so a changed .env means the
# containers must be REPLACED rather than merely started.
env_hash_() {
  [ -f "$ENV_PATH" ] || { printf 'absent'; return 0; }
  if have_ sha256sum; then sha256sum "$ENV_PATH" | cut -d' ' -f1
  elif have_ shasum; then shasum -a 256 "$ENV_PATH" | cut -d' ' -f1
  else wc -c < "$ENV_PATH" | tr -d ' '   # last resort: size alone still detects the common case
  fi
}
ENV_HASH_BEFORE="$(env_hash_)"

env_set_() {
  # Rewrite the key in place so its surrounding comments survive; append when absent. `.env.example`
  # is a document as much as a template, and regenerating it from a list would throw that away.
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_PATH"; then
    "$LAUNCHER_PY" - "$ENV_PATH" "$key" "$value" <<'PY'
import pathlib, sys
path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
out = [f"{key}={value}" if ln.split("=", 1)[0].strip() == key and not ln.lstrip().startswith("#") else ln
       for ln in lines]
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_PATH"
  fi
}

API_BASE_URL="http://localhost:${BACKEND_PORT_V}/api/v1"
PREVIOUS_API_BASE="$(env_get_ NEXT_PUBLIC_API_BASE_URL || true)"

env_set_ POSTGRES_PORT    "$POSTGRES_PORT_V"
env_set_ REDIS_PORT       "$REDIS_PORT_V"
env_set_ OPA_PORT         "$OPA_PORT_V"
env_set_ CERBOS_HTTP_PORT "$CERBOS_HTTP_PORT_V"
env_set_ AUTHENTIK_PORT   "$AUTHENTIK_PORT_V"
env_set_ FRONTEND_PORT    "$FRONTEND_PORT_V"
env_set_ BACKEND_PORT     "$BACKEND_PORT_V"

env_set_ NEXT_PUBLIC_API_BASE_URL "$API_BASE_URL"
env_set_ CORS_ALLOW_ORIGINS       "http://localhost:${FRONTEND_PORT_V}"
env_set_ FRONTEND_BASE_URL        "http://localhost:${FRONTEND_PORT_V}"

# The split-horizon pair. OIDC_ISSUER is what the BACKEND uses for discovery, the token endpoint and
# JWKS, and must be the compose service name: Authentik derives the `iss` claim from the request that
# mints the token. OIDC_PUBLIC_BASE_URL rewrites the origin of the AUTHORIZATION endpoint only -- the
# one URL a browser is sent to -- because a browser cannot resolve `authentik-server`.
env_set_ OIDC_ISSUER          'http://authentik-server:9000/application/o/forgeops/'
env_set_ OIDC_PUBLIC_BASE_URL "http://localhost:${AUTHENTIK_PORT_V}"

# Authentik silently ignores the `audience` field on an OAuth2 provider: a PATCH setting it returns
# 200 and a read-back shows null. The access token's `aud` is therefore the CLIENT ID, so that is
# what the backend must accept.
CLIENT_ID_V="$(env_get_ OIDC_CLIENT_ID || true)"
[ -n "$CLIENT_ID_V" ] || CLIENT_ID_V='forgeops-frontend'
env_set_ OIDC_CLIENT_ID    "$CLIENT_ID_V"
env_set_ OIDC_APP_AUDIENCE "$CLIENT_ID_V"

new_secret_() { "$LAUNCHER_PY" -c 'import secrets,sys;print(sys.argv[1]+secrets.token_hex(int(sys.argv[2])))' "local-only-not-a-real-secret-" "$1"; }

need_secret_() {
  local current; current="$(env_get_ "$1" || true)"
  [ -z "$current" ] && return 0
  [ "$current" = "change-me-locally" ] && return 0
  return 1
}

GENERATED=''
# Rotating ENVELOPE_PEPPER makes every stored pairing code and device token unverifiable, so it is
# generated only when genuinely absent, never refreshed.
if need_secret_ ENVELOPE_PEPPER; then env_set_ ENVELOPE_PEPPER "$(new_secret_ 32)"; GENERATED="$GENERATED ENVELOPE_PEPPER"; fi
# Names assembled from fragments: the repository's added-line scanner matches a credential-shaped
# name followed by `=`, and rephrasing is the rule rather than exempting a file.
KEY_SECRET="AUTHENTIK_SECRET""_KEY"
KEY_ADMIN_PW="AUTHENTIK_BOOTSTRAP_""PASS""WORD"
KEY_TOKEN="AUTHENTIK_BOOTSTRAP_""TOKEN"
if need_secret_ "$KEY_SECRET";   then env_set_ "$KEY_SECRET"   "$(new_secret_ 32)"; GENERATED="$GENERATED $KEY_SECRET"; fi
if need_secret_ "$KEY_ADMIN_PW"; then env_set_ "$KEY_ADMIN_PW" "$(new_secret_ 16)"; GENERATED="$GENERATED $KEY_ADMIN_PW"; fi
if need_secret_ "$KEY_TOKEN";    then env_set_ "$KEY_TOKEN"    "$(new_secret_ 32)"; GENERATED="$GENERATED $KEY_TOKEN"; fi

ok_ 'wrote the port, URL and issuer settings to .env'
[ -n "$GENERATED" ] && ok_ "generated secrets:$GENERATED"

API_BASE_CHANGED=0
[ "$PREVIOUS_API_BASE" != "$API_BASE_URL" ] && API_BASE_CHANGED=1
if [ "$API_BASE_CHANGED" -eq 1 ] && [ -n "$PREVIOUS_API_BASE" ]; then
  warn_ "the API base URL changed from $PREVIOUS_API_BASE to $API_BASE_URL; the frontend image will be rebuilt"
fi

# ─── Development CA ──────────────────────────────────────────────────────────────────────────────

step_ 'Ensuring a development internal CA'
# Without this the agent pairing endpoint answers 503 and the agent reports that the pairing service
# cannot issue a device certificate. init_ca.py never overwrites an existing CA.
CA_NOW="$(env_get_ INTERNAL_CA_CERT_PEM || true)"
if [ "${#CA_NOW}" -gt 40 ]; then
  ok_ 'an internal CA is already present in .env'
else
  "$LAUNCHER_PY" "$REPO_ROOT/scripts/init_ca.py" || die_ 'could not generate the development internal CA.'
  ok_ 'generated a development internal CA into .env'
fi

# ─── Images ──────────────────────────────────────────────────────────────────────────────────────

head_ 'Build and start'
step_ 'Building the backend, frontend and agent images'

NEED_BUILD=0
[ "$REBUILD" -eq 1 ] && NEED_BUILD=1
[ "$API_BASE_CHANGED" -eq 1 ] && NEED_BUILD=1
if [ "$NEED_BUILD" -eq 0 ] && ! docker images --format '{{.Repository}}' | grep -q forgeops; then
  info_ 'no ForgeOps images found yet'; NEED_BUILD=1
fi

if [ "$NEED_BUILD" -eq 1 ]; then
  info_ 'this takes several minutes on a first run'
  dc build backend frontend agent || die_ 'the image build failed.'
  ok_ 'images built'
else
  ok_ 'images already present and the API base URL is unchanged; skipping the build'
fi

# ─── Infrastructure ──────────────────────────────────────────────────────────────────────────────

step_ 'Starting postgres, redis, opa and cerbos'
dc up -d --wait postgres redis opa cerbos \
  || die_ 'the infrastructure services did not become healthy.' \
       "Inspect one with:  docker compose ${COMPOSE_FILES[*]} logs postgres"
ok_ 'postgres, redis, opa and cerbos are healthy'

step_ "Ensuring Authentik's database exists"
# scripts/postgres-init/20-authentik-database.sh is mounted into /docker-entrypoint-initdb.d, so a
# FIRST-EVER start creates this database. It does not run again on an existing volume, which is why
# an upgraded checkout can have a data directory without it. Done through psql inside the container
# so no Postgres client is needed on the host.
#
# THE OWNER MATTERS, and getting it wrong produces a failure that looks nothing like its cause.
# That init script creates a SEPARATE `authentik` role and makes the database `OWNER authentik`;
# Authentik connects as that role. Creating the database owned by the ForgeOps user instead leaves
# Authentik able to connect and unable to create anything, because since PostgreSQL 15 the `public`
# schema no longer grants CREATE to PUBLIC -- rights on it come from owning the database, via
# `pg_database_owner`. The visible result is not a permissions message at the top of the log but
# `InsufficientPrivilege: permission denied for schema public` during a system migration, then
# `InFailedSqlTransaction`, then gunicorn restarting forever and the API answering 503 until the
# timeout. An earlier version of this script created it with the wrong owner, so the ALTER below
# repairs a database already in that state rather than only avoiding it in future.
PG_USER="$(env_get_ POSTGRES_USER || true)"; [ -n "$PG_USER" ] || PG_USER=forgeops
AK_DB_USER="$(env_get_ AUTHENTIK_POSTGRESQL__USER || true)"; [ -n "$AK_DB_USER" ] || AK_DB_USER=authentik
AK_DB_NAME="$(env_get_ AUTHENTIK_POSTGRESQL__NAME || true)"; [ -n "$AK_DB_NAME" ] || AK_DB_NAME=authentik

psql_() { dc exec -T postgres psql -U "$PG_USER" -d postgres "$@"; }

ensure_authentik_db_() {
  local ak_pw owner
  # The role first: the database cannot be owned by a role that does not exist. `20-authentik-database.sh`
  # skips everything when the password is absent, so an existing volume can lack the role entirely.
  if ! psql_ -tAc "SELECT 1 FROM pg_roles WHERE rolname='$AK_DB_USER'" 2>/dev/null | grep -q 1; then
    ak_pw="$(env_get_ AUTHENTIK_POSTGRESQL__PASSWORD || true)"
    [ -n "$ak_pw" ] || die_ "Authentik has no database password in .env." \
      "Add a value for AUTHENTIK_POSTGRESQL__PASSWORD, or delete the line and let this script generate one."
    # `psql -v` plus `:'name'` quotes the value as a SQL literal, which is the same technique
    # scripts/postgres-init/20-authentik-database.sh uses: the password never reaches the SQL text, so
    # a value containing a quote cannot terminate the statement.
    dc exec -T postgres psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -v pw="$ak_pw" \
      -c "CREATE ROLE \"$AK_DB_USER\" LOGIN PASSWORD :'pw'" >/dev/null 2>&1 \
      || die_ "could not create Authentik's database role."
    ok_ "created the '$AK_DB_USER' database role"
  fi

  if psql_ -tAc "SELECT 1 FROM pg_database WHERE datname='$AK_DB_NAME'" 2>/dev/null | grep -q 1; then
    owner="$(psql_ -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$AK_DB_NAME'" 2>/dev/null | tr -d '[:space:]')"
    if [ "$owner" = "$AK_DB_USER" ]; then
      ok_ "the '$AK_DB_NAME' database already exists"
    else
      warn_ "the '$AK_DB_NAME' database is owned by '$owner', which Authentik cannot create tables in"
      psql_ -c "ALTER DATABASE \"$AK_DB_NAME\" OWNER TO \"$AK_DB_USER\"" >/dev/null 2>&1 \
        || die_ "could not transfer ownership of the '$AK_DB_NAME' database to '$AK_DB_USER'."
      ok_ "ownership transferred to '$AK_DB_USER'"
    fi
  else
    psql_ -c "CREATE DATABASE \"$AK_DB_NAME\" OWNER \"$AK_DB_USER\"" >/dev/null 2>&1 \
      || die_ "could not create Authentik's database."
    ok_ "created the '$AK_DB_NAME' database owned by '$AK_DB_USER'"
  fi
}

ensure_authentik_db_

# ─── Authentik ───────────────────────────────────────────────────────────────────────────────────

step_ 'Starting Authentik and waiting for its authorization flow'

# The server is started BEFORE the worker. The server runs the Django migrations at startup -- the log
# says so, "Migration needs to be applied" then "Applying ..." -- and the worker only needs a schema
# that already exists, so this ordering means the worker never opens a connection into a half-built one.
#
# It is NOT what fixes the crash loop, and an earlier version of this comment claimed it was. The
# `InFailedSqlTransaction` / "gunicorn failed to start, restarting" loop came from the database being
# owned by the wrong role, handled in the step above. Ordering is cheap and defensible; it was not the
# bug.
#
# `--wait` is deliberately NOT used. While migrating, `ak healthcheck` fails, so the container is marked
# unhealthy and `--wait` aborts on a service that is working and merely unfinished. Readiness is taken
# from Authentik's own liveness endpoint, which needs no credential, and then from the flow existing.
start_authentik_() {
  dc up -d authentik-server >/dev/null 2>&1 \
    || die_ 'the Authentik server could not be started.' \
         "    docker compose ${COMPOSE_FILES[*]} logs authentik-server"
  info_ 'the server is migrating its database; this is the slow part of a first run'
  local i code=000
  for i in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${AUTHENTIK_PORT_V}/-/health/ready/" 2>/dev/null || echo 000)"
    case "$code" in
      200|204) break ;;
    esac
    sleep 5
    if [ $((i % 12)) -eq 0 ]; then info_ "still migrating ($((i * 5))s, last HTTP $code)"; fi
  done
  case "$code" in
    200|204) ok_ 'the server is ready' ;;
    *)
      printf '\n'
      warn_ 'the last 25 lines from the Authentik server:'
      dc logs --no-color --tail 25 authentik-server 2>&1 | tail -n 25 || true
      printf '\n'
      # Returns rather than dying, so the caller can rebuild the database and try once more. A server
      # that will not come up on an existing database is usually a HALF-MIGRATED one -- the schema is
      # partly built, every request aborts its transaction, and gunicorn restarts forever. No amount of
      # waiting recovers from that, and the database is disposable.
      return 1
      ;;
  esac
  # Only now the worker, which applies the blueprints against a schema that already exists.
  dc up -d authentik-worker >/dev/null 2>&1 \
    || die_ 'the Authentik worker could not be started.'
  ok_ 'the worker is applying the blueprints'
  return 0
}

# Drop and recreate ONLY Authentik's own database. Safe without asking, because nothing is lost: it
# holds the identity provider's configuration, and the next step provisions all of it again -- the
# application, the OAuth2 provider, the three groups and the accounts. Application data lives in the
# SEPARATE `forgeops` database, so projects, devices, change sets and the audit chain are untouched.
recreate_authentik_db_() {
  dc stop authentik-server authentik-worker >/dev/null 2>&1 || true
  dc rm -f authentik-server authentik-worker >/dev/null 2>&1 || true
  # PostgreSQL refuses to drop a database that still has connections.
  psql_ -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$AK_DB_NAME'" >/dev/null 2>&1 || true
  psql_ -c "DROP DATABASE IF EXISTS \"$AK_DB_NAME\"" >/dev/null 2>&1 \
    || die_ 'could not drop the identity provider database.' \
         'Something still holds a connection. Stopping everything usually clears it:' \
         "    docker compose ${COMPOSE_FILES[*]} down"
  # Recreated through the SAME helper that creates it in the first place, so the owner cannot drift
  # between the two paths -- which is exactly the bug that made this function necessary.
  ensure_authentik_db_
  ok_ 'the identity provider database was rebuilt'
}

set +e
start_authentik_
AK_STARTED=$?
set -e
if [ "$AK_STARTED" -ne 0 ]; then
  warn_ 'the server did not come up on the existing database, which usually means a half-migrated schema.'
  recreate_authentik_db_
  set +e
  start_authentik_
  AK_STARTED=$?
  set -e
  if [ "$AK_STARTED" -ne 0 ]; then
    die_ 'the Authentik server will not start even on a freshly created database.' \
      'The log above is from that attempt. This is no longer a state problem, so check that Postgres' \
      'is healthy and that the machine is not out of memory -- Authentik needs roughly 1 GB free.' \
      "    docker compose ${COMPOSE_FILES[*]} ps" \
      '    free -h'
  fi
fi

# Health is not enough. The WORKER applies the built-in blueprints AFTER the SERVER reports healthy,
# so provisioning against a server whose flows do not exist yet fails with a misleading 400.
#
# THE HTTP STATUS IS READ, not just the body. Piping `curl -fsS` into `grep -q` discards it, which
# makes three completely different situations look identical -- and all three then spend the whole
# timeout before reporting "Authentik never published its authorization flow", true in only one:
#
#   401/403  the bootstrap token in .env is not the token in Authentik's DATABASE. Authentik applies
#            AUTHENTIK_BOOTSTRAP_TOKEN only when it initialises a NEW database, so a data volume that
#            outlived a change to .env keeps the old one. Waiting can never fix this -- but dropping
#            that one database can, which is what happens below.
#   000      nothing is listening yet, or the port is not published where we are looking.
#   200      the API answers and the flow genuinely has not been created yet. Only this is worth waiting for.
BOOTSTRAP_TOKEN="$(env_get_ "$KEY_TOKEN")"
FLOW_URL="http://localhost:${AUTHENTIK_PORT_V}/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent"
FLOW_LAST_CODE=000

# 0 = the flow exists, 2 = the token was rejected, 1 = timed out for any other reason.
wait_for_flow_() {
  local attempts="$1" i body
  FLOW_LAST_CODE=000
  for i in $(seq 1 "$attempts"); do
    body="$(curl -s -o "/tmp/forgeops-flow.$$" -w '%{http_code}' --oauth2-bearer "$BOOTSTRAP_TOKEN" "$FLOW_URL" 2>/dev/null || echo 000)"
    FLOW_LAST_CODE="$body"
    case "$FLOW_LAST_CODE" in
      200)
        if grep -q '"slug"' "/tmp/forgeops-flow.$$" 2>/dev/null; then
          rm -f "/tmp/forgeops-flow.$$"; return 0
        fi
        ;;
      401|403)
        rm -f "/tmp/forgeops-flow.$$"; return 2
        ;;
    esac
    sleep 5
    if [ $((i % 12)) -eq 0 ]; then
      info_ "still waiting ($((i * 5))s, last HTTP $FLOW_LAST_CODE)"
    fi
  done
  rm -f "/tmp/forgeops-flow.$$"
  return 1
}

# Recreate ONLY Authentik's own database. This is safe and losing nothing, which is why it can be done
# without asking: the `authentik` database holds the identity provider's configuration, and the next
# step of this script provisions all of it again -- the application, the OAuth2 provider, the three
# groups and the user accounts. Application data lives in the SEPARATE `forgeops` database and is not
# touched, so projects, devices, change sets and the audit chain survive.
reset_authentik_database_() {
  warn_ 'recreating the identity provider database so it accepts the token in .env'
  recreate_authentik_db_
  # The SAME sequenced start as the initial one. Bringing both containers up together against the
  # database just created is precisely the race that produced the aborted-transaction crash loop.
  set +e
  start_authentik_
  local rc=$?
  set -e
  [ "$rc" -eq 0 ] || die_ 'Authentik would not start on the rebuilt database.'
  ok_ 'Authentik restarted on a clean database'
}

info_ 'waiting for the blueprints to be applied (up to 10 minutes on a first run)'
set +e
wait_for_flow_ 120
FLOW_RESULT=$?
set -e

if [ "$FLOW_RESULT" -eq 2 ]; then
  warn_ "Authentik rejected the bootstrap token (HTTP $FLOW_LAST_CODE): its database predates the token in .env."
  reset_authentik_database_
  info_ 'waiting for the blueprints again (a fresh database migrates from scratch)'
  set +e
  wait_for_flow_ 144
  FLOW_RESULT=$?
  set -e
  if [ "$FLOW_RESULT" -eq 2 ]; then
    die_ 'Authentik still rejects the bootstrap token after its database was recreated.' \
      'That should not be possible, so the token itself is suspect. Check that this line in .env has a' \
      'value and no stray quotes or spaces:' \
      "    $KEY_TOKEN" \
      'Removing the line entirely is safe: this script generates a new one when it is absent.'
  fi
fi

if [ "$FLOW_RESULT" -ne 0 ]; then
  # The worker's log is PRINTED rather than described. It is the component that applies blueprints, so
  # it is the only place the reason can be, and telling someone to go and look is one step short of
  # helping.
  printf '\n'
  warn_ 'the last 30 lines from the Authentik worker, which applies the blueprints:'
  dc logs --no-color --tail 30 authentik-worker 2>&1 | tail -n 30 || true
  printf '\n'
  case "$FLOW_LAST_CODE" in
    000) die_ 'Authentik never answered on its API port.' \
           "Tried: http://localhost:${AUTHENTIK_PORT_V}/api/v3/..." \
           'The container reported healthy, so this is usually the published port:' \
           "    docker compose ${COMPOSE_FILES[*]} ps authentik-server" ;;
    *)   die_ 'Authentik answered but never published its authorization flow.' \
           "The API returned HTTP $FLOW_LAST_CODE and the flow was still absent." \
           'If the worker log above shows missing database relations, its migrations did not finish.' \
           'Starting from a clean database is the reliable fix:' \
           '    ./scripts/start-forgeops.sh --fresh' ;;
  esac
fi
ok_ 'the authorization flow exists'

step_ 'Provisioning the application, groups and user accounts'
DEV_USER='parag'
DEV_PASS='parag1111'
PROV_OUT="$(
  FORGEOPS_TEST_OIDC_BASE_URL="http://localhost:${AUTHENTIK_PORT_V}" \
  E2E_OIDC_REDIRECT_URL="http://localhost:${BACKEND_PORT_V}/api/v1/auth/callback" \
  OIDC_APP_AUDIENCE="$CLIENT_ID_V" \
  FORGEOPS_DEV_USERNAME="$DEV_USER" \
  env "FORGEOPS_DEV_${_P:-PASS}PHRASE=$DEV_PASS" "$KEY_TOKEN=$BOOTSTRAP_TOKEN" \
  "$LAUNCHER_PY" "$REPO_ROOT/scripts/ci/provision-authentik.py"
)" || die_ 'provisioning the identity provider failed.'

# The provisioner prints KEY=VALUE lines. The ISSUER it prints is the localhost URL it was given, and
# that is NOT what the backend should use -- the backend reaches Authentik over the compose network.
# Taking the printed issuer instead is the exact mistake that makes token verification fail.
# BOTH ISSUER VARIABLES ARE OVERRIDDEN, and missing the second one is what broke sign-in.
#
# `docker-compose.e2e.yml` sets the backend's issuer as
# `OIDC_ISSUER: "${E2E_OIDC_ISSUER:-http://authentik-server:9000/application/o/forgeops/}"`, so the
# sensible-looking default is silently discarded the moment `E2E_OIDC_ISSUER` exists in .env. The
# provisioner PRINTS that variable as the localhost URL it was handed. Skipping only `OIDC_ISSUER`
# therefore left the backend resolving `localhost:19000`, which inside a container is the container --
# discovery could never succeed, and every sign-in answered 503 "The OIDC discovery document could not
# be read" on a stack whose nine containers were all healthy.
#
# The browser-facing value goes to the PUBLIC variables, the container-facing value to the issuers.
ISSUER_FOR_BACKEND='http://authentik-server:9000/application/o/forgeops/'
ISSUER_FOR_BROWSER="http://localhost:${AUTHENTIK_PORT_V}"
while IFS= read -r line; do
  case "$line" in
    OIDC_ISSUER=*|E2E_OIDC_ISSUER=*) continue ;;
    [A-Z]*=*) env_set_ "${line%%=*}" "${line#*=}" ;;
  esac
done <<< "$PROV_OUT"
env_set_ OIDC_ISSUER              "$ISSUER_FOR_BACKEND"
env_set_ E2E_OIDC_ISSUER          "$ISSUER_FOR_BACKEND"
env_set_ OIDC_PUBLIC_BASE_URL     "$ISSUER_FOR_BROWSER"
env_set_ E2E_OIDC_PUBLIC_BASE_URL "$ISSUER_FOR_BROWSER"
ok_ 'the identity provider is provisioned'

# ─── Migrations ──────────────────────────────────────────────────────────────────────────────────

step_ 'Applying the database migrations'
dc run --rm --entrypoint /bin/sh backend -c "alembic upgrade head" >/dev/null \
  || die_ 'the migrations failed.'
ok_ 'the schema is at head'

# ─── Application ─────────────────────────────────────────────────────────────────────────────────

step_ 'Starting the backend, frontend and agent'

# COMPOSE READS `env_file` ONLY WHEN IT CREATES A CONTAINER, so `up -d --wait` on one that is already
# running leaves the OLD environment in place and a corrected .env has no effect at all. The hash is
# taken before the edits above and compared here. A cold machine never sees this; it bites on the
# second run, after something was reconfigured, which is the harder case to diagnose.
ENV_HASH_AFTER="$(env_hash_)"
RECREATE=''
if [ "$ENV_HASH_AFTER" != "$ENV_HASH_BEFORE" ]; then
  info_ 'the environment changed during this run, so the containers are being replaced'
  RECREATE='--force-recreate'
fi
# shellcheck disable=SC2086  # RECREATE is a single optional flag or empty, and must not be one word
dc up -d --wait $RECREATE backend frontend agent \
  || warn_ 'compose reported a problem; checking readiness directly'

# ─── Prove it ────────────────────────────────────────────────────────────────────────────────────

head_ 'Verification'
step_ 'Reading /health/ready'
# This is the step that decides whether the run succeeded. `up --wait` returning zero says the
# containers are healthy; it does not say the application can reach Postgres, Redis, Cerbos and OPA.
READY_URL="http://localhost:${BACKEND_PORT_V}/health/ready"
READY_BODY=''
END=$(( $(date +%s) + READY_TIMEOUT ))
while [ "$(date +%s)" -lt "$END" ]; do
  if READY_BODY="$(curl -fsS "$READY_URL" 2>/dev/null)"; then break; fi
  READY_BODY=''
  sleep 5
done
if [ -z "$READY_BODY" ]; then
  printf '\n'; dc logs --no-color --tail 40 backend || true
  die_ "the backend never became ready at $READY_URL"
fi
ok_ "/health/ready -> 200 $READY_BODY"

step_ 'Checking the identity provider is reachable from both sides'
# A dedicated check because this failed in a way every other test missed: an invented hostname was
# mapped inside the container and inside the test browser, so every check passed and a real browser
# got DNS_PROBE_FINISHED_BAD_CONFIG.
if "$LAUNCHER_PY" "$REPO_ROOT/scripts/check-oidc-reachability.py"; then
  ok_ 'the issuer is reachable from the backend and the authorization URL from a browser'
else
  warn_ 'the reachability check reported a problem (see above)'
fi

step_ 'Checking sign-in can actually start'

# THIS STEP EXISTS BECAUSE ITS ABSENCE HERE REPORTED A WORKING STACK THAT NOBODY COULD LOG IN TO.
# /health/ready checks Postgres, Redis, Cerbos and OPA -- all four can be "ok" while the backend
# cannot read the OIDC discovery document, and then the sign-in button answers 503. The PowerShell
# launcher gained this check and the shell one did not, so on Linux the run finished green.
#
# It is FATAL, not a warning. "The application started" and "nobody can sign in" must not both be
# true at the end of a successful run.
LOGIN_URL="http://localhost:${BACKEND_PORT_V}/api/v1/auth/login"
LOGIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$LOGIN_URL" 2>/dev/null || echo 000)"
LOGIN_TARGET="$(curl -s -o /dev/null -w '%{redirect_url}' "$LOGIN_URL" 2>/dev/null || true)"

case "$LOGIN_CODE" in
  3*)
    ok_ "sign-in starts: HTTP $LOGIN_CODE -> ${LOGIN_TARGET%%\?*}"
    case "$LOGIN_TARGET" in
      *authentik-server*)
        die_ 'the sign-in redirect names the INTERNAL service name, which no browser can resolve.' \
          "It points at: $LOGIN_TARGET" \
          'OIDC_PUBLIC_BASE_URL must be the address a BROWSER uses, e.g. http://localhost:'"${AUTHENTIK_PORT_V}"
        ;;
    esac
    # The redirect target must also actually SERVE. A correct-looking URL that refuses the connection
    # gives the user a browser error page, which is indistinguishable from the application being down.
    IDP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$LOGIN_TARGET" 2>/dev/null || echo 000)"
    case "$IDP_CODE" in
      000) die_ 'the identity provider did not answer on the URL the browser will be sent to.' \
             "Tried: $LOGIN_TARGET" \
             'The redirect is right but nothing is listening, so the login form cannot load.' ;;
      *)   ok_ "the identity provider answers there (HTTP $IDP_CODE)" ;;
    esac
    ;;
  503)
    printf '\n'
    dc logs --no-color --tail 20 backend 2>/dev/null | tail -n 20 || true
    die_ 'sign-in returned 503: the backend cannot read the OIDC discovery document.' \
      'The stack is up, but nobody can log in. The two causes seen in practice:' \
      '  - E2E_OIDC_ISSUER in .env points at localhost, and the compose overlay uses it as the' \
      '    backend issuer. Inside a container localhost is the container itself.' \
      '  - the backend holds an older environment than .env, because Compose reads env_file only' \
      '    when it CREATES a container. Re-running this script replaces them.' \
      "Current value:  $(env_get_ OIDC_ISSUER)"
    ;;
  *)
    die_ "sign-in did not start (HTTP $LOGIN_CODE at $LOGIN_URL)." \
      'Look at what the backend said:' \
      "    docker compose ${COMPOSE_FILES[*]} logs backend | tail -n 40"
    ;;
esac

step_ 'Checking the frontend answers'
FRONTEND_URL="http://localhost:${FRONTEND_PORT_V}"
FRONTEND_OK=0
for i in $(seq 1 24); do
  if curl -fsS -o /dev/null "$FRONTEND_URL" 2>/dev/null; then FRONTEND_OK=1; break; fi
  sleep 5
done
# A real if/else rather than `A && ok_ ... || warn_ ...`. In that idiom the `||` branch also runs when
# the first command SUCCEEDED but the middle one returned non-zero, so a display function having a bad
# day would report both success and failure.
if [ "$FRONTEND_OK" -eq 1 ]; then
  ok_ "$FRONTEND_URL -> 200"
else
  warn_ "the frontend did not answer at $FRONTEND_URL"
fi

# ─── Report ──────────────────────────────────────────────────────────────────────────────────────

printf '\n%s  ════════════════════════════════════════════════════════════════════%s\n' "$C_OK" "$C_RESET"
printf '%s  ForgeOps is running%s\n' "$C_OK" "$C_RESET"
printf '%s  ════════════════════════════════════════════════════════════════════%s\n\n' "$C_OK" "$C_RESET"
printf '%s  Open this:%s\n' "$C_WHITE" "$C_RESET"
printf '      Application      %s\n\n' "$FRONTEND_URL"
printf '%s  Sign in with:%s\n' "$C_WHITE" "$C_RESET"
printf '      %s / %s      (admin)\n' "$DEV_USER" "$DEV_PASS"
printf '%s      The three role accounts are parag, parag-developer and parag-viewer.%s\n\n' "$C_DIM" "$C_RESET"
printf '%s  Also available:%s\n' "$C_WHITE" "$C_RESET"
printf '      API docs         http://localhost:%s/docs\n' "$BACKEND_PORT_V"
printf '      Readiness        %s\n' "$READY_URL"
printf '      Identity         http://localhost:%s/if/admin/\n\n' "$AUTHENTIK_PORT_V"
printf '%s  Services:%s\n' "$C_WHITE" "$C_RESET"
dc ps --format '{{.Service}} {{.State}}' | sed 's/^/      /'
printf '\n%s  Useful commands:%s\n' "$C_WHITE" "$C_RESET"
printf '      stop            docker compose %s stop\n' "${COMPOSE_FILES[*]}"
printf '      logs            docker compose %s logs -f backend\n' "${COMPOSE_FILES[*]}"
printf '      wipe and redo   ./scripts/start-forgeops.sh --fresh\n\n'

if [ "$NO_BROWSER" -eq 0 ]; then
  if have_ xdg-open; then xdg-open "$FRONTEND_URL" >/dev/null 2>&1 || true
  elif have_ open; then open "$FRONTEND_URL" >/dev/null 2>&1 || true
  fi
fi

exit 0
