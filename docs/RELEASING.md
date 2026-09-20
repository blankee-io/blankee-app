# Releasing

One repository, public: `blankee-io/blankee-app`. Work reaches `main` through
pull requests, squash-merged. **A release is a signed tag**, not a commit on
`main`: an installation follows the newest `v*` tag that verifies against the key
it pinned when it was installed, so an ordinary merge changes nothing for anybody
until a tag says otherwise.

Until 1.45.0 this worked the other way round. Development happened in a private
repository, `~/blankee-publish.sh` snapshotted it here as one squashed commit per
release, and installations tracked the tip of `main` - so every commit on `main`
*was* a release. The private history was purged of what could not be published
and grafted onto this repository, which is why `git log` goes back further than
the snapshots do. The publish script is no longer used.

## Cutting a release

1. On `main`, with everything that is going out already merged, set the version
   and describe it:

   ```bash
   printf '1.45.1\n' > VERSION
   $EDITOR CHANGELOG.md          # add a "## 1.45.1 — YYYY-MM-DD" section
   ```

2. Commit and push. Commits on `main` must be signed - the branch rule requires
   it, and GitHub signs what it creates when a pull request is squash-merged.

3. Tag it with the **release key**, and push the tag:

   ```bash
   git tag -s -a v1.45.1 -m '1.45.1' && git push origin v1.45.1
   ```

   That signature is the whole trust anchor: an installation verifies the tag
   against its pinned `allowed_signers` and refuses anything else. A tag signed
   with some other key, or not signed at all, is skipped by the updater and
   logged as skipped - it is not a release.

4. Nothing else. `.github/workflows/release.yml` fires on `v*` and builds the
   release page from the changelog section as it stood at the tag.

## Version numbers

`VERSION` is the single source of truth for what an instance reports. The
application reads it once at import and shows it in the footer; the update check
reads `VERSION` **at the tag** to say "1.45.0 → 1.45.1", so a tag whose name and
`VERSION` disagree makes the admin console state something untrue.

**Bump `VERSION` on every release**, even for a one-line fix. It costs a line and
it is what makes the version a complete signal rather than an approximate one: an
instance that reports "up to date" while a newer release exists is worse than one
that reports a patch bump nobody needed.

Tags are no longer documentation - **the tag is the release**. A tag pushed a day
late is a release a day late, and until it exists nothing is offered to anyone.
Pre-release names (`v1.46.0-rc.1`) are skipped by the updater on purpose, so one
can be pushed freely for somebody to check out by hand.

## Installing from a release

An operator who clones and installs without checking out a tag installs whatever
was merged last. Both paths in the README check out the newest tag first, and
`install/install.sh` says so when it is run from a branch. After that the updater
keeps the instance on tags by itself.

## The move to tags, and why `main` was frozen for a while

The updater that applies an update is the one already running (see the next
section), so the tag-following code shipped in 1.45.0 was itself installed by a
tip-following updater and only took effect afterwards. Until every installation
had 1.45.0, a commit landing on `main` would have been offered to the stragglers
as an update - so `main` stayed frozen to releases until the known installations
reported 1.45.0. That freeze is history, recorded here so it is not mistaken for
a rule that still applies.

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
to that file, so nothing new is granted. 1.38.2 made `blankee.conf` 660 as
well: the updater consumes a request by writing `UPDATE_REQUESTED=0` back into
that file in place, and at 640 it could not, so the timer re-ran the same
request every minute.

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
