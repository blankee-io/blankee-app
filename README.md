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

Either way, start from an up-to-date machine with `git` available. A minimal
container image typically has neither:

```bash
sudo apt update && sudo apt-get upgrade -y
sudo apt-get install -y git
```

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

Installs onto the machine directly with Apache and mod_wsgi. Run the update and
`git` step above first.

```bash
sudo apt update && sudo apt-get upgrade -y
sudo apt-get install -y git

sudo git clone https://github.com/blankee-io/blankee-app.git /opt/blankee
cd /opt/blankee
sudo ./install/install.sh --server-name budget.example.com
```

The installer takes it from there — Apache, mod_wsgi, MySQL, Redis and Python
are all installed by it, so `git` is the only thing needed up front.

Clone it somewhere Apache can reach. **Not your home directory** — `/root` is
mode `700`, so www-data cannot traverse into it, and the result is `Internal
Server Error` on every request with the real cause buried in the Apache log. The
installer now refuses to continue in that situation, but `/opt/blankee` or
`/var/www/blankee` avoids it entirely.

To see what it would touch without changing anything:

```bash
sudo ./install/install.sh --check
```

This serves on **port 18420**, matching the Docker default, and Apache is
configured to bind nothing else — not 80, not 443. Open
`http://budget.example.com:18420/`, or `http://<server-ip>:18420/` if DNS is not
pointing at it yet; the installer disables Apache's default site, so the app
answers for any hostname. Pick a different port with:

```bash
sudo ./install/install.sh --server-name budget.example.com --port 39000
```

Because the standard ports are deliberately left free, the installer replaces
`/etc/apache2/ports.conf` — Debian ships it with `Listen 80` and a `Listen 443`
inside an `ssl_module` guard, which would otherwise stay open regardless of how
the vhost is written. The original is kept once as
`ports.conf.blankee-orig`. If you are adding Blankee to a machine already
serving other sites from Apache, that is the one file to look at first.

If the browser cannot reach it, the app is usually fine and something in front
of it is not. Check, in order:

```bash
systemctl is-active apache2
ss -lntp | grep ':18420'                        # is anything listening?
curl -I http://127.0.0.1:18420/register         # does it answer locally?
sudo ufw status                                 # a firewall the installer does not touch
tail -30 /var/log/apache2/blankee_error.log
```

A local `200` or `302` with nothing reachable from outside means the port is
blocked or unmapped rather than misconfigured — on a Docker container it needs
`-p 18420:18420`, and on LXC or a VM check the host firewall.

Re-running an existing install is worth doing once for a security fix: the
config directory used to be group-writable by the web user, and write permission
on a *directory* is the right to rename or unlink what is in it — so the web user
could have swapped out the virtualenv that root runs `pip` from, or replaced
`blankee.wsgi`, the file that loads the application. Both are root-owned, which
was not enough. The directory is now `750`, which closes it. Nothing moves and
nothing else changes.

It generates its own secrets and database password into
`/var/www/budget_env/.env`. Re-running is safe — existing secrets are kept,
because regenerating them would log everyone out and orphan the stored SMTP
password.

Serving is plain HTTP. For HTTPS, run certbot afterwards, then set `APP_URL` in
that file to the `https://` address including the port. Note that certbot
assumes 443 and will want to add its own `Listen`; if you are keeping the
standard ports clear, point it at your chosen TLS port instead and re-check
`ports.conf` after it runs.

### First run

Open the site and create an account. It becomes the administrator, and
registration closes permanently behind it — after that, accounts are created
from the admin console. That console is also where email delivery is set up,
which is what enables notifications and the Forgot Password link.

### Upgrading

The admin console has an **Updates** section: it shows the running version and a
button that checks for a newer one. If there is an update it offers to install
it — pulling the code, installing any new dependencies, applying migrations and
reloading the application.

The web server cannot do any of that itself, and deliberately so: it can only
write a request flag, which a root-owned systemd timer picks up within a minute.
Progress and the result end up in `journalctl -u blankee-update`, and a failure
leaves the exact recovery commands in the console.

There is also an **Update automatically** toggle, off by default. With it on, a
nightly timer checks at midnight and installs anything newer. Worth a moment's
thought before turning on: it means code from the internet is installed and run
while nobody is watching, and a release with a problem reaches you before anyone
has noticed. The trade is that fixes arrive without you having to remember.

To turn the button off entirely, set `SELF_UPDATE=0` in
`/var/www/budget_env/blankee.conf`; the console then shows the commands instead.
`AUTO_UPDATE=0` in the same file turns off just the nightly run. Both are off
automatically under Docker, where the code is part of the image.

#### From a shell

```bash
# Debian/Ubuntu — the same steps the console runs
sudo systemctl start blankee-update            # applies an update if there is one
sudo /opt/blankee/install/blankee_update.py --dry-run --force   # check without changing anything

# Docker
git pull && docker compose up -d --build
```

Or by hand, which is what to reach for if the updater itself is the problem:

```bash
cd /opt/blankee
sudo git pull
sudo /var/www/budget_env/venv/bin/pip install -r requirements.txt
set -a; . /etc/blankee/db.conf; set +a
sudo -E /var/www/budget_env/venv/bin/python install/migrate.py
sudo /opt/blankee/install/install.sh --permissions-only
sudo systemctl restart apache2
```

Two of those steps are easy to skip and both bite later. **`pip install` is not
optional** — a release that adds a dependency fails at import without it. And
`migrate.py` reads its database credentials from the environment, so it needs
`db.conf` sourced; `sudo` alone scrubs the environment and it exits with
"Missing environment variables".

To check the schema without changing it, add `--verify-only`.

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

It also checks *style* availability, which is a separate trap: Free has 2,583
icons but only 273 in regular, so `fa-regular fa-lock` renders an empty box even
though `fa-lock` is plainly in Free. The regular family is listed in
`install/fa_free_regular.txt`, and any icon requested in regular that Free only
has in solid gets an automatic override to solid rather than being left blank.

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
