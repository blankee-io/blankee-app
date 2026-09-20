# Contributing to Blankee

Blankee is a self-hosted budgeting application: people run it on their own
machine, it holds their real money, and it is maintained by one person. That
shapes everything below. Contributions are welcome, and the bar is less "is this
clever" than "will this still be right on somebody's instance in a year, when
nobody is watching".

There is no test suite. That is not a boast — it means review is by reading, and
a pull request that explains what it changed and how it was checked gets merged,
while one that does not sits waiting.

## Before writing code

- **A bug** — open an issue with the form. The version from the footer and the
  relevant log lines are usually enough to find it.
- **A feature or an idea** — start a [Discussion]. Blankee is opinionated about
  how forecasting works, and a change can be good code and still not fit. Ten
  minutes in a discussion can save an afternoon of work.
- **A small fix** — typo, broken link, obvious mistake — just send the pull
  request.
- **A security issue** — do not open an issue. See [SECURITY.md](SECURITY.md).

[Discussion]: https://github.com/blankee-io/blankee-app/discussions

## Getting it running

Docker builds the working tree, so a clone is enough:

```bash
git clone https://github.com/blankee-io/blankee-app.git && cd blankee-app
cp .env.docker.example .env                                 # fill in the three secrets it names
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up -d --build
```

Open <http://localhost:18420> and create the first account — it becomes the
administrator and registration closes behind it. The override file mounts your
clone into the container and turns on template reloading, so an edit to a
template or a `.py` file is visible after a refresh; without it, both are baked
into the image and you have to rebuild. After changing `requirements.txt`, run
`docker compose up -d --build app`.

Bank syncing and AI categorisation are off unless you configure them
(`BANK_PROVIDER=null` and no Claude key), and most work does not need them.

## How a change lands

1. Branch off `main`, one topic per branch.
2. Open a pull request. The checks run automatically (see below).
3. It gets reviewed by reading. Expect questions about the conventions below,
   and about what happens on an instance that has data in it already.
4. It is **squash-merged** — one commit on `main`, authored by you.
5. It ships in the next release, which is a signed tag. Your name goes in the
   changelog entry and in [CONTRIBUTORS.md](CONTRIBUTORS.md).

`main` is not a release. Installations follow the newest signed tag, so a merge
changes nothing for anybody until a release is cut — which means a merged change
gets a little time to be wrong before it reaches real instances.

## Sign your work

Every commit needs a `Signed-off-by` line, which `git commit -s` adds for you.
It is the [Developer Certificate of Origin](https://developercertificate.org):
a statement that you wrote the change, or have the right to submit it, and that
you are content for it to be distributed under the project's licence. There is
no CLA to sign and nothing to post.

```
Signed-off-by: Your Name <your.email@example.com>
```

Forgot on the last commit:

```bash
git commit -s --amend --no-edit && git push --force-with-lease
```

On several commits:

```bash
git rebase --signoff main && git push --force-with-lease
```

The DCO check on your pull request will tell you exactly which commits are
missing it.

## The conventions that matter here

These are not style preferences. Each one exists because breaking it caused a
real problem.

### Redis first, always

**Redis is the primary store; MySQL is the durable copy.** A background worker
flushes dirty tables to MySQL every 15 seconds and deletes MySQL rows that Redis
no longer has. So a write that goes straight to MySQL is either overwritten on
the next flush or treated as an orphan and deleted.

```python
# correct — write to Redis and mark the table dirty
_update_entry_in_redis('income_entries', user_id, category_id, date, amount)

# wrong — the flush worker will undo this
cursor.execute("UPDATE income_entries SET amount = %s WHERE id = %s", (amount, entry_id))
```

Use the existing helpers (`_update_entry_in_redis`, `_delete_entry_in_redis` and
the rest in `app.py`); `docs/redis_keys.md` documents the key layout and the
dirty-table protocol.

### Money is `Decimal`, and forecasts are load-bearing

Amounts are `Decimal`, never `float`, anywhere a total is produced. The forecast
model — buckets, recurring records, allowances versus bills — is the heart of the
application and the easiest thing to break invisibly, because a wrong forecast
looks like a plausible number. Read `bucket_utils.py` and `bucket_confirmation.py`
before changing anything that touches it, and say in the pull request which dates
and amounts you tried.

### No inline styles, and no new colours

All styling goes in `static/css/style.css` with a semantic class name. Every
colour comes from the CSS custom properties defined on `:root` at the top of that
file — `--primary`, `--accent`, `--danger`, `--text-dark`, the `--shadow-*` set
and so on. No hex, `rgb()` or `rgba()` literals.

```html
<div style="color: red;">      <!-- no -->
<div class="error-message">    <!-- yes -->
```

### Reuse the toast and the modal

`showToast(message, type, duration)` and `showConfirmModal(options)` live in
`static/js/script.js`. Use them. If one cannot do what you need, **extend the
shared helper** rather than writing a second implementation beside it: two
modals diverge immediately — different escape handling, different focus
behaviour — and the difference is invisible until somebody hits it.

### Structured logging, never `print`

Log through the helpers in `log_config.py`, always with an uppercase tag, and
pass context as keyword arguments rather than formatting it into the message:

```python
log_info(logger, 'INCOME', 'Entry created', entry_id=new_id, amount=amount)
log_exception(logger, 'REDIS', 'Could not update the cache', user_id=user_id)
```

Existing tags include `AUTH`, `INCOME`, `EXPENSE`, `REDIS`, `BUCKET`, `BANK`,
`AUTOBALANCE`, `UPDATE`. Reuse one where it fits. Never `print()`, never
`app.logger` directly — the admin log viewer parses the JSON these produce.

### Migrations are three edits in one commit

1. the SQL file in `install/sql/`,
2. an entry appended to `MIGRATIONS` in `install/migration_manifest.py`,
3. the assertions in `EXPECTED_TABLES` / `EXPECTED_COLUMNS` /
   `EXPECTED_CONSTRAINTS` in `install/migrate.py`.

Run `python3 install/check_migrations.py` before committing; it checks all three
without needing a database.

A migration must also stay **backward-compatible with the previous release**. An
update applies code, then dependencies, then migrations, then reloads — so for a
moment the new schema serves the old code. Add columns; do not rename or drop one
in the same release that stops using it. Removal is a later release's job.

### Files not to touch

- `static/css/fa-pro-fallback.css` — generated. Change
  `install/fa_fallback_map.json` and run `install/build_fa_fallback.py`.
- `install/allowed_signers` — the trust anchor installations pin to verify
  releases. A pull request that edits it will be declined on sight.
- `static/fontawesome/` — Font Awesome **Pro**, licensed per seat and
  deliberately absent. The Free fallback in `static/fontawesome-free/` is what
  makes a clean clone work. Never add Pro assets.
- Anything under `optimization/` or `index-optimization/` — historical notes.

### Adding a sibling app

Blankee hosts a family of small apps (Loaf, the time-off planner, is the first).
`apps_registry.py` is the only place an app is described, and an app with no row
in `instance_apps` is off. The registry's own docstring is the guide.

## What the checks do

Every pull request runs, and all four must pass:

| Check | What it catches |
|---|---|
| DCO | a commit without `Signed-off-by` |
| `install/check_requirements.py` | an import that is not declared in `requirements.txt` |
| `install/build_fa_fallback.py --check` | a Pro-only icon with no Free fallback |
| `install/check_migrations.py` | a migration missing from the manifest or the assertions |

They are all standard-library Python and run in seconds. Run them locally first.

## Saying how you tested it

Since there is nothing automated to lean on, this is the part of a pull request
that earns trust. Not "tested locally" — rather which page you opened, what you
did, and what you saw. For anything touching money: the dates, the amounts, and
what the totals were before and after. For anything touching the bank importer:
whether you used a real feed or a stub, and what happened to the forecast the
transaction landed on.

## Licence

Blankee is [AGPL-3.0](LICENSE). By contributing you agree that your contribution
is licensed under it, and your `Signed-off-by` line records that you have the
right to make it. If you run a modified copy as a service, the AGPL asks you to
offer that source to its users — that is the point of the licence, and the reason
a self-hosted budgeting app uses it.
