-- =========================================================================
-- Migration: Buds become Bundles, and are identified by id rather than name
-- =========================================================================
-- Purpose: Two things at once, in one file, because the order between them
--          matters and splitting them makes a fresh install go wrong.
--
--          1. The rename. "Bud" never said what the feature was - a named,
--             itemised list of planned one-off spends - and the storage
--             carried the word too, so leaving it behind meant renaming the
--             tables and columns as well as the screens.
--
--          2. The identity fix. A bud's per-credit-card category was found by
--             matching c_expense_categories.name against buds.name. Name was
--             the only join key and that table has no uniqueness constraint,
--             so two buds called the same thing collided on one card, and an
--             ordinary expense category sharing the name got dragged along by
--             the rename sync.
--
-- WHY RENAMES COME FIRST
--   apply_file runs every migration with --force, so a statement that is
--   already satisfied fails harmlessly and the rest continues. That is what
--   makes a migration safe to re-run, and it is also what a fresh install
--   relies on: schema.sql is kept current, so every ALTER here fails against
--   it and only verify() decides whether the end state is right.
--
--   That only holds if each statement is a no-op on the final schema. Adding
--   bud_id first would NOT be: on a fresh install the column is already there
--   under its new name, so "ADD COLUMN bud_id" would succeed and leave a
--   second, junk column behind. Renaming first and then adding under the new
--   name means every statement is either a real step (upgrade) or a harmless
--   failure (fresh install), on both paths.
--
-- WHY A DATA-REPAIRING MIGRATION IS SAFE HERE
--   Hydration overwrites Redis from MySQL rather than merging into it -
--   _hydrate_table setex's each key from a fresh SELECT - and the in-process
--   _hydrated_users set is emptied by the reload that follows this migration.
--   So the first request afterwards rebuilds Redis from the rows below rather
--   than flushing stale ones over them.
--
-- ORDERED LAST IN MIGRATIONS, deliberately: between this running and the
-- application reloading, the old process is querying tables that no longer
-- exist under the names it knows. Keeping the window as short as possible is
-- the whole reason this is the final file.
--
-- Run on: each environment in turn, production last
-- =========================================================================

-- ----- 1. the tables -----
RENAME TABLE `buds` TO `bundles`;
RENAME TABLE `bud_items` TO `bundle_items`;

-- ----- 2. the columns -----
ALTER TABLE `bundle_items`      RENAME COLUMN `bud_id`       TO `bundle_id`;
ALTER TABLE `expense_categories`   RENAME COLUMN `is_bud`      TO `is_bundle`;
ALTER TABLE `c_expense_categories` RENAME COLUMN `is_bud`      TO `is_bundle`;
ALTER TABLE `expense_entries`   RENAME COLUMN `bud_item_id` TO `bundle_item_id`;
ALTER TABLE `c_expense_entries` RENAME COLUMN `bud_item_id` TO `bundle_item_id`;

-- ----- 3. the constraint names -----
-- Renaming a constraint means dropping and re-adding it. The FK is recreated
-- immediately, so nothing is unprotected for longer than this statement pair.
--
-- EXPECTED NOISE: the first three pairs normally fail, and that is correct.
-- RENAME TABLE already renamed those constraints - InnoDB rewrites a foreign
-- key called <old_table>_ibfk_N to <new_table>_ibfk_N when the table moves - so
-- the DROP reports "Can't DROP" and the ADD reports "Duplicate foreign key
-- constraint name". They are kept anyway: they cost nothing when MySQL has
-- already done the work, and they are the safety net if a version ever does
-- not. verify() is what decides whether the end state is right.
--
-- The last two pairs are NOT redundant. They sit on expense_entries and
-- c_expense_entries, which are not renamed here, and their names do not follow
-- the _ibfk_N convention - so nothing renames them automatically.
ALTER TABLE `bundle_items` DROP FOREIGN KEY `bud_items_ibfk_1`;
ALTER TABLE `bundle_items`
  ADD CONSTRAINT `bundle_items_ibfk_1`
      FOREIGN KEY (`bundle_id`) REFERENCES `bundles` (`id`) ON DELETE CASCADE;

ALTER TABLE `bundles` DROP FOREIGN KEY `buds_ibfk_1`;
ALTER TABLE `bundles`
  ADD CONSTRAINT `bundles_ibfk_1`
      FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;

ALTER TABLE `bundles` DROP FOREIGN KEY `buds_ibfk_2`;
ALTER TABLE `bundles`
  ADD CONSTRAINT `bundles_ibfk_2`
      FOREIGN KEY (`expense_category_id`) REFERENCES `expense_categories` (`id`)
      ON DELETE SET NULL;

ALTER TABLE `expense_entries` DROP FOREIGN KEY `expense_entries_ibfk_bud_item`;
ALTER TABLE `expense_entries`
  ADD CONSTRAINT `expense_entries_ibfk_bundle_item`
      FOREIGN KEY (`bundle_item_id`) REFERENCES `bundle_items` (`id`) ON DELETE CASCADE;

ALTER TABLE `c_expense_entries` DROP FOREIGN KEY `c_expense_entries_ibfk_bud_item`;
ALTER TABLE `c_expense_entries`
  ADD CONSTRAINT `c_expense_entries_ibfk_bundle_item`
      FOREIGN KEY (`bundle_item_id`) REFERENCES `bundle_items` (`id`) ON DELETE CASCADE;

-- ----- 4. the mirror category points at its bundle -----
-- ON DELETE CASCADE, on the many side: one bundle has one mirror per card.
-- c_expense_categories has no user_id and is not getting one; ownership
-- already runs account_id -> credit_accounts.user_id.
ALTER TABLE `c_expense_categories`
  ADD COLUMN `bundle_id` INT DEFAULT NULL,
  ADD KEY `c_expense_categories_bundle_fk` (`bundle_id`);

-- ----- 5. bundle items point at a card, not at its name -----
-- NULL reads as the cash budget, which retires the 'deleted account' sentinel.
ALTER TABLE `bundle_items`
  ADD COLUMN `credit_account_id` INT DEFAULT NULL,
  ADD KEY `bundle_items_account_fk` (`credit_account_id`);

-- ----- 6. backfill from the name match in use today -----
-- MIN(b.id) settles an ambiguous match deterministically: where a user has two
-- bundles of one name, the older claims the existing mirror. The newer gets a
-- fresh mirror the first time it is activated after this.
UPDATE `c_expense_categories` c
  JOIN `credit_accounts` a ON c.account_id = a.id
  JOIN (
        SELECT user_id, name, MIN(id) AS bundle_id
          FROM `bundles`
         GROUP BY user_id, name
       ) b ON b.user_id = a.user_id AND b.name = c.name
   SET c.bundle_id = b.bundle_id
 WHERE c.is_bundle = 1 AND c.bundle_id IS NULL;

UPDATE `bundle_items` bi
  JOIN `bundles` b ON bi.bundle_id = b.id
  JOIN `credit_accounts` a ON a.user_id = b.user_id AND a.name = bi.account
   SET bi.credit_account_id = a.id
 WHERE bi.credit_account_id IS NULL
   AND bi.account IS NOT NULL
   AND LOWER(bi.account) NOT IN ('blankee', 'deleted account');

-- ----- 7. de-duplicate what the name match already collided -----
-- Entries are re-pointed onto the surviving category FIRST: c_expense_entries
-- cascades on category delete, so deleting before re-pointing would take real
-- spending history with it.
DROP TEMPORARY TABLE IF EXISTS bundle_dupe_map;
CREATE TEMPORARY TABLE bundle_dupe_map AS
SELECT c.id AS loser_id, k.keeper_id
  FROM `c_expense_categories` c
  JOIN (
        SELECT account_id, bundle_id, MIN(id) AS keeper_id
          FROM `c_expense_categories`
         WHERE bundle_id IS NOT NULL
         GROUP BY account_id, bundle_id
        HAVING COUNT(*) > 1
       ) k ON k.account_id = c.account_id AND k.bundle_id = c.bundle_id
 WHERE c.bundle_id IS NOT NULL AND c.id <> k.keeper_id;

UPDATE `c_expense_entries` e
  JOIN bundle_dupe_map m ON e.category_id = m.loser_id
   SET e.category_id = m.keeper_id;

-- recurring_c_expense_buckets has UNIQUE (category_id, bucket_date), so a
-- loser bucket can only move across if the keeper has nothing on that date.
-- Where both have one they are two forecasts of the same spend on the same day
-- - the duplication being removed here - and the loser's copy goes with its
-- category.
UPDATE `recurring_c_expense_buckets` rb
  JOIN bundle_dupe_map m ON rb.category_id = m.loser_id
   SET rb.category_id = m.keeper_id
 WHERE NOT EXISTS (
       SELECT 1 FROM (SELECT category_id, bucket_date
                        FROM `recurring_c_expense_buckets`) existing
        WHERE existing.category_id = m.keeper_id
          AND existing.bucket_date = rb.bucket_date
       );

DELETE c FROM `c_expense_categories` c
  JOIN bundle_dupe_map m ON c.id = m.loser_id;

DROP TEMPORARY TABLE IF EXISTS bundle_dupe_map;

-- ----- 8. demote mirrors that match no bundle at all -----
-- Deleting these would cascade their c_expense_entries away, and those are real
-- spending. Demoting leaves the history in an ordinary category.
UPDATE `c_expense_categories`
   SET is_bundle = 0
 WHERE is_bundle = 1 AND bundle_id IS NULL;

-- ----- 9. the constraints this migration exists for -----
ALTER TABLE `c_expense_categories`
  ADD CONSTRAINT `c_expense_categories_bundle_fk`
      FOREIGN KEY (`bundle_id`) REFERENCES `bundles` (`id`) ON DELETE CASCADE;

-- MySQL allows unlimited NULLs in a UNIQUE index, so ordinary categories are
-- untouched - the same trick add_is_savings_to_categories.sql relies on.
CREATE UNIQUE INDEX `idx_c_expense_categories_account_bundle`
  ON `c_expense_categories` (`account_id`, `bundle_id`);

ALTER TABLE `bundle_items`
  ADD CONSTRAINT `bundle_items_account_fk`
      FOREIGN KEY (`credit_account_id`) REFERENCES `credit_accounts` (`id`)
      ON DELETE SET NULL;

-- ----- 10. the notification kind keeps its opt-outs -----
-- users.email_notify_disabled is a CSV of kind slugs. Renaming the kind from
-- 'buds' to 'bundles' without rewriting it would silently re-enable the mail
-- for everyone who had turned it off - the opt-out would still be there,
-- naming a kind that no longer exists.
--
-- FIND_IN_SET and the comma-wrapped REPLACE together match the whole element
-- and nothing else: a plain REPLACE would also rewrite a slug that merely
-- contained "buds".
UPDATE `users`
   SET `email_notify_disabled` = TRIM(BOTH ',' FROM
         REPLACE(CONCAT(',', `email_notify_disabled`, ','), ',buds,', ',bundles,'))
 WHERE FIND_IN_SET('buds', `email_notify_disabled`);
