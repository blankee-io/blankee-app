<!--
Thanks for this. The checks will tell you about the sign-off and the three
scripts; what they cannot check is the part below. See CONTRIBUTING.md.
-->

## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Why

<!-- What was wrong, or what could not be done before. Skip for a typo. -->

## How it was checked

<!--
There is no test suite, so this is the part that earns trust. Not "tested
locally" - which page you opened, what you did, what you saw.

For anything touching money or forecasts: the dates, the amounts, and the totals
before and after. For the bank importer: whether the feed was real or a stub, and
what happened to the forecast the transaction landed on.
-->

## Checklist

- [ ] Every commit is signed off (`git commit -s`)
- [ ] Writes go through Redis, not straight to MySQL
- [ ] No inline styles, and colours come from the `:root` palette
- [ ] Toasts and confirmations use `showToast` / `showConfirmModal`
- [ ] Logging goes through `log_config.py` with a tag, no `print()`
- [ ] `python3 install/check_requirements.py` passes
- [ ] `python3 install/build_fa_fallback.py --check` passes

If this adds a migration:

- [ ] SQL file in `install/sql/`
- [ ] listed in `MIGRATIONS` in `install/migration_manifest.py`
- [ ] asserted in `EXPECTED_*` in `install/migrate.py`
- [ ] `python3 install/check_migrations.py` passes
- [ ] backward-compatible with the previous release (adds, does not rename or drop)
