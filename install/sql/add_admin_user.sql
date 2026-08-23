-- =========================================================================
-- Migration: administrator flag on users
-- =========================================================================
-- Purpose: the deployment gets one administrator - the first account created -
--          who is the only one who can reach the admin console, create other
--          users, and configure email delivery. Self-registration closes as
--          soon as that account exists.
--
-- WHY A COLUMN rather than "the lowest user id":
--   Implicit rules break quietly. If the first account were ever deleted,
--   "lowest id" would silently promote whoever happened to be next, which is a
--   privilege escalation nobody asked for. An explicit flag can only change
--   when something sets it.
--
-- SAFE AGAINST THE REDIS FLUSH: redis_manager's users flush is a fixed
--   "UPDATE users SET ..." column list (redis_manager.py:2838) which does not
--   mention is_admin, so the flush cannot overwrite it. That is the failure
--   mode CLAUDE.md warns about for new users columns, and the reason this flag
--   is written MySQL-direct at creation and never cached.
--
-- BACKFILL: an existing deployment already has users but no administrator, and
--   with registration closed nobody could ever reach the console. So the
--   lowest-id existing account is promoted. On a fresh database this matches
--   zero rows and does nothing, which is correct - the first registration will
--   set the flag itself.
--
-- Pattern: plain ALTER, like the other add_* migrations. Re-running fails with
--          "Duplicate column name", which is noise rather than damage. Check:
--            SELECT id, username, is_admin FROM users WHERE is_admin = 1;
-- =========================================================================

ALTER TABLE `users`
    ADD COLUMN `is_admin` tinyint(1) NOT NULL DEFAULT 0
        COMMENT 'Exactly one administrator: the first account created';

-- Promote the earliest existing account, if any. Ordered by id because that is
-- creation order here - member_since is NULL until profile setup, so it cannot
-- be used for this.
UPDATE `users`
SET `is_admin` = 1
WHERE `id` = (SELECT MIN(`id`) FROM (SELECT `id` FROM `users`) AS u)
  AND NOT EXISTS (SELECT 1 FROM (SELECT `id` FROM `users` WHERE `is_admin` = 1) AS a);
