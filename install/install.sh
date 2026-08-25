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
#   sudo ./install/install.sh --no-self-update       # do not install the updater
#   sudo ./install/install.sh --permissions-only     # re-apply ownership and exit
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
SECURE_DIR="/etc/blankee"
DB_CONF="$SECURE_DIR/db.conf"
CONF_FILE="$CONFIG_DIR/blankee.conf"
CHECK_ONLY=0
PERMISSIONS_ONLY=0
SELF_UPDATE=1
UPDATER_SERVICE="/etc/systemd/system/blankee-update.service"
UPDATER_TIMER="/etc/systemd/system/blankee-update.timer"
UPDATER_AUTO_SERVICE="/etc/systemd/system/blankee-update-auto.service"
UPDATER_AUTO_TIMER="/etc/systemd/system/blankee-update-auto.timer"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --port)        HTTP_PORT="$2"; shift 2 ;;
    --check)       CHECK_ONLY=1; shift ;;
    --permissions-only) PERMISSIONS_ONLY=1; shift ;;
    --no-self-update)   SELF_UPDATE=0; shift ;;
    -h|--help)     sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done


say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# Set one KEY=VALUE in blankee.conf, in place, appending if absent. The app does
# the same thing from Python; this is for the values the installer decides.
set_conf_key() {
  local key="$1" value="$2"
  [[ -f "$CONF_FILE" ]] || return 0
  if grep -qE "^[[:space:]]*$key=" "$CONF_FILE"; then
    sed -i "s|^[[:space:]]*$key=.*|$key=$value|" "$CONF_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$CONF_FILE"
  fi
}

apply_permissions() {
  # static/uploads (profile pictures) is the only path the application writes to,
  # so the code stays owned by root and merely readable. Two reasons not to hand
  # the whole tree to www-data: a web process able to rewrite its own source turns
  # any code-execution bug into persistence, and a repository owned by www-data
  # makes every later "git pull" as root fail with "detected dubious ownership".
  #
  # A function because the self-updater calls it through --permissions-only after
  # every checkout. git creates new files with root's umask, so on a host with a
  # restrictive one every added file would be unreadable by www-data and the site
  # would 500 the moment it reloaded. One copy of these rules, called from both
  # places.
  chown -R root:root "$APP_DIR"
  chmod -R a+rX "$APP_DIR"
  mkdir -p "$APP_DIR/static/uploads"
  chown -R www-data:www-data "$APP_DIR/static/uploads"
  chmod 775 "$APP_DIR/static/uploads"
}

# The self-updater calls this after every checkout. Kept as early as possible so
# it needs nothing else to be true - no packages, no database, no vhost.
if [[ $PERMISSIONS_ONLY -eq 1 ]]; then
  [[ $EUID -eq 0 ]] || die "Run with sudo."
  [[ -f "$APP_DIR/app.py" ]] || die "Cannot find app.py - run this from inside the repository."
  say "Setting permissions"
  apply_permissions
  info "code owned by root and readable; static/uploads writable by www-data"
  exit 0
fi


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
    if sudo -u www-data test -x "$(dirname "$APP_DIR")" 2>/dev/null; then
      info "reachable:     $APP_DIR by www-data"
    else
      warn "UNREACHABLE:   www-data cannot traverse into $(dirname "$APP_DIR"), so"
      warn "               Apache will 500 on every request. /root is mode 700, the"
      warn "               usual cause. Move the repository to /opt/blankee first."
    fi
  fi
  if [[ -f "$UPDATER_TIMER" ]]; then
    if systemctl is-enabled blankee-update.timer >/dev/null 2>&1; then
      info "enabled:       blankee-update.timer"
    else
      warn "installed but not enabled: blankee-update.timer"
    fi
  else
    warn "will create:   $UPDATER_TIMER"
  fi
  if [[ -f "$APP_DIR/VERSION" ]]; then
    info "version:       $(cat "$APP_DIR/VERSION")"
  else
    warn "no VERSION file - the footer will show no version"
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
  openssl curl ca-certificates >/dev/null
info "apache2, mod_wsgi, mysql-server, redis-server, python3, curl"

systemctl enable --now mysql >/dev/null 2>&1 || systemctl enable --now mariadb >/dev/null 2>&1 || \
  die "Could not start MySQL."
systemctl enable --now redis-server >/dev/null 2>&1 || die "Could not start Redis."
info "MySQL and Redis are running"

# Apache runs as www-data, and no chmod inside the repo helps if an ancestor
# directory blocks the way in. Cloning into /root is the easy mistake: /root is
# mode 700, so www-data cannot traverse into it and every request becomes a 500
# with a permission error buried in the Apache log. Fail here, where the cause is
# obvious. Testing the parent rather than app.py on purpose - test -x requires
# traversal of every ancestor, and it does not depend on the repo's own modes,
# which the permissions step below fixes anyway.
PARENT_DIR="$(dirname "$APP_DIR")"
if ! sudo -u www-data test -x "$PARENT_DIR" 2>/dev/null; then
  die "www-data cannot traverse into $PARENT_DIR, so Apache will not be able to
    serve $APP_DIR. /root is mode 700, which is the usual cause.
    Move the repository somewhere Apache can reach and re-run:
        mv $APP_DIR /opt/blankee && cd /opt/blankee && ./install/install.sh"
fi
info "reachable by www-data"

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

# Root owns the directory and www-data may read and traverse it, but NOT write
# in it. That distinction is the security boundary: write permission on a
# directory is the right to rename() and unlink() its entries whatever their own
# modes say, so a group-writable CONFIG_DIR would let the web user swap out the
# virtualenv root is about to run pip from, or replace blankee.wsgi - the file
# that loads the application - even though both are root-owned. 750 closes both
# without moving anything.
#
# The cost is that the app can no longer create blankee.conf itself, so this
# installer creates it below. The app only ever rewrites it in place, which
# needs permission on the file rather than the directory.
chown root:www-data "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

# Root-only home for things root reads or executes during an update. Never
# world-readable and never writable by the web user.
mkdir -p "$SECURE_DIR"
chown root:root "$SECURE_DIR"
chmod 755 "$SECURE_DIR"

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

# Read the four values this installer needs WITHOUT executing the file.
#
# `source "$ENV_FILE"` would be shell injection: .env is owned and writable by
# www-data, so a compromised web process could leave
# DB_NAME='x$(chmod u+s /bin/dash)' in it and have this script run that as root
# on the next install. The same reasoning applies to the updater, which is why
# the credentials it uses are written to a root-only file further down instead.
read_env_key() {
  sed -n "s/^[[:space:]]*$1=//p" "$2" | tail -1
}

DB_HOST="$(read_env_key DB_HOST "$ENV_FILE")"
DB_USER="$(read_env_key DB_USER "$ENV_FILE")"
DB_NAME="$(read_env_key DB_NAME "$ENV_FILE")"
DB_PASSWORD="$(read_env_key DB_PASSWORD "$ENV_FILE")"

for _v in DB_HOST DB_USER DB_NAME; do
  [[ "${!_v}" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "$_v in $ENV_FILE is empty or has unexpected characters. Fix it by hand."
done
# The password is interpolated into single-quoted SQL below, so these two
# characters are the ones that could break out of the quoting.
case "$DB_PASSWORD" in
  '' ) die "DB_PASSWORD is empty in $ENV_FILE." ;;
  *[\'\\]* ) die "DB_PASSWORD contains a quote or a backslash. MySQL treats both as special inside the quoted string this installer builds, so it cannot be passed safely. Change it in $ENV_FILE." ;;
esac

# blankee.conf, created here rather than by the app. CONFIG_DIR is no longer
# group-writable, so server_config.ensure_config_file() can no longer create it
# on first request - but the app only ever rewrites it in place, which needs
# permission on the file, not the directory.
#
# Calling the app's own function rather than writing a second copy of the
# template in bash: that file is documentation as much as configuration, and two
# copies of it would drift.
if [[ ! -f "$CONF_FILE" ]]; then
  if PYTHONPATH="$APP_DIR" BLANKEE_CONFIG="$CONF_FILE" \
       "$VENV_DIR/bin/python" -c 'import server_config; server_config.ensure_config_file()'; then
    info "created $CONF_FILE"
  else
    warn "could not create $CONF_FILE - flags will all read as off until it exists"
  fi
fi
if [[ -f "$CONF_FILE" ]]; then
  chown www-data:www-data "$CONF_FILE"
  chmod 640 "$CONF_FILE"
fi

# Database credentials for root-run tooling. The updater and migrate.py need
# these, and .env is owned and writable by www-data - so a root process reading
# it would be taking its instructions from the web user. This copy is root-only
# and is the one root reads.
umask 077
cat > "$DB_CONF" <<EOF
# Database credentials for root-run Blankee tooling. Generated by
# install/install.sh from $ENV_FILE, which is owned by www-data and must never
# be read by a privileged process.
DB_HOST=$DB_HOST
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
DB_NAME=$DB_NAME
EOF
umask 022
chown root:root "$DB_CONF"
chmod 600 "$DB_CONF"
info "wrote $DB_CONF (root only)"

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
apply_permissions
info "code owned by root and readable; static/uploads writable by www-data"

# ---------------------------------------------------------------- self-updater
say "Configuring the self-updater"
if [[ $SELF_UPDATE -eq 0 ]]; then
  set_conf_key SELF_UPDATE 0
  info "skipped (--no-self-update); the console will show the commands instead"
elif ! command -v systemctl >/dev/null; then
  set_conf_key SELF_UPDATE 0
  warn "no systemd here, so the updater was not installed; updates stay manual"
else
  cat > "$UPDATER_SERVICE" <<EOF
[Unit]
Description=Blankee self-update (applies a request from the admin console)
Documentation=file://$APP_DIR/docs/RELEASING.md
After=network-online.target mysql.service apache2.service
Wants=network-online.target

[Service]
Type=oneshot
# Root deliberately: it replaces root-owned code, installs into a root-owned
# virtualenv and reloads the web server. The web process gains nothing from
# this - it can only set a flag in blankee.conf, which this reads and validates.
#
# /usr/bin/python3 rather than the virtualenv's python: the moment this most
# needs to report clearly is when pip has just broken that virtualenv.
#
# ExecStart points at the copy in the repository on purpose. The script rewrites
# that tree while running, which is safe because CPython compiles the whole file
# before executing it - and it means there is no second copy to go stale.
ExecStart=/usr/bin/python3 $APP_DIR/install/blankee_update.py
ExecStopPost=/usr/bin/python3 $APP_DIR/install/blankee_update.py --mark-aborted
Environment=BLANKEE_APP_DIR=$APP_DIR
Environment=BLANKEE_CONFIG_DIR=$CONFIG_DIR
Environment=BLANKEE_CONFIG=$CONF_FILE
Environment=BLANKEE_VENV=$VENV_DIR
Environment=BLANKEE_WSGI=$WSGI_FILE
Environment=BLANKEE_DB_CONF=$DB_CONF
UMask=0022
TimeoutStartSec=1800
Nice=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=blankee-update
EOF

  cat > "$UPDATER_TIMER" <<EOF
[Unit]
Description=Check for a Blankee update request every minute

[Timer]
# A timer rather than a .path unit watching blankee.conf. A path unit is
# instant, but: an in-place rewrite truncates the file first, so the watcher can
# fire on the truncation, read an empty file and exit, and the event for the
# real content is dropped while the service is still active - the button appears
# to do nothing, silently. PathModified also never fires retroactively, so a
# request written while the unit is stopped is lost. A timer re-reads the
# current state every tick, which makes all of that impossible.
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s
Unit=blankee-update.service

[Install]
WantedBy=timers.target
EOF

  cat > "$UPDATER_AUTO_SERVICE" <<EOF
[Unit]
Description=Blankee automatic update (daily, when AUTO_UPDATE is on)
Documentation=file://$APP_DIR/docs/RELEASING.md
After=network-online.target mysql.service apache2.service
Wants=network-online.target

[Service]
Type=oneshot
# Same updater, same privileges, same refusals. The only difference is that it
# runs on a schedule and exits immediately unless AUTO_UPDATE is set, which is
# read from blankee.conf on every run - so turning it off takes effect at once.
ExecStart=/usr/bin/python3 $APP_DIR/install/blankee_update.py --auto
ExecStopPost=/usr/bin/python3 $APP_DIR/install/blankee_update.py --mark-aborted
Environment=BLANKEE_APP_DIR=$APP_DIR
Environment=BLANKEE_CONFIG_DIR=$CONFIG_DIR
Environment=BLANKEE_CONFIG=$CONF_FILE
Environment=BLANKEE_VENV=$VENV_DIR
Environment=BLANKEE_WSGI=$WSGI_FILE
Environment=BLANKEE_DB_CONF=$DB_CONF
UMask=0022
TimeoutStartSec=1800
Nice=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=blankee-update
EOF

  cat > "$UPDATER_AUTO_TIMER" <<EOF
[Unit]
Description=Apply Blankee updates nightly when AUTO_UPDATE is on

[Timer]
# Local midnight. Persistent so a machine that was off overnight catches up on
# the next boot rather than skipping a day silently.
OnCalendar=*-*-* 00:00:00
Persistent=true
# Spread the load on the source a little, and avoid every instance in the world
# fetching at the same second.
RandomizedDelaySec=900
Unit=blankee-update-auto.service

[Install]
WantedBy=timers.target
EOF

  chmod 644 "$UPDATER_SERVICE" "$UPDATER_TIMER" "$UPDATER_AUTO_SERVICE" "$UPDATER_AUTO_TIMER"
  systemctl daemon-reload
  # The nightly timer is enabled either way; it does nothing at all unless
  # AUTO_UPDATE is on, and enabling it here means the toggle in the console
  # needs no privileged action to take effect.
  systemctl enable --now blankee-update-auto.timer >/dev/null 2>&1 || true
  if systemctl enable --now blankee-update.timer >/dev/null 2>&1; then
    # Only now is the flag true. It answers "can this instance update itself",
    # so writing it before the timer is actually enabled would make the console
    # offer a button whose request nothing would ever read.
    set_conf_key SELF_UPDATE 1
    info "blankee-update.timer enabled; updates can be applied from the admin console"
  else
    set_conf_key SELF_UPDATE 0
    warn "could not enable blankee-update.timer; updates stay manual"
  fi
fi

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
    # Run in the main interpreter, not a sub-interpreter. mod_wsgi defaults to a
    # sub-interpreter per application, and Rust/PyO3 extension modules refuse to
    # load in one - bcrypt 4.x is built that way, so without this every request
    # dies with "PyO3 modules do not yet support subinterpreters" and Flask-Bcrypt
    # reports itself missing. cryptography is in the same family.
    WSGIApplicationGroup %{GLOBAL}
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
if ! command -v curl >/dev/null; then
  # Said explicitly rather than reported as a bare 000: a verification step that
  # cannot run is worth knowing about, and it used to read as a dead site.
  warn "curl is not installed, so the site could not be checked from here"
else
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1:$HTTP_PORT/register || echo 000)"
  case "$CODE" in
    200) info "GET /register -> 200: ready for the first account" ;;
    302) info "GET /register -> 302: an administrator already exists, so registration is closed" ;;
    000) warn "GET /register -> no response. Check /var/log/apache2/blankee_error.log" ;;
    *)   warn "GET /register -> $CODE. Check /var/log/apache2/blankee_error.log" ;;
  esac
fi

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
