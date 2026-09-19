#!/usr/bin/env bash
# Install or update the Blankee push relay on a Debian/Ubuntu host.
#
#   sudo ./install.sh
#
# Puts the code in /opt/blankee-relay, a virtualenv beside it, the settings in
# /etc/blankee-relay/relay.env (root-owned, filled in by hand), the database in
# /var/lib/blankee-relay, and a systemd service listening on 127.0.0.1:8100.
# Whatever fronts it - the reverse proxy that holds the TLS certificate - points
# push.blankee.io at that port.
#
# Safe to run again: existing settings and the database are kept.
set -euo pipefail

APP_DIR=/opt/blankee-relay
ENV_DIR=/etc/blankee-relay
ENV_FILE=$ENV_DIR/relay.env
DATA_DIR=/var/lib/blankee-relay
USER=blankee-relay
PORT=${RELAY_PORT:-8100}
# 127.0.0.1 when the TLS-terminating proxy runs on this host; a LAN address
# or 0.0.0.0 when it runs elsewhere and reaches the relay over the network.
BIND=${RELAY_BIND:-127.0.0.1}
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# python3-venv is not part of Ubuntu's python3; without it `python3 -m venv`
# creates a venv with no pip and every later step fails. Said up front.
python3 -c 'import ensurepip' 2>/dev/null || { echo "python3-venv is missing: apt install python3-venv" >&2; exit 1; }

id -u "$USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$USER"

mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"
install -m 644 "$HERE/relay.py" "$APP_DIR/relay.py"
install -m 644 "$HERE/requirements.txt" "$APP_DIR/requirements.txt"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# Blankee push relay settings. Read by the systemd service as root; the
# service itself runs as $USER and only ever sees the values.
#
# The APNs key from developer.apple.com (Certificates, Identifiers & Profiles
# > Keys, with Apple Push Notifications service enabled). Put the .p8 file in
# $ENV_DIR, root-only, and name it here.
APNS_KEY_PATH=$ENV_DIR/apns.p8
APNS_KEY_ID=
APNS_TEAM_ID=
APNS_TOPIC=io.blankee.app

RELAY_DB=$DATA_DIR/relay.db
# Pushes one phone may receive in an hour, whoever asks.
RELAY_HOURLY_LIMIT=60
EOF
  echo "created $ENV_FILE - fill in the APNS_ values and restart the service"
fi
chmod 600 "$ENV_FILE"
chown -R "$USER:$USER" "$DATA_DIR"
# The key must be readable by the service user and nobody else.
if [ -f "$ENV_DIR/apns.p8" ]; then
  chown "$USER:$USER" "$ENV_DIR/apns.p8"; chmod 400 "$ENV_DIR/apns.p8"
fi

cat > /etc/systemd/system/blankee-relay.service <<EOF
[Unit]
Description=Blankee push relay
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
Group=$USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn --bind $BIND:$PORT --workers 2 --timeout 30 relay:app
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=$DATA_DIR
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now blankee-relay.service
systemctl restart blankee-relay.service
sleep 1
if "$APP_DIR/venv/bin/python" -c "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:$PORT/v1/health', timeout=5).read().decode())"; then
  echo
  echo "relay is up on 127.0.0.1:$PORT"
else
  echo "relay did not answer; see: journalctl -u blankee-relay -n 50" >&2
  exit 1
fi
