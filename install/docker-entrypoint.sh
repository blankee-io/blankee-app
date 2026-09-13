#!/usr/bin/env bash
#
# Container startup: wait for the database, apply migrations, then serve.
#
# Migrations run here rather than in a separate init container so that
# `docker compose up` on an empty volume produces a working application with no
# second step. install/migrate.py is idempotent, so every restart re-running it
# costs one connection and a few information_schema queries.
set -euo pipefail

say() { printf '\033[1m[blankee]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[blankee] FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- secrets
# SECRET_KEY signs session cookies. The application generates a random one if
# it is unset, which silently means sessions break on restart AND differ between
# gunicorn workers - so refuse to start instead of limping.
if [[ -z "${SECRET_KEY:-}" ]]; then
  die "SECRET_KEY is not set. Generate one with:
       python3 -c 'import secrets; print(secrets.token_hex(32))'"
fi

if [[ -z "${SETTINGS_ENCRYPTION_KEY:-}" ]]; then
  say "SETTINGS_ENCRYPTION_KEY is not set - email settings cannot store a password."
  say "Generate one with: python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
fi

for var in DB_HOST DB_USER DB_PASSWORD DB_NAME; do
  [[ -n "${!var:-}" ]] || die "$var is not set."
done

# --------------------------------------------------------------- wait for mysql
say "Waiting for MySQL at $DB_HOST..."
ATTEMPTS=60
until MYSQL_PWD="$DB_PASSWORD" mysql --no-defaults -h "$DB_HOST" -u "$DB_USER" \
        -e 'SELECT 1' "$DB_NAME" >/dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS - 1))
  # compose's depends_on with a healthcheck covers the common case; this covers
  # the rest, because "the port is open" and "the database exists and this user
  # can reach it" are not the same moment.
  [[ $ATTEMPTS -gt 0 ]] || die "MySQL did not become reachable. Check DB_PASSWORD and the db service logs."
  sleep 2
done
say "MySQL is reachable."

# --------------------------------------------------------------- privileges
# Run as the unprivileged user from here on. The container starts as root for
# one reason: a named volume created by an older image is root-owned, and the
# server has to be able to write profile pictures and blankee.conf. So root
# re-owns those two mounts, then everything else - the migrations and the
# server itself - runs as blankee. setpriv is util-linux, in the base image;
# if the image were ever built without the user this degrades to what it did
# before rather than refusing to start.
APP_USER=blankee
drop_privs=0
if [[ "$(id -u)" -eq 0 ]] && id "$APP_USER" >/dev/null 2>&1 && command -v setpriv >/dev/null; then
  chown -R "$APP_USER:$APP_USER" /app/static/uploads /config 2>/dev/null || true
  drop_privs=1
  say "Running as $APP_USER."
fi
as_app() {
  if [[ $drop_privs -eq 1 ]]; then
    setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"
  else
    "$@"
  fi
}

# --------------------------------------------------------------- migrations
say "Applying migrations..."
as_app python3 /app/install/migrate.py || die "Migrations failed."

# --------------------------------------------------------------- go
say "Starting: $*"
if [[ $drop_privs -eq 1 ]]; then
  exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"
fi
exec "$@"
