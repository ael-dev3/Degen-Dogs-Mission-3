#!/usr/bin/env bash
set -Eeuo pipefail

units=(
  degen-dogs-runner.target
  degen-dogs-watcher.timer
  degen-dogs-hourly.timer
  degen-dogs-health.timer
)

state_dir=/var/lib/degen-dogs
runtime_dir=/run/degen-dogs
armed_marker="${state_dir}/activation-armed"
active_marker="${runtime_dir}/activation-enabled"
ready_marker="${runtime_dir}/anchor-ready"

cleanup() {
  rm -f -- "$ready_marker" "$active_marker"
}

write_marker() {
  local target="$1"
  local temporary
  temporary="$(mktemp "${runtime_dir}/.marker.XXXXXX")"
  printf 'pid=%s\n' "$$" >"$temporary"
  install -o root -g root -m 0644 "$temporary" "$target"
  rm -f -- "$temporary"
}

start_units() {
  local unit
  if [[ ! -f "$armed_marker" || -L "$armed_marker" || \
    "$(stat -c %U "$armed_marker")" != "root" || \
    "$(stat -c %h "$armed_marker")" != "1" || \
    "$(stat -c %a "$armed_marker")" != "644" ]]; then
    rm -f -- "$active_marker"
    return 0
  fi
  for unit in "${units[@]}"; do
    systemctl is-enabled --quiet "$unit"
  done
  for unit in "${units[@]}"; do
    if ! systemctl is-active --quiet "$unit"; then
      systemctl start "$unit"
    fi
  done
  for unit in "${units[@]}"; do
    systemctl is-active --quiet "$unit"
  done
  write_marker "$active_marker"
}

anchor_main() {
  # A Windows Task Scheduler job keeps this process attached to WSL. systemd
  # supervises the actual one-shot jobs; this root-only anchor starts missing
  # timers after a WSL restart and keeps the distro alive between activations.
  [[ "$(id -u)" == "0" ]] || {
    printf 'error: the WSL runner anchor must run as root\n' >&2
    exit 77
  }

  install -d -o root -g root -m 0755 "$state_dir" "$runtime_dir"
  trap cleanup EXIT
  trap 'exit 1' HUP INT TERM
  write_marker "$ready_marker"
  start_units

  while sleep 60; do
    start_units
  done
}

if [[ "${BASH_SOURCE[0]:-}" == "$0" ]]; then
  anchor_main
fi
