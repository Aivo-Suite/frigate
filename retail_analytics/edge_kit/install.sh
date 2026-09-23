#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo 'Run this installer as root on the store Edge host.'; exit 1; fi
if [ "$(pwd -P)" != /opt/aivo-edge ]; then echo 'Extract the kit into /opt/aivo-edge and run it there.'; exit 1; fi
command -v docker >/dev/null
docker compose version >/dev/null
command -v python3 >/dev/null
command -v systemctl >/dev/null
umask 077
mkdir -p .secrets media state
if [ ! -s .secrets/tenant-api-key ]; then
  read -r -s -p 'Tenant API key: ' edge_tenant_key
  printf '\n'
  if [ -z "$edge_tenant_key" ]; then exit 1; fi
  printf '%s' "$edge_tenant_key" > .secrets/tenant-api-key
  unset edge_tenant_key
fi
python3 -m venv .venv
.venv/bin/pip install -r requirements.intelbras.txt
cat > /etc/systemd/system/aivo-intelbras-provisioner.service <<'SERVICE'
[Unit]
Description=Aivo Intelbras camera configuration
After=docker.service network-online.target
Requires=docker.service
[Service]
WorkingDirectory=/opt/aivo-edge
Environment=CLOUD_API_URL=https://frigate.agenticx.ia.br
Environment=TENANT_API_KEY_FILE=/opt/aivo-edge/.secrets/tenant-api-key
Environment=FRIGATE_CONFIG_PATH=/opt/aivo-edge/config/config.yml
Environment=FRIGATE_CONTAINER=aivo-intelbras-frigate
Environment=CAMERA_POLL_SECONDS=30
ExecStart=/opt/aivo-edge/.venv/bin/python /opt/aivo-edge/edge_provisioner.py
Restart=on-failure
RestartSec=10
UMask=0077
[Install]
WantedBy=multi-user.target
SERVICE
docker compose up -d --build
systemctl daemon-reload
systemctl enable --now aivo-intelbras-provisioner.service
echo 'Edge installed. Configure the Intelbras and its zones in the Aivo dashboard.'
