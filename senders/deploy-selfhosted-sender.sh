#!/bin/bash
set -euo pipefail

SELFHOSTED_CONFIG_SOURCE="${1:-/tmp/selfhosted.alloy}"
SYSTEMD_OVERRIDE_SOURCE="${2:-/tmp/alloy-selfhosted.conf}"

IFS= read -r ingest_username
IFS= read -r ingest_password
[[ -n "$ingest_username" && -n "$ingest_password" ]]
[[ -r "$SELFHOSTED_CONFIG_SOURCE" && -r "$SYSTEMD_OVERRIDE_SOURCE" ]]

config_target=/etc/alloy/config.alloy
restic_target=$(sudo readlink -f /etc/alloy/restic.alloy 2>/dev/null || true)
caddy_target=$(sudo readlink -f /etc/alloy/caddy.alloy 2>/dev/null || true)
sudo test -f "$config_target"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/var/backups/alloy-selfhosted/$timestamp"
sudo install -d -o root -g root -m 0700 "$backup_dir"
sudo cp -a "$config_target" "$backup_dir/config.alloy"
if [[ -n "$restic_target" ]] && sudo test -f "$restic_target"; then
  sudo cp -a "$restic_target" "$backup_dir/restic.alloy"
fi
if [[ -n "$caddy_target" ]] && sudo test -f "$caddy_target"; then
  sudo cp -a "$caddy_target" "$backup_dir/caddy.alloy"
fi

restore_previous_config() {
  sudo cp -a "$backup_dir/config.alloy" "$config_target"
  if sudo test -f "$backup_dir/restic.alloy"; then
    sudo cp -a "$backup_dir/restic.alloy" "$restic_target"
  fi
  if sudo test -f "$backup_dir/caddy.alloy"; then
    sudo cp -a "$backup_dir/caddy.alloy" "$caddy_target"
  fi
  sudo rm -f /etc/alloy/selfhosted.alloy /etc/alloy/selfhosted.env \
    /etc/systemd/system/alloy.service.d/selfhosted.conf
  sudo systemctl daemon-reload
  sudo systemctl restart alloy
}

targets=("$config_target")
if [[ -n "$restic_target" ]] && sudo test -f "$restic_target"; then
  targets+=("$restic_target")
fi
if [[ -n "$caddy_target" ]] && sudo test -f "$caddy_target"; then
  targets+=("$caddy_target")
fi

prometheus_refs=$(sudo grep -hFc 'prometheus.remote_write.metrics_service.receiver' "${targets[@]}" \
  | awk '{sum += $1} END {print sum + 0}')
loki_refs=$(sudo grep -hFc 'loki.write.grafana_cloud_loki.receiver' "${targets[@]}" \
  | awk '{sum += $1} END {print sum + 0}')
[[ "$prometheus_refs" -gt 0 && "$loki_refs" -gt 0 ]]

for target in "${targets[@]}"; do
  sudo sed -i \
    -e 's/\[prometheus\.remote_write\.metrics_service\.receiver\]/[prometheus.remote_write.metrics_service.receiver, prometheus.remote_write.selfhosted.receiver]/g' \
    -e 's/\[loki\.write\.grafana_cloud_loki\.receiver\]/[loki.write.grafana_cloud_loki.receiver, loki.write.selfhosted.receiver]/g' \
    "$target"
done

sudo install -o root -g root -m 0644 "$SELFHOSTED_CONFIG_SOURCE" /etc/alloy/selfhosted.alloy
secret_tmp=$(mktemp)
chmod 0600 "$secret_tmp"
printf 'SELFHOSTED_GRAFANA_USERNAME=%s\n' "$ingest_username" > "$secret_tmp"
printf 'SELFHOSTED_GRAFANA_PASSWORD=%s\n' "$ingest_password" >> "$secret_tmp"
sudo install -o root -g root -m 0600 "$secret_tmp" /etc/alloy/selfhosted.env
rm -f "$secret_tmp"

sudo install -d -o root -g root -m 0755 /etc/systemd/system/alloy.service.d
sudo install -o root -g root -m 0644 "$SYSTEMD_OVERRIDE_SOURCE" \
  /etc/systemd/system/alloy.service.d/selfhosted.conf

if ! sudo env \
    SELFHOSTED_GRAFANA_USERNAME="$ingest_username" \
    SELFHOSTED_GRAFANA_PASSWORD="$ingest_password" \
    alloy validate /etc/alloy/; then
  restore_previous_config
  echo "Alloy validation failed; previous configuration restored." >&2
  exit 1
fi

sudo systemctl daemon-reload
if ! sudo systemctl restart alloy || ! sudo systemctl is-active --quiet alloy; then
  restore_previous_config
  echo "Alloy restart failed; previous configuration restored." >&2
  exit 1
fi

new_prometheus_refs=$(sudo grep -RhFc 'prometheus.remote_write.selfhosted.receiver' \
  /etc/alloy --include='*.alloy' | awk -F: '{sum += $NF} END {print sum + 0}')
new_loki_refs=$(sudo grep -RhFc 'loki.write.selfhosted.receiver' \
  /etc/alloy --include='*.alloy' | awk -F: '{sum += $NF} END {print sum + 0}')
[[ "$new_prometheus_refs" -eq "$prometheus_refs" ]]
[[ "$new_loki_refs" -eq "$loki_refs" ]]

printf 'alloy=active prometheus_refs=%s loki_refs=%s backup=%s\n' \
  "$new_prometheus_refs" "$new_loki_refs" "$backup_dir"
