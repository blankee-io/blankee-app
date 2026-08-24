#!/usr/bin/env bash
#
# Blankee installer for Debian/Ubuntu. Installs and configures everything:
# Apache with mod_wsgi, MySQL, Redis, Python dependencies, the database schema,
# generated secrets, and the vhost.
#
#   sudo ./install/install.sh                        # interactive
#   sudo ./install/install.sh --server-name budget.example.com
#   sudo ./install/install.sh --check                # check prerequisites, change nothing
#   sudo ./install/install.sh --port 18420           # listen here instead of the default
#
# Re-running is safe. Anything already in place is left alone, and existing
# secrets in the config file are never regenerated - doing so would invalidate
# every session and orphan the stored SMTP password.
set -euo pipefail

# ---------------------------------------------------------------- settings
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${CONFIG_DIR:-/var/www/budget_env}"
ENV_FILE="$CONFIG_DIR/.env"
WSGI_FILE="$CONFIG_DIR/blankee.wsgi"
VENV_DIR="${VENV_DIR:-$CONFIG_DIR/venv}"
DB_NAME="${DB_NAME:-blankee}"
DB_USER="${DB_USER:-blankee}"
DB_HOST="${DB_HOST:-127.0.0.1}"
SERVER_NAME=""
# Deliberately not 80 or 443: this serves on one uncommon port and Apache is
# configured to bind nothing else.
HTTP_PORT="${HTTP_PORT:-18420}"
VHOST="/etc/apache2/sites-available/blankee.conf"
PORTS_CONF="/etc/apache2/ports.conf"
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --port)        HTTP_PORT="$2"; shift 2 ;;
    --check)       CHECK_ONLY=1; shift ;;
    -h|--help)     sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
say "Checking the machine"

[[ $EUID -eq 0 ]] || die "Run with sudo."
[[ -f /etc/debian_version ]] || die "This installer targets Debian/Ubuntu. For anything else, use Docker (see README)."
command -v apt-get >/dev/null || die "apt-get not found."
[[ -f "$APP_DIR/app.py" ]] || die "Cannot find app.py - run this from inside the repository."

info "repository:  $APP_DIR"
info "config dir:  $CONFIG_DIR"
info "database:    $DB_NAME as $DB_USER on $DB_HOST"

if [[ -z "$SERVER_NAME" ]]; then
  if [[ $CHECK_ONLY -eq 1 ]]; then
    SERVER_NAME="localhost"
  else
    read -rp "    Hostname this will be served on [localhost]: " SERVER_NAME
    SERVER_NAME="${SERVER_NAME:-localhost}"
  fi
fi
info "server name: $SERVER_NAME"

if ! [[ "$HTTP_PORT" =~ ^[0-9]+$ ]] || (( HTTP_PORT < 1 || HTTP_PORT > 65535 )); then
  die "--port must be a number between 1 and 65535 (got: $HTTP_PORT)"
fi
if (( HTTP_PORT == 80 || HTTP_PORT == 443 )); then
  warn "port $HTTP_PORT is the default web port this installer exists to avoid"
fi
info "http port:   $HTTP_PORT"

if [[ $CHECK_ONLY -eq 1 ]]; then
  say "Check only - nothing will be changed"
  for pkg in apache2 mysql-server redis-server python3; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then info "installed:     $pkg"; else warn "will install:  $pkg"; fi
  done
  [[ -f "$ENV_FILE" ]] && info "exists:        $ENV_FILE (secrets kept)" || warn "will create:   $ENV_FILE"
  [[ -f "$VHOST" ]] && info "exists:        $VHOST" || warn "will create:   $VHOST"
  warn "will set:      $PORTS_CONF to listen on $HTTP_PORT only"
  if command -v ss >/dev/null && ss -lnt 2>/dev/null | grep -q ":$HTTP_PORT "; then
    warn "in use:        something is already listening on $HTTP_PORT"
  else
    info "free:          port $HTTP_PORT"
  fi
  if id www-data >/dev/null 2>&1; then
    if sudo -u www-data test -r "$APP_DIR/app.py" 2>/dev/null; then
      info "readable:      $APP_DIR by www-data"
    else
      warn "NOT readable:  $APP_DIR by www-data - Apache will 500. A parent directory"
      warn "               denies access; /root is mode 700, the usual cause. Move the"
      warn "               repository to somewhere like /opt/blankee first."
    fi
  fi
  say "Check complete"
  exit 0
fi

# ---------------------------------------------------------------- packages
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  apache2 libapache2-mod-wsgi-py3 \
  mysql-server \
  redis-server \
  python3 python3-pip python3-venv \
  openssl >/dev/null
info "apache2, mod_wsgi, mysql-server, redis-server, python3"

systemctl enable --now mysql >/dev/null 2>&1 || systemctl enable --now mariadb >/dev/null 2>&1 || \
  die "Could not start MySQL."
systemctl enable --now redis-server >/dev/null 2>&1 || die "Could not start Redis."
info "MySQL and Redis are running"

# Apache runs as www-data, and no amount of chown inside the repo helps if a
# parent directory blocks it. Cloning into /root is the easy mistake: /root is
# mode 700, so www-data cannot traverse into it and every request becomes a 500
# with a permission error buried in the Apache log. Fail here instead, where the
# cause is obvious.
if ! sudo -u www-data test -r "$APP_DIR/app.py" 2>/dev/null; then
  die "www-data cannot read $APP_DIR/app.py, so Apache will not be able to serve it.
    A parent directory denies access - /root is mode 700, which is the usual cause.
    Move the repository somewhere Apache can reach and re-run:
        mv $APP_DIR /opt/blankee && cd /opt/blankee && ./install/install.sh"
fi
info "readable by www-data"

# ---------------------------------------------------------------- python deps
say "Installing Python dependencies"
# A virtualenv rather than system-wide pip: Debian marks the system Python as
# externally managed, and mod_wsgi is pointed at this venv below via python-home.
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
info "installed into $VENV_DIR"

# ---------------------------------------------------------------- config
say "Writing configuration"
mkdir -p "$CONFIG_DIR"

# Root owns the directory, www-data has group write. The app creates
# blankee.conf here at startup, and rewrites it when the password-reset flag is
# consumed, so it needs to be able to write in this directory.
chown root:www-data "$CONFIG_DIR"
chmod 770 "$CONFIG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  info "$ENV_FILE exists - keeping it, and every secret in it"
else
  DB_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  SECRET_KEY="$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
  ENCRYPTION_KEY="$("$VENV_DIR/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

  cat > "$ENV_FILE" <<EOF
# Blankee environment. Generated by install/install.sh.
#
# SECRET_KEY signs session cookies and SETTINGS_ENCRYPTION_KEY encrypts the
# stored SMTP password. Losing either is not fatal, but changing them logs
# everyone out and makes the stored mail password undecryptable respectively.
DB_HOST=$DB_HOST
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
DB_NAME=$DB_NAME

REDIS_HOST=127.0.0.1
REDIS_PORT=6379

APP_URL=http://$SERVER_NAME:$HTTP_PORT
SECRET_KEY=$SECRET_KEY
SETTINGS_ENCRYPTION_KEY=$ENCRYPTION_KEY

# Bank and enrichment providers. 'null' means the features are present but
# inert; no vendor is wired up in this build.
BANK_PROVIDER=null
ENRICHMENT_PROVIDER=null
EOF
  info "created $ENV_FILE with generated secrets"
fi

if [[ "$(grep -c "^APP_URL=http://$SERVER_NAME:$HTTP_PORT$" "$ENV_FILE" || true)" -eq 0 ]]; then
  sed -i "s|^APP_URL=.*|APP_URL=http://$SERVER_NAME:$HTTP_PORT|" "$ENV_FILE"
  info "APP_URL set to http://$SERVER_NAME:$HTTP_PORT"
fi

chown www-data:www-data "$ENV_FILE"
chmod 600 "$ENV_FILE"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

cat > "$WSGI_FILE" <<EOF
import sys
from dotenv import load_dotenv

load_dotenv('$ENV_FILE')
sys.path.insert(0, '$APP_DIR')

from app import app as application
EOF
chown root:www-data "$WSGI_FILE"
chmod 640 "$WSGI_FILE"
info "created $WSGI_FILE"

# ---------------------------------------------------------------- database
say "Setting up the database"
# Connecting as root over the local socket: on a default Debian/Ubuntu MySQL the
# root account uses auth_socket, so running as root is the credential.
# --no-defaults so a password in /root/.my.cnf cannot override this connection,
# which fails in a way that looks like wrong credentials.
mysql --no-defaults <<EOF
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASSWORD';
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASSWORD';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
EOF
info "database $DB_NAME and user $DB_USER ready"

DB_HOST="$DB_HOST" DB_USER="$DB_USER" DB_PASSWORD="$DB_PASSWORD" DB_NAME="$DB_NAME" \
  "$VENV_DIR/bin/python" "$APP_DIR/install/migrate.py" || die "Migrations failed."

# ---------------------------------------------------------------- permissions
say "Setting permissions"
chown -R www-data:www-data "$APP_DIR"
# Profile pictures are written here at runtime.
mkdir -p "$APP_DIR/static/uploads"
chown www-data:www-data "$APP_DIR/static/uploads"
chmod 775 "$APP_DIR/static/uploads"
info "$APP_DIR owned by www-data, static/uploads writable"

# ---------------------------------------------------------------- apache
say "Configuring Apache"
a2enmod wsgi >/dev/null 2>&1 || true

# Apache must answer on the chosen port and nothing else. Debian ships
# ports.conf with "Listen 80", plus "Listen 443" inside an ssl_module guard, so
# leaving that file alone keeps both open no matter how the vhost is written.
# It is replaced outright; the original is kept once, for reference.
if [[ ! -f "$PORTS_CONF.blankee-orig" ]]; then
  cp -a "$PORTS_CONF" "$PORTS_CONF.blankee-orig"
  info "original ports.conf saved as $PORTS_CONF.blankee-orig"
fi
cat > "$PORTS_CONF" <<EOF
# Managed by install/install.sh - Blankee listens on $HTTP_PORT and nothing else.
# The Debian original is at ports.conf.blankee-orig.
Listen $HTTP_PORT
EOF
info "listening on $HTTP_PORT only - not 80, not 443"

cat > "$VHOST" <<EOF
<VirtualHost *:$HTTP_PORT>
    ServerName $SERVER_NAME

    # python-home points mod_wsgi at the virtualenv created by the installer.
    WSGIDaemonProcess blankee python-home=$VENV_DIR python-path=$APP_DIR
    WSGIProcessGroup blankee
    WSGIScriptAlias / $WSGI_FILE

    <Directory $APP_DIR>
        Require all granted
    </Directory>

    Alias /static $APP_DIR/static
    <Directory $APP_DIR/static>
        Require all granted
    </Directory>

    ErrorLog \${APACHE_LOG_DIR}/blankee_error.log
    CustomLog \${APACHE_LOG_DIR}/blankee_access.log combined
</VirtualHost>
EOF

a2ensite blankee >/dev/null 2>&1 || true
# The default site would otherwise answer for any hostname that is not this one.
a2dissite 000-default >/dev/null 2>&1 || true
# Ships enabled on some images and would try to bind 443, which no longer exists.
a2dissite default-ssl >/dev/null 2>&1 || true
apache2ctl configtest >/dev/null 2>&1 || die "Apache config test failed - run 'apache2ctl configtest'."
systemctl restart apache2
info "vhost $VHOST enabled"

# ---------------------------------------------------------------- verify
say "Verifying"
sleep 2
CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1:$HTTP_PORT/register || echo 000)"
case "$CODE" in
  200) info "GET /register -> 200: ready for the first account" ;;
  302) info "GET /register -> 302: an administrator already exists, so registration is closed" ;;
  *)   warn "GET /register -> $CODE. Check /var/log/apache2/blankee_error.log" ;;
esac

DB_HOST="$DB_HOST" DB_USER="$DB_USER" DB_PASSWORD="$DB_PASSWORD" DB_NAME="$DB_NAME" \
  "$VENV_DIR/bin/python" "$APP_DIR/install/migrate.py" --verify-only >/dev/null \
  && info "schema verified" || warn "schema verification failed"

say "Done"
# The server name may not resolve yet, so print the IPs too - otherwise the
# only URL offered here is one the browser cannot reach.
IPS="$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}' | tr '\n' ' ')"
cat <<EOF
    Open http://$SERVER_NAME:$HTTP_PORT/ and create the first account - it becomes the
    administrator, and registration closes behind it.

    Serving on port $HTTP_PORT. If $SERVER_NAME does not resolve yet, reach
    it by address instead:
$(for ip in $IPS; do echo "        http://$ip:$HTTP_PORT/"; done)

    Configuration:  $ENV_FILE
    Logs:           /var/log/apache2/blankee_error.log
    Serving over plain HTTP. For HTTPS, run certbot and set APP_URL to the
    https:// address in $ENV_FILE.
EOF
