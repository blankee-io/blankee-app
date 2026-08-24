# Changelog

Notable changes to Blankee. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/): patch for fixes, minor for features,
major for anything that breaks an existing installation's data or configuration.

Headings are `## <version> — <YYYY-MM-DD>`. Nothing in the application parses
this file; the admin console links to it, it does not read it.

## 1.0.0 — 2026-08-24

First published version.

### Added
- Budget tracking and forecasting: income, expenses, credit accounts, recurring
  items with buckets, savings, and daily, monthly, three-month and yearly
  dashboards.
- Redis-first storage with a background worker persisting to MySQL, so ordinary
  use never waits on the database.
- An administrator console: user creation, email delivery configuration with a
  verified sending address, and per-user data reset.
- Two supported installations — a single script for Debian and Ubuntu using
  Apache with mod_wsgi, and a Docker image with Compose. Both install
  everything, generate their own secrets, and serve on port 18420, leaving 80
  and 443 free.
- An idempotent migration runner that records what it has applied and verifies
  the resulting schema.
- Administrator password recovery for an instance with no working mail
  configuration, gated on a flag only someone with server access can set.
- Font Awesome Pro is used when present and falls back to a bundled Free build
  otherwise, so every icon renders either way.

### Added (since 1.0.0 was cut)
- The administrator console reports whether newer code exists, whether the
  installed Python packages match `requirements.txt`, and whether any database
  migration is outstanding. Checking contacts GitHub only when the button is
  pressed; nothing runs on a schedule.
- Updates can be applied from the console. The web process only sets a flag in a
  file it owns; a root systemd timer does the work, so the web user gains no
  privilege. Reloading touches the WSGI script rather than restarting Apache, so
  no connection is dropped.
- `install/check_requirements.py` fails if the code imports something
  `requirements.txt` does not declare — the class of bug that made `httpx` and
  `PyJWT` break clean installs.

### Security
- The web user cannot write in the configuration directory. Write permission on
  a directory is the right to replace what is in it, which would have allowed
  the virtualenv that root installs into, and the file that loads the
  application, to be swapped out.
- The installer no longer sources `.env`, which the web user owns; database
  credentials for root-run tooling live in a root-only file instead.
- `SameSite` and `HttpOnly` are set explicitly on the session cookies.

### Fixed
- `httpx` could not be used at all on a clean install. `hyper` was pinned but
  never imported, and it pulled in an old `hyperframe` that uses
  `collections.MutableSet`, removed in Python 3.10 — so constructing an HTTP
  client raised `AttributeError`, which also meant push notifications were
  broken on every fresh install. `hyper` and `apns2` are both gone; neither was
  imported anywhere.
- `PyJWT` is now declared. It was imported by the push-notification code and
  present only because something else happened to install it.

### Notes
- This is the first release, so there is nothing to upgrade from. Later entries
  will describe what changed and anything an operator must do by hand.
- An installation made before the dependency fix above keeps the broken packages,
  because `pip install -r requirements.txt` does not remove what the file no
  longer names. Clear them once with
  `pip uninstall -y apns2 hyper hyperframe h2`.
