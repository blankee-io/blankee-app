# Blankee

Budget tracking and forecasting. Flask + MySQL + Redis, self-hosted.

---

## Install

Two options. Both install everything — database, cache, web server, Python
dependencies, schema and generated secrets — and both end at the same place: a
running site whose first account becomes the administrator.

### Docker

Needs Docker with the Compose plugin. Nothing else.

```bash
git clone <repo> blankee && cd blankee
cp .env.docker.example .env
```

Fill in the three secrets `.env` asks for — the file lists the command to
generate each — then:

```bash
docker compose up -d
```

Open <http://localhost:18420>. Change `HTTP_PORT` in `.env` if you want a
different one.

### Debian / Ubuntu

Installs onto the machine directly with Apache and mod_wsgi.

```bash
git clone <repo> blankee && cd blankee
sudo ./install/install.sh --server-name budget.example.com
```

To see what it would touch without changing anything:

```bash
sudo ./install/install.sh --check
```

It generates its own secrets and database password into
`/var/www/budget_env/.env`. Re-running is safe — existing secrets are kept,
because regenerating them would log everyone out and orphan the stored SMTP
password.

Serving is plain HTTP. For HTTPS, run certbot afterwards and set `APP_URL` in
that file to the `https://` address.

### First run

Open the site and create an account. It becomes the administrator, and
registration closes permanently behind it — after that, accounts are created
from the admin console. That console is also where email delivery is set up,
which is what enables notifications and the Forgot Password link.

### Upgrading

```bash
git pull
```

Then apply any new migrations — this is idempotent, so it is safe whether or not
anything changed:

```bash
# Docker
docker compose up -d --build

# Debian/Ubuntu
sudo /var/www/budget_env/venv/bin/python install/migrate.py
sudo systemctl restart apache2
```

To check the schema without touching it, add `--verify-only`.

---

## Forgotten administrator password

If email delivery is set up, use the Forgot Password link on the login page.
If it isn't, reset it like this:

1. Turn the flag on:

   ```bash
   sudo -u www-data sed -i 's/^RESET_ADMIN_PASSWORD=0/RESET_ADMIN_PASSWORD=1/' \
        /var/www/budget_env/blankee.conf
   ```

   Under Docker the file is inside the `config` volume:

   ```bash
   docker compose exec app sed -i 's/^RESET_ADMIN_PASSWORD=0/RESET_ADMIN_PASSWORD=1/' \
        /config/blankee.conf
   ```

2. Reload the site — the recovery page is now the landing page.

3. Set a new password, then sign in with it.

The flag turns itself back off at step 3. While it is on, anyone who can reach
the site can set the administrator password, so confirm it closed:

```bash
grep '^RESET_ADMIN_PASSWORD' /var/www/budget_env/blankee.conf
```
