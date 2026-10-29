#!/usr/bin/env bash
# Installer for Debian/Ubuntu with systemd. Never prints the Hetzner token.
set -Eeuo pipefail
umask 077

REPO_RAW="https://raw.githubusercontent.com/Cmd81/hetzner-auto-provisioner/main"
INSTALL_DIR="/opt/hetzner-auto-provisioner"
ENV_FILE="/etc/hetzner-auto-provisioner.env"
SERVICE="hetzner-auto-provisioner"
SERVICE_USER="hetzner-auto"

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run as root: sudo bash install.sh" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required. Install it first (apt install python3)." >&2
  exit 1
fi

# For local source checkout: install adjacent files. For one-liner: fetch from GitHub.
SOURCE_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
install -d -m 0755 "$INSTALL_DIR"
if [[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/monitor.py" && -f "$SOURCE_DIR/.env.example" ]]; then
  install -m 0644 "$SOURCE_DIR/monitor.py" "$INSTALL_DIR/monitor.py"
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0600 "$SOURCE_DIR/.env.example" "$ENV_FILE"
  fi
else
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for remote installation" >&2
    exit 1
  fi
  curl -fsSL --retry 3 "$REPO_RAW/monitor.py" -o "$INSTALL_DIR/monitor.py"
  chmod 0644 "$INSTALL_DIR/monitor.py"
  if [[ ! -f "$ENV_FILE" ]]; then
    curl -fsSL --retry 3 "$REPO_RAW/.env.example" -o "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
  fi
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --home-dir /var/lib/hetzner-auto-provisioner --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 /var/lib/hetzner-auto-provisioner
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

if grep -q '^HETZNER_API_TOKEN=YOUR_HETZNER_TOKEN$' "$ENV_FILE"; then
  if [[ -t 0 ]]; then
    echo "Hetzner Cloud API: use a Read & Write token (do NOT paste it in chat)."
    read -r -s -p "Enter Hetzner API token (blank to configure later): " api_token
    printf '\n'
    if [[ -n "$api_token" ]]; then
      if [[ ! "$api_token" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        echo "Invalid token characters; please edit $ENV_FILE manually" >&2
        exit 1
      fi
      sed -i "s/^HETZNER_API_TOKEN=.*/HETZNER_API_TOKEN=${api_token}/" "$ENV_FILE"
    fi
    unset api_token
  fi
fi

cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Hetzner Cloud capacity watcher and auto provisioner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/monitor.py
Restart=on-failure
RestartSec=30
StateDirectory=hetzner-auto-provisioner
StateDirectoryMode=0700
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
LockPersonality=true
RestrictSUIDSGID=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE}.service" >/dev/null

if grep -q '^HETZNER_API_TOKEN=YOUR_HETZNER_TOKEN$' "$ENV_FILE"; then
  echo "Installed but not started: configure $ENV_FILE with your Hetzner token first."
else
  systemctl restart "${SERVICE}.service"
  echo "Installed and started: ${SERVICE}.service"
fi
cat <<EOF

Config: $ENV_FILE
Status: systemctl status ${SERVICE}
Logs:   journalctl -u ${SERVICE} -f
Edit:   sudo nano ${ENV_FILE}
Apply:  sudo systemctl restart ${SERVICE}

IMPORTANT: AUTO_CREATE=false by default. Set AUTO_CREATE=true to allow paid purchases.
EOF
