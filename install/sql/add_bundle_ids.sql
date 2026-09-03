-- =========================================================================
-- Migration: Identify bud categories and bud items by id, not by name
-- =========================================================================
-- Purpose: A bud's per-credit-card category is currently found by matching
--          c_expense_categories.name against buds.name. Name is the only
--          join key, and c_expense_categories has no uniqueness constraint,
--          so two buds called the same thing collide on one card and an
--          ordinary expense category that shares the name gets dragged
--          along by the rename sync.
--
--          Likewise bud_items.account is a varchar holding a credit card's
--          name, with the string 'deleted account' as a sentinel. Renaming
--          a card orphaned its items.
--
-- Pattern:
--   - Both new columns are nullable. NULL on c_expense_categories.bud_id
--     means "an ordinary category", which is most of the table; NULL on
--     bud_items.credit_account_id means "comes out of the cash budget",
--     which retires the 'deleted account' sentinel.
--   - UNIQUE INDEX (account_id, bud_id) is what actually makes the
--     collision impossible. MySQL allows unlimited NULLs in a UNIQUE index,
--     so ordinary categories are unaffected - the same trick
--     add_is_savings_to_categories.sql relies on.
--
-- Safe against a hydrated user: hydration overwrites Redis from MySQL
-- rather than merging into it (_hydrate_table setex's each key from a
-- fresh SELECT), and the in-process _hydrated_users set is emptied by the
-- reload that follows this migration. So the first request after reload
-- rebuilds Redis from the rows below rather than flushing over them.
--
-- Run on: each environment in turn, production last
-- =========================================================================

-- ----- 1. the mirror category points at its bud -----
ALTER TABLE c_expense_categories
  ADD COLUMN bud_id INT DEFAULT NULL,
  ADD KEY c_expense_categories_bud_fk (bud_id);

-- ----- 2. backfill from the name match in use today -----
-- MIN(b.id) settles an ambiguous match deterministically: where a user has
-- two buds of the same name, the older one claims the existing mirror. The
-- newer one gets a fresh mirror the first time it is activated after this.
UPDATE c_expense_categories c
  JOIN credit_accounts a ON c.account_id = a.id
  JOIN (
        SELECT user_id, name, MIN(id) AS bud_id
          FROM buds
         GROUP BY user_id, name
       ) b ON b.user_id = a.user_id AND b.name = c.name
   SET c.bud_id = b.bud_id
 WHERE c.is_bud = 1;

-- ----- 3. de-duplicate what the name match already collided -----
-- Keep the lowest id per (account_id, bud_id) and re-point the entries of
-- the losers onto it FIRST - c_expense_entries cascades on category delete,
-- so deleting before re-pointing would take real spending history with it.
CREATE TEMPORARY TABLE bundle_dupe_map AS
SELECT c.id AS loser_id, k.keeper_id
  FROM c_expense_categories c
  JOIN (
        SELECT account_id, bud_id, MIN(id) AS keeper_id
          FROM c_expense_categories
         WHERE bud_id IS NOT NULL
         GROUP BY account_id, bud_id
        HAVING COUNT(*) > 1
       ) k ON k.account_id = c.account_id AND k.bud_id = c.bud_id
 WHERE c.bud_id IS NOT NULL AND c.id <> k.keeper_id;

UPDATE c_expense_entries e
  JOIN bundle_dupe_map m ON e.category_id = m.loser_id
   SET e.category_id = m.keeper_id;

-- recurring_c_expense_buckets has UNIQUE (category_id, bucket_date), so a
-- loser bucket can only move across if the keeper has nothing on that date.
-- Where both have one they are two forecasts of the same spend on the same
-- day - the duplication being removed here - and the loser's copy goes with
-- its category.
UPDATE recurring_c_expense_buckets rb
  JOIN bundle_dupe_map m ON rb.category_id = m.loser_id
   SET rb.category_id = m.keeper_id
 WHERE NOT EXISTS (
       SELECT 1 FROM (SELECT category_id, bucket_date
                        FROM recurring_c_expense_buckets) existing
        WHERE existing.category_id = m.keeper_id
          AND existing.bucket_date = rb.bucket_date
       );

DELETE c FROM c_expense_categories c
  JOIN bundle_dupe_map m ON c.id = m.loser_id;

DROP TEMPORARY TABLE bundle_dupe_map;

-- ----- 4. demote mirrors that match no bud at all -----
-- Deleting these would cascade their c_expense_entries away, and those are
-- real spending. Demoting leaves the history in an ordinary category.
UPDATE c_expense_categories
   SET is_bud = 0
 WHERE is_bud = 1 AND bud_id IS NULL;

-- ----- 5. the constraint this migration exists for -----
ALTER TABLE c_expense_categories
  ADD CONSTRAINT c_expense_categories_bud_fk
      FOREIGN KEY (bud_id) REFERENCES buds (id) ON DELETE CASCADE;

CREATE UNIQUE INDEX idx_c_expense_categories_account_bud
  ON c_expense_categories (account_id, bud_id);

-- ----- 6. bud items point at a card, not at its name -----
ALTER TABLE bud_items
  ADD COLUMN credit_account_id INT DEFAULT NULL,
  ADD KEY bud_items_account_fk (credit_account_id);

UPDATE bud_items bi
  JOIN buds b ON bi.bud_id = b.id
  JOIN credit_accounts a ON a.user_id = b.user_id AND a.name = bi.account
   SET bi.credit_account_id = a.id
 WHERE bi.account IS NOT NULL
   AND LOWER(bi.account) NOT IN ('blankee', 'deleted account');

ALTER TABLE bud_items
  ADD CONSTRAINT bud_items_account_fk
      FOREIGN KEY (credit_account_id) REFERENCES credit_accounts (id) ON DELETE SET NULL;
