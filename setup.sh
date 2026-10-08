#!/usr/bin/env bash
#
# ForgeOps Master Setup & Installation Script for Linux (Ubuntu / Debian)
#
# Designed for fresh machines (e.g., college lab PCs) that:
#   - Do NOT have Docker or Docker Compose installed
#   - May have older system packages (Python 3.8/3.10, outdated pip, missing build tools)
#   - May be running in standard terminal, SSH, or WSL2 environments
#   - Require a single command to configure prerequisites, build, start, and verify the stack
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh                  # Start setup and launch ForgeOps
#   ./setup.sh --fresh          # Start setup with clean database volumes
#

set -Eeuo pipefail

# ─── Terminal Presentation ───────────────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  C_HEAD=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
  C_DIM=$'\033[90m'; C_RESET=$'\033[0m'; C_WHITE=$'\033[1;97m'
else
  C_HEAD=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_RESET=''; C_WHITE=''
fi

msg_head() { printf '\n%s=== %s ===%s\n' "$C_HEAD" "$1" "$C_RESET"; }
msg_step() { printf '%s[*] %s%s\n' "$C_WHITE" "$1" "$C_RESET"; }
msg_ok()   { printf '%s    [OK] %s%s\n' "$C_OK" "$1" "$C_RESET"; }
msg_info() { printf '%s    [..] %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
msg_warn() { printf '%s    [WARN] %s%s\n' "$C_WARN" "$1" "$C_RESET"; }

die() {
  printf '\n%s----------------------------------------------------------------------%s\n' "$C_ERR" "$C_RESET"
  printf '%sSETUP FAILED: %s%s\n' "$C_ERR" "$1" "$C_RESET"
  printf '%s----------------------------------------------------------------------%s\n' "$C_ERR" "$C_RESET"
  shift || true
  for line in "$@"; do printf '%s  %s%s\n' "$C_WARN" "$line" "$C_RESET"; done
  printf '\n'
  exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# ─── Root & User Resolution ───────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
cd "$REPO_ROOT"

# Shared apt preflight. Sourced rather than duplicated so this script and the launcher cannot drift
# apart on how they configure and report package installs.
if [ -f "$REPO_ROOT/scripts/lib/apt-preflight.sh" ]; then
  # shellcheck source=scripts/lib/apt-preflight.sh
  . "$REPO_ROOT/scripts/lib/apt-preflight.sh"
fi

ACTUAL_USER="${SUDO_USER:-$(id -un)}"
ACTUAL_HOME="$(getent passwd "$ACTUAL_USER" 2>/dev/null | cut -d: -f6 || echo "$HOME")"

SUDO_CMD=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then
    SUDO_CMD="sudo"
  else
    die "This script requires superuser privileges to install system packages and configure Docker." \
      "Please run as root or install sudo."
  fi
fi

msg_head "ForgeOps Master Installer - Linux / Ubuntu"
msg_info "Working directory: $REPO_ROOT"
msg_info "Target user: $ACTUAL_USER"

for marker in docker-compose.yml backend frontend .env.example scripts/start-forgeops.sh; do
  [ -e "$marker" ] || die "Missing repository marker: '$marker'" "Run setup.sh from the ForgeOps repository root."
done

# Ensure execution bits on repository scripts
chmod +x scripts/*.sh 2>/dev/null || true

# ─── System Preflight & Apt Update ───────────────────────────────────────────────────────────────

msg_step "Verifying system package manager and core tools"

# Make apt WAIT for the package lock instead of failing on it. A fresh Ubuntu desktop boots into
# `unattended-upgrades`, which holds the dpkg lock while it applies security updates; without this,
# every install below fails silently and setup dies later blaming Docker. The rationale and the
# version guard live in the helper, shared with the launcher.
apt_configure_lock_timeout "$SUDO_CMD"

if have apt-get; then
  msg_info "Updating apt package cache..."
  if ! $SUDO_CMD apt-get update -qq; then
    msg_warn "apt-get update did not succeed; continuing with the existing package lists"
  fi

  msg_info "Installing core utility packages..."
  if ! apt_install_quiet "$SUDO_CMD" \
    ca-certificates curl gnupg lsb-release software-properties-common \
    git jq netcat-openbsd build-essential libssl-dev libffi-dev; then
    msg_warn "some core packages could not be installed; each is checked again before it is used"
  fi
  msg_ok "Core utilities installed"
elif have dnf; then
  msg_info "Installing core packages with dnf..."
  $SUDO_CMD dnf install -y -q curl git jq nc gcc openssl-devel libffi-devel >/dev/null 2>&1 || true
elif have pacman; then
  msg_info "Installing core packages with pacman..."
  $SUDO_CMD pacman -Sy --noconfirm curl git jq openbsd-netcat base-devel >/dev/null 2>&1 || true
else
  msg_warn "No recognized apt, dnf, or pacman package manager. Continuing with existing tools."
fi

# ─── Docker Engine & Compose V2 Installation ─────────────────────────────────────────────────────

msg_step "Checking Docker and Docker Compose"

install_docker_official() {
  if have apt-get; then
    msg_info "Setting up Docker official repository keyrings..."
    $SUDO_CMD install -m 0755 -d /etc/apt/keyrings

    local os_distro="ubuntu"
    if [ -f /etc/os-release ]; then
      . /etc/os-release
      case "${ID:-ubuntu}" in
        debian|raspbian) os_distro="debian" ;;
        *) os_distro="ubuntu" ;;
      esac
    fi

    local codename="${VERSION_CODENAME:-}"
    if [ -z "$codename" ] && have lsb_release; then
      codename="$(lsb_release -cs 2>/dev/null || echo '')"
    fi
    [ -n "$codename" ] || codename="jammy"

    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
      $SUDO_CMD curl -fsSL "https://download.docker.com/linux/${os_distro}/gpg" -o /etc/apt/keyrings/docker.asc 2>/dev/null || true
      $SUDO_CMD chmod a+r /etc/apt/keyrings/docker.asc 2>/dev/null || true
    fi

    if [ -f /etc/apt/keyrings/docker.asc ]; then
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${os_distro} ${codename} stable" | \
        $SUDO_CMD tee /etc/apt/sources.list.d/docker.list > /dev/null
      $SUDO_CMD apt-get update -qq || true
      $SUDO_CMD apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 || true
    fi

    if ! have docker; then
      msg_warn "Official Docker repository install failed; falling back to distribution docker.io"
      $SUDO_CMD apt-get install -y -qq docker.io docker-compose >/dev/null 2>&1 || true
    fi
  fi
}

if ! have docker; then
  msg_info "Docker is not installed. Installing Docker Engine..."
  install_docker_official
  have docker || die "Could not install Docker. Check internet connection and apt configuration."
  msg_ok "Docker installed successfully"
else
  msg_ok "Docker is already installed: $(docker --version)"
fi

# Ensure Docker daemon is running
msg_info "Ensuring Docker daemon service is started..."
if have systemctl && { systemctl is-system-running >/dev/null 2>&1 || [ -d /run/systemd/system ]; }; then
  $SUDO_CMD systemctl enable --now docker >/dev/null 2>&1 || true
  $SUDO_CMD systemctl start docker >/dev/null 2>&1 || true
elif have service; then
  $SUDO_CMD service docker start >/dev/null 2>&1 || true
fi

# Handle Docker group and socket permissions without requiring a session logout
$SUDO_CMD usermod -aG docker "$ACTUAL_USER" 2>/dev/null || true
if [ -S /var/run/docker.sock ]; then
  $SUDO_CMD chmod 666 /var/run/docker.sock 2>/dev/null || true
fi
if [ -S /run/docker.sock ]; then
  $SUDO_CMD chmod 666 /run/docker.sock 2>/dev/null || true
fi

# Verify Docker engine response
docker_ready=0
for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    docker_ready=1
    break
  fi
  if [ -S /var/run/docker.sock ]; then
    $SUDO_CMD chmod 666 /var/run/docker.sock 2>/dev/null || true
  fi
  sleep 2
done

[ "$docker_ready" -eq 1 ] || die "Docker daemon did not answer after starting." \
  "Check daemon status with: sudo systemctl status docker or sudo journalctl -u docker -n 20"
msg_ok "Docker engine is running and responding"

# Verify Docker Compose V2
if ! docker compose version --short >/dev/null 2>&1; then
  msg_info "Docker Compose v2 plugin is missing. Installing Compose v2..."
  if have apt-get; then
    $SUDO_CMD apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
  fi
  if ! docker compose version --short >/dev/null 2>&1 && have docker-compose; then
    mkdir -p "$ACTUAL_HOME/.docker/cli-plugins"
    ln -sf "$(command -v docker-compose)" "$ACTUAL_HOME/.docker/cli-plugins/docker-compose" 2>/dev/null || true
  fi
  if ! docker compose version --short >/dev/null 2>&1; then
    arch="$(uname -m)"
    case "$arch" in
      x86_64) arch="x86_64" ;;
      aarch64|arm64) arch="aarch64" ;;
      *) arch="x86_64" ;;
    esac
    plugin_dir="/usr/local/lib/docker/cli-plugins"
    $SUDO_CMD mkdir -p "$plugin_dir" 2>/dev/null || true
    $SUDO_CMD curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" -o "$plugin_dir/docker-compose" 2>/dev/null || true
    $SUDO_CMD chmod +x "$plugin_dir/docker-compose" 2>/dev/null || true
  fi
fi

docker compose version --short >/dev/null 2>&1 || die "Docker Compose v2 is required but could not be configured."
msg_ok "Docker Compose v$(docker compose version --short) is ready"

# ─── Python Environment Setup ────────────────────────────────────────────────────────────────────

msg_step "Checking Python interpreter and prerequisites"

ensure_python() {
  # Prefer Python 3.13
  if have python3.13; then
    msg_ok "Python 3.13 is available: $(python3.13 --version)"
    return 0
  fi

  msg_info "Python 3.13 not found on host. Attempting installation via package manager..."
  if have apt-get; then
    $SUDO_CMD apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
    $SUDO_CMD add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || msg_warn "deadsnakes PPA could not be added"
    $SUDO_CMD apt-get update -qq >/dev/null 2>&1 || true
    $SUDO_CMD apt-get install -y -qq python3.13 python3.13-venv python3.13-dev >/dev/null 2>&1 || true
  fi

  if have python3.13; then
    msg_ok "Installed Python 3.13: $(python3.13 --version)"
    return 0
  fi

  # Fallback to host python3 if >= 3.10
  if have python3 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    msg_warn "Using system Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])') as fallback for launcher scripts"
    if have apt-get; then
      $SUDO_CMD apt-get install -y -qq python3-venv python3-dev python3-pip >/dev/null 2>&1 || true
    fi
    return 0
  fi

  die "Python >= 3.10 is required on the host for launcher bootstrapping." \
    "Install Python: sudo apt-get install -y python3 python3-venv"
}

ensure_python

# ─── Resource Precaution (Swap for low-memory College PCs) ────────────────────────────────────────

msg_step "Checking system memory and swap"
mem_avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 4000000)
swap_total_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)

# If available RAM is less than 2.5 GB and swap is 0, setup a safe 2GB swapfile
if [ "$mem_avail_kb" -lt 2500000 ] && [ "$swap_total_kb" -eq 0 ]; then
  msg_warn "Host has low available RAM (< 2.5GB) and no swap configured."
  msg_info "Creating a temporary 2GB swap file to prevent OOM errors during container build..."
  if [ -n "$SUDO_CMD" ] || [ "$(id -u)" -eq 0 ]; then
    $SUDO_CMD fallocate -l 2G /swapfile 2>/dev/null || $SUDO_CMD dd if=/dev/zero of=/swapfile bs=1M count=2048 2>/dev/null || true
    if [ -f /swapfile ]; then
      $SUDO_CMD chmod 600 /swapfile
      $SUDO_CMD mkswap /swapfile >/dev/null 2>&1 || true
      $SUDO_CMD swapon /swapfile >/dev/null 2>&1 || true
      msg_ok "Configured 2GB swap file at /swapfile"
    fi
  fi
else
  msg_ok "System memory is sufficient"
fi

# ─── Launch ForgeOps Stack ────────────────────────────────────────────────────────────────────────

msg_step "Launching ForgeOps stack via scripts/start-forgeops.sh"

# Fix directory ownership if invoked under sudo
if [ "$(id -u)" -eq 0 ] && [ "$ACTUAL_USER" != "root" ]; then
  chown -R "$ACTUAL_USER:$ACTUAL_USER" "$REPO_ROOT" 2>/dev/null || true
fi

# Forward script arguments (e.g. --fresh) to start-forgeops.sh
#
# `${ARR[@]:-}` is NOT the way to do this, and using it here broke the plain `./setup.sh` that the
# README tells you to run. With no arguments, `${ARR[@]:-}` expands to a single EMPTY string rather
# than to nothing, and the launcher's parser reads that as an unknown option and exits 2 -- so the
# default invocation failed while `./setup.sh --fresh` worked, which is why it survived unnoticed.
# `${ARR[@]+"${ARR[@]}"}` expands to the elements when the array is non-empty and to NOTHING when it
# is empty, which is the behaviour every call site below actually wants.
LAUNCHER_ARGS=("$@")

if [ "$(id -u)" -eq 0 ] && [ "$ACTUAL_USER" != "root" ]; then
  # Run launcher as the actual non-root user so files (.env, venvs) retain non-root ownership.
  # Each argument is %q-quoted because this is assembled into a string that `su -c` re-parses: an
  # unquoted value containing a space would split into two arguments on the far side.
  LAUNCH_CMD='./scripts/start-forgeops.sh'
  for arg in ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}; do
    LAUNCH_CMD="$LAUNCH_CMD $(printf '%q' "$arg")"
  done
  msg_info "Executing launcher as user: $ACTUAL_USER"
  su - "$ACTUAL_USER" -c "cd '$REPO_ROOT' && $LAUNCH_CMD"
else
  ./scripts/start-forgeops.sh ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}
fi

msg_head "Setup and Verification Complete"
msg_ok "ForgeOps is successfully running and ready for use!"
