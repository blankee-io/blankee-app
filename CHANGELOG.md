# Changelog

Notable changes to Blankee. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/): patch for fixes, minor for features,
major for anything that breaks an existing installation's data or configuration.

Headings are `## <version> — <YYYY-MM-DD>`. Nothing in the application parses
this file; the admin console links to it, it does not read it.

## 1.11.0 — 2026-08-31

### Added
- **Updating now shows you what is happening.** Applying an update used to leave
  the admin page as it was, with a notice that something had been requested -
  while underneath, the application was being replaced and could not answer for
  most of a minute.

  There is now a screen for it: the Blankee mark with a hammer at work, the same
  one shown while a new budget is being built, and a bar counting down the minute
  a run takes. The page reloads itself at the end. If it comes back still running
  the old version the update has not landed yet, so it waits out another minute
  and tries again, up to ten times before it stops and says where the log is.

  It knows the update arrived by checking which version is actually answering,
  not by the updater reporting success - those are different things, and the
  difference is exactly what an update screen exists to show.

## 1.10.4 — 2026-08-31

### Fixed
- **The rest of what 1.10.3 fixed.** Answering "not yet" moves two things: the
  entry itself, and the forecast record it belongs to. 1.10.3 made the entry's
  new date save; the record's did not, so the two halves of one fact ended up
  disagreeing and the confirmation could come back for an entry already dealt
  with. Both now move together.

  The column holding the record's date is named differently from the entry's,
  which is how it escaped the first fix.

- A date revised by a bank feed after import - a payment moving from pending to
  posted, most often - was not saved either. Nothing uses that path today, since
  no bank provider ships with the app, but it is the same fault waiting for
  whoever connects one.

## 1.10.3 — 2026-08-31

### Fixed
- **Changing the date of an entry was never saved.** Answering "not yet" to a
  confirmation, or moving an entry to another day, appeared to work and then
  undid itself a few minutes later - once for every entry, every time.

  Entries are held in a fast store and written to the database behind the scenes.
  That write updated every field of an entry except the one that had changed: its
  date. The entry kept its new date only until the fast copy was next discarded,
  and then went back to where it had been.

  Anything that moves an entry in time was affected - deferring a confirmation,
  the move button on the day view, and the same operations on credit card
  entries and card payments.

  Entries already sitting on the wrong date will correct themselves the next time
  anything in that list is edited, which is what prompts the write. There is
  nothing to run by hand.

## 1.10.2 — 2026-08-31

### Fixed
- **Moving an entry to another date could delete it.** Update immediately if you
  use the move button on the day view - it affected income, expenses and credit
  card entries alike.

  The entry usually reappeared to be gone some minutes later rather than at
  once, because it survived in the cache until the cache was next reloaded.
  Moving it again did not help and made a second copy of the problem.

  What went wrong: moving an entry wrote it at its new date and then deleted it
  from the old one, and the deletion was recorded by the entry's own identifier -
  the same identifier the newly written entry was using. The record of the
  deletion then applied to the surviving entry. Moving now changes the date of
  the entry itself and deletes nothing.

  A second guard was added underneath: an identifier belonging to something that
  still exists can no longer be recorded as deleted, whatever asks for it. Any
  entry lost this way is not recoverable from within the app.

## 1.10.1 — 2026-08-30

### Fixed
- **Entries waiting to be confirmed no longer disappear overnight.** The count in
  the menu bar was hidden until the evening reminder had gone out, and that
  reminder is recorded per day - so at midnight the count vanished and did not
  come back until 8pm, while the entries themselves sat there unanswered the
  whole time. It read as the app having forgotten them.

  Entries that fell due on an earlier day now stay in the menu bar until they are
  answered. Today's still wait for the evening reminder, which is the point of
  the reminder: an entry that became due a minute after midnight is not something
  to be asked about at a minute past midnight.

- **The confirmation prompt shows every entry, not the first fifty.** There was a
  cap, so someone with a longer backlog saw a count in the menu bar that the list
  could not account for, and no way to clear it in one sitting. The same number
  also limited the balance reminder's "confirm everything outstanding" to fifty
  entries per run, quietly leaving a backlog behind on exactly the account that
  most needed clearing.

- The installer ended a successful run on a healthy installation with a warning
  about `GET /register` returning 404. That is the correct response once an
  administrator exists - registration closes behind the first account, and the
  route is meant to look like it never existed - but the check treated it as a
  fault and pointed at the error log.

## 1.10.0 — 2026-08-30

### Added
- **Releases are signed, and new installations check the signature.** The
  updater runs as root and runs the installer out of whatever it just fetched,
  so anyone able to publish a release could run code as root on every machine
  that updates. Until now the only thing standing in the way was access to the
  account that publishes.

  Every release from this one is signed. A new installation pins the public key
  at install time and refuses an update that is not signed by it, stopping
  before anything is written, so a machine that rejects an update carries on
  serving what it already had.

  The key is pinned once and never refreshed automatically - not by a later
  install, not by an update. That is the point: a key that arrived with the code
  could be replaced by whoever replaced the code, and the signature would prove
  nothing. Rotating it takes a person at the machine.

  Existing installations are unaffected and keep updating as before. To turn the
  check on, copy `install/allowed_signers` to `/etc/blankee/allowed_signers` and
  set `UPDATE_VERIFY_SIGNERS` to that path in `blankee.conf`. To turn it off
  again, clear that one line.

### Fixed
- **Updates can now apply the web server settings the application needs.** A
  release that required an Apache directive could not reach an installation that
  updates from the admin console - the updater never touches the web server
  configuration - so 1.9.0's cache fix only arrived for anyone who re-ran the
  installer by hand. Browsers on every other machine carried on using stale
  files, which is the problem that change was meant to solve.

  The directives the application needs now live in their own file, apart from
  the configuration belonging to whoever runs the server: hostname, TLS,
  anything added locally. Updates refresh the first and never touch the second.
  If the web server rejects the result, the new file is removed before anything
  is reloaded.

## 1.9.0 — 2026-08-30

### Added
- **A day box for the iOS home screen.** The app can now show today at a
  glance - income, spending, what is left - without being opened. Two endpoints
  serve it: one issues a token from inside a signed-in session, the other
  returns the day.

  A widget runs as its own process and cannot see the app's session, so it
  carries a token instead. The token is generated with 256 bits of randomness,
  shown once, and stored only as a SHA-256 hash: a readable copy in the
  database would be a password sitting in plain text, and nothing needs to read
  it back. It is accepted only on the widget's own endpoints, so a token taken
  off a device grants a view of the day and nothing else - and never the ability
  to issue more tokens, which is why the endpoint that mints them is
  deliberately not one of them. Revoking is immediate and covers every device.

  The date comes from the phone, not the server. A phone in a timezone behind
  the server would otherwise be handed tomorrow.

  Adds a `widget_tokens` table; `install/migrate.py` applies it.

### Fixed
- **Browsers kept using old CSS and JavaScript after an update.** Apache sends
  no cache instructions for files under `/static` on its own, and with none to
  follow a browser invents its own - commonly a tenth of the file's age, which
  for a long-lived file is days. The pages do not carry a version in those URLs
  either, so an updated installation could serve new markup against an old
  stylesheet: elements in the wrong place, or appearing where they should have
  been hidden, with nothing wrong on the server to find.

  Static files now say to revalidate. An unchanged file still answers "not
  modified", so this costs one small question per file rather than downloading
  anything again. **Existing installations need `install/install.sh` re-run to
  pick this up** - updating does not rewrite the web server's configuration.

- The vhost now sets `WSGIPassAuthorization On`. Apache does not pass the
  `Authorization` header to the application without it, which silently breaks
  any client that authenticates that way.

## 1.8.1 — 2026-08-30

### Fixed
- **Logging in could land on a page of raw JSON instead of the dashboard.**
  Every dashboard load asks the server which forecast entries are waiting to be
  confirmed. When a session had expired, that background request was the first
  one to reach the handler that remembers where you were going, so it was
  remembered as the destination - and signing in delivered you to the answer it
  had wanted, printed as JSON.

  The handler always meant to ignore requests like this; its own comment said
  so. It only checked that the request was a GET, and a background request is a
  GET like any other. It now checks that the browser actually asked for a page,
  so links followed while signed out still return you to the right place after
  signing in, and nothing the page fetches for itself can take that spot.

- The count of hidden categories on the dashboard was drawn half again bigger
  on a phone than the identical count on the confirm-entries button, from a
  mobile-only rule that overrode a size the two are meant to share. It also
  fixed the badge's width, which would have clipped the number once more than
  nine categories were hidden.

### Changed
- **Mobile Options opens the app's server screen directly.** It asked for
  confirmation first, which was a question with only one answer: the row exists
  only inside the app, tapping it is already the decision, and the screen it
  opens has its own way out.

## 1.8.0 — 2026-08-29

### Added
- **A Mobile Options entry in the menu, for the iOS app.** The app asks for a
  server address the first time it is opened and remembers it, which left no
  visible way to point it somewhere else afterwards - only a gesture nobody
  would find on their own. The profile menu now carries a Mobile Options row
  that names the server this app is pointed at and opens the app's own server
  settings, where it can be changed or forgotten.

  The row is hidden unless the page is running inside the app, and that is
  decided in the browser rather than on the server. Nothing in a request says
  which client is asking, and adding a user-agent check would charge every page
  render for something only the app needs. An older app build, or any real
  browser, simply leaves the row hidden - a menu item that does nothing is worse
  than no menu item at all.

## 1.7.2 — 2026-08-27

### Changed
- **Balance corrections go where automatic adjustments already went.** 1.7.0
  gave them a new Autobalance category. It should not have: the app has
  reconciled against bank balances since long before the balance reminder
  existed, and that path never used a category of its own - a current-account
  adjustment goes to Uncategorized, a card adjustment to that card's
  Uncategorized, and savings to a savings_adjustments row with no category at
  all. One idea now has one convention again.

  The category is removed by migration, which moves any entries already in it to
  Uncategorized first. They are not discarded: each one is the difference between
  the app and a real balance, and deleting it would put the two back out of step
  by exactly that amount.

### Fixed
- The Autobalance category carried the `is_auto_adjustment` flag, and the credit
  branch of the bank reconciliation picks its target by scanning for that flag
  and keeping the last match. Which category a bank adjustment landed in would
  therefore have depended on the order rows came back.
- The paid, late, negative and below-threshold markers sat 3px too far right on
  the week grid and the month calendar, where the cells are narrower than on the
  day view, so a marker could overhang its cell and meet the one in the next
  column.

## 1.7.1 — 2026-08-27

### Fixed
- **Tomorrow's entries appeared in the confirmation prompt.** "Today" was taken
  from the server's clock rather than the user's. The Docker image runs UTC, so
  for anyone west of it the server is already on tomorrow's date through their
  evening - from 5pm Pacific, say - and the next day's forecast entries were
  listed as due.

  The same mistake affected the balance reminder, which read the stored balance
  for the server's date: west of UTC that fetched tomorrow's row, so the figure
  offered for reconciliation included a day the user had not had yet. And the
  boundary deciding whether a new entry is a forecast or a record used the
  server's date too, so an entry dated tomorrow could be written as a record.

  All of them now use the date where the user is, which the browser already
  reports on every visit. An account that has never loaded a page since that was
  introduced has no timezone recorded and still falls back to the server's date.

## 1.7.0 — 2026-08-27

### Added
- **Savings and credit cards can be balanced too.** The balance reminder asked
  only about the current account. It now asks about each balance separately -
  current account, savings, and every card - because a card balance is a debt
  and savings is not spendable cash, so netting them into one figure would be
  asking a question with no single right answer.

  Each correction is contained to the thing it corrects. Savings uses an
  adjustment row rather than a Savings category entry, which would be a transfer
  and would move the cash balance as well. A card uses a signed entry on that
  card rather than a payment, because a payment is created from an expense
  against the cash balance and would move two figures at once.
- **An Autobalance category** for the corrections themselves. They used to land
  in Uncategorized, which means "this needs sorting out" - a correction does
  not, and mixed in with genuinely uncategorised spending it hid the real
  backlog. Having them together also makes them add up, which is what turns a
  run of corrections in one direction into a visible signal that something
  upstream is wrong.

### Changed
- **Only balances no bank feed covers are offered.** Checked per account type,
  since a checking feed says nothing about whether savings is kept current, and
  a linked card says nothing about the others. When a feed covers everything,
  the reminder is not sent at all rather than opening an empty prompt.
- **The entry confirmation prompt waits for its notification.** Entries fall due
  at midnight and the prompt used to appear as soon as a date rolled over,
  asking about a day that had barely started. Nothing appears now until the
  notification for that day has actually gone out.
- The confirmation launcher moved from a floating bubble at the bottom left into
  the nav bar, beside the support icon, as an icon with a count over its corner.
- Counts across the app now share one treatment: the entries count, the hidden
  categories count and the notification dot are all the same red, and the two
  numbered ones the same size and shape.
- The verification email sent while setting up email delivery now looks like the
  mail those settings will actually send. It was the only message in the app in
  a different typeface and colour.
- The FAQ describes the app as it is: bank import is not available without a
  provider, buckets are forecasts you confirm rather than a way to tweak an
  occurrence, and the confirmation prompt and balance reminder are explained.

### Fixed
- The shortfall marker on the next-period button was positioned by reaching
  backwards over the button from a sibling element, with one hard-coded offset
  for desktop and another for mobile - so it drifted off the corner whenever the
  button's size changed. It is part of the button now and needs no offsets.
- The paid, late, negative and below-threshold markers on entries and remainders
  sat beneath the current week/month outline and were crossed by it. The paid
  one needed its cell raised as well as the badge: the cell is a stacking
  context, so nothing inside it could rise above the outline on its own.
- A saved cadence could not have its weekday unticked. The settings page only
  ever ticked boxes and never cleared them, so Friday - which is checked by
  default - came back on every render whatever had been saved.

## 1.6.0 — 2026-08-27

### Added
- **End-of-day entry confirmation.** Nothing ever resolved a forecast entry
  whose day had passed, so an unconfirmed forecast stayed in the totals
  indefinitely. At a time you choose, a prompt now lists the day's forecast
  entries and lets you confirm each one, correct its amount, push it to
  tomorrow, or skip it.

  An entry disappears in exactly two cases, both of them an answer you gave:
  skip, or a push that lands on a date the category already has an entry for.
  There is no sweeper and no automatic expiry - an unanswered entry keeps being
  asked about until you answer it.
- **Balance Reminder.** On a cadence you set in Settings, a reminder asks whether
  you want to reconcile against your real bank balance. If you say yes, any
  outstanding confirmations are answered and the difference between the app and
  your bank is recorded as a single Uncategorized entry. Cash only - credit
  accounts are never touched. Nothing is changed unless you say yes, and
  declining costs nothing: the next occurrence asks again.
- **Per-type email notifications.** Email was a single switch, so wanting the
  evening reminder meant accepting a mail for everything else. Each kind of
  notification now has its own switch, all on by default, under the general
  switch that still gates them all.
- Explanatory tips on every option in Settings and in the recurring income,
  expense and credit-expense forms - 155 of them, replacing the static
  descriptions that used to sit under a handful of settings.
- `tzdata`, so timezone-aware scheduling works on a slim image that ships
  without the system database.

### Changed
- **An entry dated today is a real entry when no checking account is synced.**
  Previously anything dated today or later became a forecast, so recording
  today's spending created a second forecast beside the first rather than
  consuming it. With no bank feed there is nothing that will ever confirm it, so
  what you type for today is the record.
- **A manual entry now depletes the occurrence whose period contains it.** For a
  Wage or Bill that is the most recent one due, so paying rent on the 3rd
  consumes the 1st's forecast rather than next month's. For an Allowance it is
  the next one forward, because a period that has ended should not absorb what
  you spend today. This replaces a fixed 45-day look-back that was too long for
  a weekly cadence and shorter than the cadence itself for a yearly one.
- Answering a confirmation updates the page in place - entries, totals and
  remainders - instead of reloading it.
- Bank Accounts is hidden from the profile menu unless an account or connection
  exists. The default provider connects nothing, so on a stock install the menu
  offered a page that could not work.

### Fixed
- The days-late badge counted from today rather than from the date the entry was
  originally due, so an entry pushed this evening showed nothing until tomorrow.
  It now reads 1 the moment it is pushed. Seven places computed this; all seven
  agree now.
- Manage Recurring showed the previous and next occurrence side by side only for
  Wages and Bills. Allowances saw just the upcoming one, with no way to tell
  whether the period just gone had been spent.
- `Email Notifications` could not be turned on. The switch read a variable that
  was never declared, so every click threw before saving and a refresh showed
  the unchanged value - which looked like the switch turning itself off.
- `/get_dashboard_d_data` omitted `original_date`, so the days-late badge
  survived a full page load but vanished on any in-place refresh.
- A credit-account answer did not recalculate the card's daily balance, leaving
  the card showing a balance for spending that no longer existed.
- The evening prompt notification could accumulate: a read one saying "3
  entries" sat under an unread one saying "11". There is now exactly one, and it
  removes itself once nothing is left to confirm.
- A user setting written to Redis but absent from the flush worker's column list
  never reached MySQL, so it was lost on the next cache cycle.

## 1.5.0 — 2026-08-26

### Added
- `TRUST_PROXY`, for running behind a reverse proxy. Off by default, and set to
  the number of proxies in front - `1` for a single nginx. Only then are
  `X-Forwarded-For` and `X-Forwarded-Proto` believed, so the app sees the real
  client address and the scheme they actually used instead of the proxy's
  address and plain http.

  Off by default because these are ordinary request headers: an instance that
  trusts them while being directly reachable lets any client claim any address
  and any scheme.
- The session cookie is now marked `Secure` when `APP_URL` names an `https://`
  address, so it stops travelling over plain HTTP. `SESSION_COOKIE_SECURE=0`
  overrides it for the case where the same instance must also be reachable over
  http on a LAN - without that escape hatch the browser silently withholds the
  cookie and the login page loops.

### Fixed
- The README said certbot could be pointed at a non-standard TLS port. It
  cannot: Let's Encrypt validates on port 80, port 443, or DNS, and there is no
  challenge that reaches a service on 18420, so following that advice fails at
  issuance. Replaced with **Putting it on the internet** - DNS, the three
  certificate routes with a worked nginx example, and the two settings the
  application needs.
- A new **If the address changes** section. Moving to a new IP or hostname
  leaves the site working while `APP_URL` still names the old address, so the
  only visible symptom is that emailed password-reset links go nowhere.

## 1.4.1 — 2026-08-26

### Changed
- The log viewer is 875px wide, matching every other page rather than running to
  1600px and overhanging the navbar above it. The table scrolls inside its own
  box instead of widening the page.
- On screens 600px and under - this stylesheet's phone breakpoint everywhere
  else - each log entry becomes a card with its fields stacked and labelled,
  rather than nine columns dragged sideways. Cells with nothing in them are left
  out, so an entry with no endpoint or user does not show empty rows saying so.
- **Logs** moved out of the profile menu into its own section on the admin
  console, beside Updates.
- The caret on the current-period button is slightly larger and sits about 5px
  lower. Still in em, so it scales as one piece with the glyph beneath it.

### Removed
- The **Source code** entry in the profile dropdown. It was there because the
  footer did not reach every page; since 1.2.0 it does, so the AGPL section 13
  link is still on every page, signed in or not, from one place instead of two.
  `THIRD-PARTY-NOTICES.md` records the change and what to keep in mind if the
  footer is ever moved.

## 1.4.0 — 2026-08-26

### Added
- **A log viewer, at `/admin/logs`.** Filter by level, tag, user, endpoint or
  request id, search message text, page through, and export what matches as CSV.
  Reached from **Logs** in the profile menu, behind the same `@admin_required`
  as the rest of the console.

  This existed as a separate Flask application with its own database of users.
  It is part of Blankee now because the only people who should read these lines
  are the ones the app already authenticates, and a second account to create and
  rotate is a poor trade for a page that renders a file. It needed no new
  dependencies.

  It defaults to reading only the live file. Archives are kept for 180 days, and
  parsing every one of them at 50,000 lines each is nine million lines to render
  one page — a year-old instance would time out on its own log viewer, which is
  precisely when someone needs it. "Last 14 days" and single-day views are one
  click away, and the page says how many files it read.

### Changed
- **Blankee's Apache logs move to `/var/log/blankee`**, owned `root:www-data`
  and mode 750. That is what lets the viewer read them as the web user. The
  alternative was adding `www-data` to `adm`, which also grants `syslog`,
  `auth.log` and `mail.log` — too much for a page that lists log lines.

  A side effect worth having: the files are no longer under `/var/log/apache2`,
  so Debian's apache2 logrotate rule cannot see them, and the 23:59 job is the
  only thing rotating them. The directory split 1.3.0 introduced to dodge that
  rule is no longer a workaround, just where the logs are.

  **Existing installations must run the installer once** — the path is set in
  the Apache vhost, and the updater reloads the WSGI application, not the vhost.
  Until then the viewer explains what to run instead of showing an empty table.

## 1.3.1 — 2026-08-26

### Added
- Repository tooling, not application behaviour: pushing a `v*` tag to the
  public repository now cuts a GitHub release, with the body taken from
  `CHANGELOG.md` rather than retyped. Nothing in Blankee reads releases - the
  updater compares `VERSION` over HTTPS - so this is for people: a readable
  summary on the repository front page, and a feed anyone running an instance
  can subscribe to, which a bare tag cannot provide.

  The notes reach back to the previous tag rather than covering one version,
  because public `main` is a squash of private `main` and one published release
  can carry several private versions - 1.2.0 shipped 1.1.5 inside it.

## 1.3.0 — 2026-08-26

### Added
- The installer now installs the daily log rotation it always shipped but never
  scheduled. `/etc/cron.d/blankee-logs` runs `rotate_blankee_logs.sh` at 23:59
  and keeps a dated copy of each day for 180 days.

  The dated copies go to `/var/log/blankee`, not beside the live logs. Debian's
  stock apache2 logrotate rule globs `/var/log/apache2/*.log` on a 14 day cycle,
  so a copy left there would be rotated again and deleted on day 14 - silently
  cutting the retention this exists to provide to a fortnight. Set
  `BLANKEE_ROTATED_LOG_DIR` to move them; unset, the script behaves as before.

### Changed
- The self-updater writes its progress to `/var/log/apache2/blankee_error.log`
  as well as to the journal, as one JSON line per message tagged `UPDATE`,
  matching the shape the application already writes and the log viewer already
  parses. Failures are logged at `ERROR`. An update is now visible in the log an
  operator already reads instead of only in a second place they have to know
  about. The journal is unchanged, so `journalctl -u blankee-update` still works.

  It appends only to a log that already exists, never creating one: that keeps
  root from guessing an owner and mode for a file Apache manages, and means
  installations with no Apache write nothing.

## 1.2.2 — 2026-08-25

### Fixed
- `install/blankee_update.py` shipped without its executable bit, so the command
  the README gives for checking an update by hand -
  `sudo ./install/blankee_update.py --dry-run --force` - failed with
  `Permission denied` on every installation. The admin console was unaffected,
  because systemd invokes the file through `/usr/bin/python3`; it was only the
  path an operator reaches for when the updater itself is the problem.

### Changed
- The footer logo is 22px rather than 20px. Matching cap heights exactly leaves
  a wordmark looking smaller than the text beside it, because it carries no
  ascenders past its own letters.

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
