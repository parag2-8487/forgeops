#!/usr/bin/env bash
#
# Shared apt preflight for setup.sh and scripts/start-forgeops.sh.
#
# Both scripts install system packages with apt-get, and both used to swallow every failure with
# `|| true`. On a fresh Ubuntu desktop that combination is actively misleading: the machine boots
# into `unattended-upgrades`, which holds the dpkg lock while it applies security updates, so the
# install fails, the failure is discarded, and the script dies much later with a message about
# Docker or Python being unavailable. The real cause -- a busy package lock -- has already scrolled
# past by then.
#
# Sourced rather than duplicated because two copies of a preflight is how the two scripts came to
# disagree about argument handling in the first place.

# Tell apt to WAIT for a busy package lock rather than failing on it.
#
# apt has understood `DPkg::Lock::Timeout` since 1.9.11, which every supported Ubuntu release
# (20.04 and later) ships. Setting it once in apt.conf.d covers EVERY apt invocation in both scripts
# -- including `add-apt-repository` and any call added later -- which a per-call flag would not.
#
# The version check is load-bearing. An apt older than 1.9.11 does not know the option, and a config
# file it cannot parse breaks EVERY apt call, which is a worse failure than the one being fixed. So
# the check runs first and nothing is written when it cannot be certain.
#
# Usage: apt_configure_lock_timeout <sudo-prefix>
#   <sudo-prefix> is "sudo", or "" when the script is already running as root.
apt_configure_lock_timeout() {
  local sudo_prefix="${1:-}"

  command -v apt-get >/dev/null 2>&1 || return 0
  command -v apt-config >/dev/null 2>&1 || return 0

  local apt_version
  apt_version="$(apt-get --version 2>/dev/null | head -n 1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1)"
  [ -n "$apt_version" ] || return 0

  # `sort -V` puts the lower version first; if 1.9.11 sorts first, the installed apt is at least it.
  [ "$(printf '%s\n' "1.9.11" "$apt_version" | sort -V | head -n 1)" = "1.9.11" ] || return 0

  local conf=/etc/apt/apt.conf.d/99forgeops-lock-timeout
  printf 'DPkg::Lock::Timeout "300";\n' | ${sudo_prefix} tee "$conf" >/dev/null 2>&1 || return 0
  printf '      ok    apt waits up to 300s for a busy package lock (unattended-upgrades holds it on a fresh boot)\n'
}

# Install packages with apt, reporting failure instead of discarding it.
#
# Usage: apt_install_quiet <sudo-prefix> <package...>
#   Returns non-zero when the install did not succeed, so the caller can say something useful.
#   Callers treat a failure as a warning rather than fatal: each package is re-checked with
#   `command -v` before it is actually used, which is a better test of "do we have it" than apt's
#   exit code.
apt_install_quiet() {
  local sudo_prefix="${1:-}"; shift
  [ "$#" -gt 0 ] || return 0

  ${sudo_prefix} apt-get install -y -qq "$@" >/dev/null 2>&1
}
