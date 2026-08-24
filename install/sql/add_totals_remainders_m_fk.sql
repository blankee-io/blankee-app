-- =========================================================================
-- Migration: totals_remainders_m cascade on user deletion
-- =========================================================================
-- Purpose: totals_remainders_m is the only user-scoped table with a user_id
--          column and NO foreign key to users. Its 28 siblings all have
--          ON DELETE CASCADE, so deleting an account cleans them up; this one
--          was simply left behind, silently, every time.
--
-- HOW IT WAS FOUND: comparing information_schema.COLUMNS (tables with a
--   user_id column) against KEY_COLUMN_USAGE (tables with an FK to users).
--   37 tables have user_id; 29 had a foreign key. The difference was this
--   table plus the quiltt_purge_backup_* snapshots, which are deliberate
--   backups and are meant to outlive the rows they copied.
--
-- IMPACT BEFORE THIS: dev held 5,580 orphaned rows for 93 deleted users. Not
--   merely untidy - totals_remainders_m has a unique key on (user_id, date),
--   so a new account that happened to reuse a deleted id could not be given
--   its monthly rows at all. That is exactly how this surfaced: clearing an
--   account failed on a duplicate key, because the reset deleted the user row
--   (cascading everything else) and then could not rebuild this table.
--
-- ORDER MATTERS: the orphans must go first. ADD CONSTRAINT validates existing
--   rows, so it fails outright while any row points at a missing user.
--
-- Safe to run on a live app. The DELETE only touches rows whose owner is
-- already gone, and adding the constraint changes no live behaviour beyond
-- making future deletions complete.
-- =========================================================================

-- 1. Remove rows belonging to users that no longer exist.
DELETE FROM `totals_remainders_m`
WHERE `user_id` NOT IN (SELECT `id` FROM `users`);

-- 2. Cascade from now on, matching totals_remainders and totals_remainders_d.
ALTER TABLE `totals_remainders_m`
    ADD CONSTRAINT `totals_remainders_m_ibfk_1`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
