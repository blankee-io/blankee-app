# Releasing

Blankee has two repositories: a private one where work happens, and a public one
that carries the published source the AGPL requires. `blankee-publish.sh`
snapshots private `main` into the public repository as a single commit, so
**every commit on public `main` is a release**, and installations update to the
tip of that branch.

That has one consequence worth stating plainly: there is no such thing as an
unreleased commit on public `main`. Anything pushed there is what the next
instance to press "check for updates" will be offered.

## Cutting a release

1. On private `main`, set the new version and describe it:

   ```bash
   printf '1.1.0\n' > VERSION
   $EDITOR CHANGELOG.md          # add a "## 1.1.0 — YYYY-MM-DD" section
   ```

2. Commit, merge through `dev-main` to `main`, and push.

3. Tag the private repository:

   ```bash
   git tag -a v1.1.0 -m '1.1.0' && git push origin v1.1.0
   ```

4. Publish:

   ```bash
   bash ~/blankee-publish.sh "Release 1.1.0"
   ```

5. Tag the public repository at the snapshot commit, and create a GitHub release
   whose body is the changelog section.

## Version numbers

`VERSION` is the single source of truth. The application reads it once at import
and shows it in the footer; the update check compares it against the published
one to say "1.0.0 → 1.1.0".

Tags are documentation. Nothing in the application or the updater consults them,
so a tag pushed a day late breaks nothing — it only makes the two histories
harder to correlate until it exists.

**Bump `VERSION` on every publish**, even for a one-line fix. It costs a line and
it is what makes the version a complete signal rather than an approximate one: an
instance that reports "up to date" while a newer commit exists is worse than one
that reports a patch bump nobody needed.

## What a release must not do

An update applies code, then dependencies, then migrations, then reloads. So for
a brief window the new schema is serving the old code, and templates on disk are
newer than the process that renders them.

**Migrations must therefore stay backward-compatible with the previous
release**: add columns, do not rename or drop them in the same version that
stops using them. Removal is a later release's job. A migration that breaks the
previous code turns an update into an outage.

Any new migration must also add its assertions to `EXPECTED_TABLES`,
`EXPECTED_COLUMNS` or `EXPECTED_CONSTRAINTS` in `install/migrate.py` in the same
commit. Those lists are what `--verify-only` checks, and a migration outside them
is one the verification silently does not cover.
