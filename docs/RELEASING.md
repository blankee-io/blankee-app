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

## A fix to the updater lands one release late

The updater that carries out an update is the one already running: it reads and
compiles its whole source before doing anything, then replaces the tree
underneath itself. So a change to `install/blankee_update.py` is installed by
the *old* updater, and only takes effect on the following update.

That is worth remembering when a release fixes something in the update process
itself. Say so in the changelog, and give the one command that closes the gap
immediately - `sudo ./install/install.sh --units-only`, or a full installer run.

## The updater's privileges

The updater used to run as root with nothing taken away - `systemd-analyze
security blankee-update.service` read 9.6, UNSAFE - and read the *path to its
own signing key* from a file the web user owns. Both are gone, in two releases:

**1.37.0 (release A).** Still root, but sandboxed: `ProtectSystem=strict` with
only the directories it writes made writable, the capability set cut to what
the three system steps use, and the rest of the Protect*/Restrict* family. It
creates the `blankee` service user, re-owns the code tree, the virtualenv and
the WSGI file to it, and installs three root helpers - `permissions`, `units`,
`apache` - each a oneshot started by a `.path` unit watching
`/run/blankee-update/<name>.request`, each allowed to write one place. The
updater uses them only when it is not root, which in A is never.

**1.38.0 (release B).** The main units switch to `User=blankee`, an empty
capability set and `LoadCredential=` for the DB credentials. From then on root
is the three helpers only, and the updater cannot write anything it does not
own. The live error log is made group-writable (660) so the updater's lines
still reach `/admin/logs`; the web workers already held a writable descriptor
to that file, so nothing new is granted.

Two things follow that are easy to forget.

**The order is forced.** Everything in `install/` is applied by the *previous*
release's updater - see the section above. The `User=blankee` unit cannot
succeed until the user exists and the tree is chowned, and A is what does
those. Skipping A breaks the very first run of B; a machine that takes A and
never B is simply better off than before.

**The signing key is looked up where root says, not where the web user says.**
`blankee_update.py` takes the key's path from `BLANKEE_SIGNERS` in the unit
(default `/etc/blankee/allowed_signers`) and refuses a key file that is not
root's or is group/world-writable. It used to read `UPDATE_VERIFY_SIGNERS` from
`blankee.conf`, a file the web user owns - which meant a compromised web process
could blank the check or point it at a key of its own. That key is now dead;
setting it does nothing. An installation with no key pinned is updated once
unverified and seeded a key by the `units` step, so it verifies from then on.

Caveats. `ProtectHome=tmpfs` on the units means an origin reached over **SSH**
fails (no `~/.ssh/known_hosts`); the installer sets an HTTPS origin, and if you
changed it, change it back or drop that one directive. And a root shell can
still run the updater by hand: git would refuse a tree owned by someone else,
so the updater hands it a one-line global config (`GIT_CONFIG_GLOBAL`) naming
that one tree as safe - the only scope git honours `safe.directory` from - which
is sound only because the preflight has already checked its ownership.

Rolling back: the units are generated by `install/install.sh`, so reverting the
commit and running `sudo ./install/install.sh --units-only` restores the previous
units (the helper units and `/etc/tmpfiles.d/blankee-update.conf` are left
behind, harmless). The ownership change has to be reversed by hand if you go
back past 1.37.0:

```bash
sudo chown -R root:root /opt/blankee /var/www/budget_env/venv
sudo chown root:www-data /var/www/budget_env /var/www/budget_env/blankee.wsgi
```

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
