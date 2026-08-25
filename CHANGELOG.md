# Changelog

Notable changes to Blankee. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/): patch for fixes, minor for features,
major for anything that breaks an existing installation's data or configuration.

Headings are `## <version> — <YYYY-MM-DD>`. Nothing in the application parses
this file; the admin console links to it, it does not read it.

## 1.2.1 — 2026-08-25

### Fixed
- The Buy Me a Coffee widget's close button sat on every page. It is created
  hidden and the vendor only reveals it on narrow screens, because elsewhere its
  floating button doubles as the close control - and 1.2.0 hides that floating
  button, so the close button was forced visible unconditionally to compensate.
  It is now shown only while the overlay is actually open, read off the iframe's
  inline transform since the widget marks the open state nowhere else.

## 1.2.0 — 2026-08-25

### Added
- A **Buy me a coffee** button in the footer, opening the widget centred on
  screen and sized to fit the viewport.
- A copyright line in the footer.

### Changed
- The navbar logo now goes to your own landing page rather than to
  `blankee.io`. Signed-out pages still point at the marketing site, which is
  the only useful destination when there is no account to land in.
- The current-period button uses Font Awesome Free's calendar-day icon, so it
  renders the same with or without a Pro licence.
- The footer wordmark is sized to the text beside it rather than to a round
  number of pixels: the wordmark fills 0.434 of the SVG's box, so 20px puts it
  at the cap height of the 12px text it sits next to.
- The "member since" badge sits above the current-week outline on the dashboard
  and three-month views.

### Fixed
- The footer was missing from the dashboard summary page - the one full page
  that never included it.
- The caret on the current-period button sat lower on hover than at rest. It
  was a `fa-caret-down` glyph, and two fonts hinted at two different pixel
  sizes do not keep their ink in the same relative place, so the button's
  hover `scale(1.2)` re-rasterised both and they drifted apart. It is drawn
  with borders now, which is pure box geometry with nothing to hint.
- The current-period button's hover matches the other navigation buttons.

## 1.1.5 — 2026-08-25

### Added
- With automatic updates **off**, the nightly timer now still checks, and
  records what it found. An operator who has not opted into unattended updates
  should still be told that an update exists.
- When one is waiting, administrators see a small orange cloud icon in the
  footer, and are offered it once in a dialog with **Update**, **Dismiss**, and
  **Do not show again for this update**. The checkbox is remembered per update,
  so the next one asks again. Only administrators see either: nobody else can
  act on it.

### Changed
- The Updates section uses a cloud-with-arrow icon, and shows a spinner while
  checking rather than spinning the download arrow.
- The administrator tag in the user list reads `Admin`, and just `A` on screens
  600px and under.

### Fixed
- The automatic-update switch rendered as a small dark box instead of the orange
  pill used everywhere else. The rules that size and colour a toggle are scoped
  to `.profile-form`, and the Updates section had none — so it now uses the same
  form and `.input-group` markup as the rest of the console.

## 1.1.3 — 2026-08-25

### Fixed
- The version line and the automatic-update toggle now line up with the rest of
  the administrator console. Both sat hard left with their control pushed to the
  far right, while every other row puts its label in the same right-aligned
  column, so the Updates section did not read as part of the same form.

## 1.1.2 — 2026-08-25

### Fixed
- Corrected the 1.1.1 note above, which claimed that taking 1.1.1 was enough to
  install the missing timer. It is not: an update is carried out by the updater
  already running, so a fix to the updater takes effect on the update after the
  one that delivers it.

## 1.1.1 — 2026-08-25

### Fixed
- Updating now installs changed systemd units. It did not before, so 1.1.0
  shipped an automatic-update toggle whose nightly timer was never installed:
  the switch saved its setting and nothing acted on it. `install.sh` grew
  `--units-only` for the purpose, so the installer stays the only place those
  units are defined.

  **Taking 1.1.1 does not close the gap by itself.** The updater that performs
  an update is the one already running, so 1.1.0's updater installs 1.1.1's code
  while still following 1.1.0's steps. Either run
  `sudo ./install/install.sh --units-only` once, or take the update after this
  one, which will do it. This is inherent to updating in place, and applies to
  any future change to a unit file.
- The toggle now says so if the nightly timer is missing, rather than reporting
  success for a setting nothing will act on.

## 1.1.0 — 2026-08-25

Updating, from the administrator console.

### Added
- An **Updates** section at the top of the administrator console: the running
  version, and a button that checks for a newer one. If there is an update it
  offers to install it — pulling the code, installing any new dependencies,
  applying migrations and reloading.
- **Update automatically**, off by default. When on, a nightly timer checks at
  midnight and installs anything newer. Turning it off takes effect immediately.
- The check also notices when the installed Python packages do not match
  `requirements.txt`, or when a migration is outstanding, and says so — but only
  when something is wrong.
- `install/check_requirements.py`, which fails if the code imports something
  `requirements.txt` does not declare. That is the class of bug that made `httpx`
  and `PyJWT` break clean installs.
- The version now appears in the footer of every page.

### Changed
- The documented upgrade steps were wrong in two ways and are now right. They
  omitted `pip install -r requirements.txt`, so a release adding a dependency
  failed at import; and the `migrate.py` command supplied no database
  credentials, so it exited immediately with "Missing environment variables".

### Security
- The web server cannot apply an update itself, and gains no privilege from this
  feature. It writes a request flag into a file it owns; a root-owned systemd
  unit reads that flag, treats it as hostile input, and does the work.
- Reloading touches the WSGI script rather than restarting Apache, so no request
  is dropped. Measured: 120 of 120 requests survived a reload; an Apache restart
  dropped two.

### Notes
- An update refuses to run if the working tree has local modifications, verifies
  the existing schema before changing anything, and stops at the first failure
  rather than pressing on. There is no automatic rollback: a code rollback cannot
  undo an applied migration, so it would risk newer schema under older code.

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
