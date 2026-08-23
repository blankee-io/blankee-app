# Blankee

Budget tracking and forecasting. Flask + MySQL + Redis, self-hosted.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). Bundled
third-party components are listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

## Install

Two options. Both install everything — database, cache, web server, Python
dependencies, schema and generated secrets — and both end at the same place: a
running site whose first account becomes the administrator.

### Docker

Needs Docker with the Compose plugin. Nothing else.

```bash
git clone https://github.com/blankee-io/blankee-app.git && cd blankee-app
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
git clone https://github.com/blankee-io/blankee-app.git && cd blankee-app
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

## Icons

Blankee ships with **Font Awesome Free**, so it works out of the box.

If you own a **Font Awesome Pro** licence, drop your build in and the app uses it
automatically — no setting to flip, and no restart. It looks for
`static/fontawesome/css/all.min.css` on each page load:

```
static/fontawesome/
    css/all.min.css
    css/custom-icons.min.css     # only if you use a Font Awesome Kit
    webfonts/
```

Remove the directory and it falls straight back to Free. That directory is
gitignored, because Pro is commercial per-seat software and cannot be
redistributed.

Roughly 35 icons exist only in Pro, so the Free path maps each to a Free
equivalent via `static/css/fa-pro-fallback.css`. **That file is generated — do
not edit it.** To change which Free icon stands in for a Pro one, edit the map
and regenerate:

```bash
$EDITOR install/fa_fallback_map.json
python3 install/build_fa_fallback.py
```

### Adding a Pro icon later

Nothing stops you using a new Pro icon — but if it has no fallback, anyone
without a Pro licence sees a blank box. This catches that:

```bash
python3 install/build_fa_fallback.py --check
```

It exits non-zero and names any Pro icon used without a mapping, along with the
files using it. Worth running before you commit.

Two things it cannot see. Class names assembled at runtime — such as
`'fa-chevron-' + direction` — are invisible to a static scan; there is an
`ignore` list in the map for those. And a mapping is only as good as the icon it
points at: `--check` verifies the target exists in Free, but not that it means
the right thing.

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
