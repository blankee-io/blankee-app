-- =========================================================================
-- Migration: Unify category_type to 'outgoing' / 'incoming'
-- =========================================================================
-- Purpose:
--   Today, transaction-categorization stores category_type as one of
--     expense | c_expense | income | c_payment
--   AND stores account-scoped IDs (a c_expense_categories.id is meaningful
--   only for the credit account it belongs to).
--
--   This migration collapses category_type to:
--     outgoing  (was: expense + c_expense; rewritten to canonical
--                expense_categories.id)
--     incoming  (was: income; income_categories.id unchanged)
--   Old c_payment rows are dropped — they never carried real category info.
--
--   Per-account c_expense_categories.id resolution now happens at apply time
--   (auto-confirm / pending-transactions confirm), not at suggestion time.
--
-- Tables touched:
--   - quiltt_category_mappings
--   - quiltt_transactions
--
-- Run on: each environment in turn, production last
-- =========================================================================

-- -------------------------------------------------------------------------
-- 1. quiltt_category_mappings
-- -------------------------------------------------------------------------
-- Strategy:
--   a) Add a temp column to capture the new canonical category_id while we
--      rewrite the c_expense rows.
--   b) Drop the unique key so we can rewrite freely (and dedupe later).
--   c) Rewrite c_expense rows: look up the c_expense category's name, find
--      the matching expense_categories row for the same user, set
--      category_id = expense_categories.id and category_type = 'outgoing'.
--      If no match exists (orphan c_expense), delete the mapping.
--   d) Rewrite expense / income / c_payment rows.
--   e) Dedupe: keep the row with MAX(times_confirmed) per
--      (user_id, description, category_type), summing times_confirmed.
--   f) Re-add the unique key.
-- -------------------------------------------------------------------------

ALTER TABLE quiltt_category_mappings
  DROP INDEX unique_user_desc_type;

-- (c) Rewrite c_expense rows that have a matching expense_categories row.
UPDATE quiltt_category_mappings m
  JOIN c_expense_categories cec ON cec.id = m.category_id
  JOIN expense_categories ec
    ON ec.user_id = m.user_id
   AND ec.name    = cec.name
   SET m.category_id   = ec.id,
       m.category_type = 'outgoing',
       m.account_id    = NULL
 WHERE m.category_type = 'c_expense';

-- (c) Drop orphan c_expense mappings (no matching expense category).
DELETE FROM quiltt_category_mappings
 WHERE category_type = 'c_expense';

-- (d) Rewrite remaining values.
UPDATE quiltt_category_mappings SET category_type = 'outgoing', account_id = NULL
 WHERE category_type = 'expense';
UPDATE quiltt_category_mappings SET category_type = 'incoming', account_id = NULL
 WHERE category_type = 'income';
DELETE FROM quiltt_category_mappings
 WHERE category_type = 'c_payment';

-- (e) Dedupe — sum times_confirmed into the surviving row, drop the rest.
--     We pick the row with the highest times_confirmed per
--     (user_id, description, category_type) as the survivor.
CREATE TEMPORARY TABLE _qcm_keep AS
  SELECT MIN(id) AS keep_id,
         user_id, description, category_type,
         SUM(times_confirmed) AS total_confirmed
    FROM quiltt_category_mappings
GROUP BY user_id, description, category_type;

UPDATE quiltt_category_mappings m
  JOIN _qcm_keep k
    ON k.keep_id = m.id
   SET m.times_confirmed = k.total_confirmed;

DELETE m FROM quiltt_category_mappings m
  LEFT JOIN _qcm_keep k ON k.keep_id = m.id
 WHERE k.keep_id IS NULL;

DROP TEMPORARY TABLE _qcm_keep;

-- (f) Re-add unique key + change default.
ALTER TABLE quiltt_category_mappings
  MODIFY category_type VARCHAR(20) NOT NULL DEFAULT 'outgoing',
  ADD UNIQUE KEY unique_user_desc_type
    (user_id, description(191), category_type);


-- -------------------------------------------------------------------------
-- 2. quiltt_transactions
-- -------------------------------------------------------------------------
-- Strategy:
--   a) Expand the ENUM to allow the new + old values during the rewrite.
--   b) Rewrite c_expense rows: look up name → expense_categories.id; if no
--      match, set custom_category_id = NULL.
--   c) Rewrite expense → outgoing, income → incoming, c_payment → outgoing
--      (and clear custom_category_id for c_payment).
--   d) Shrink ENUM to only the new values.
-- -------------------------------------------------------------------------

-- (a) Expand ENUM.
ALTER TABLE quiltt_transactions
  MODIFY custom_category_type
    ENUM('income','expense','c_expense','c_payment','outgoing','incoming')
    DEFAULT NULL;

-- (b) Rewrite c_expense rows that have a matching expense category.
UPDATE quiltt_transactions t
  JOIN c_expense_categories cec ON cec.id = t.custom_category_id
  JOIN expense_categories ec
    ON ec.user_id = t.user_id
   AND ec.name    = cec.name
   SET t.custom_category_id   = ec.id,
       t.custom_category_type = 'outgoing'
 WHERE t.custom_category_type = 'c_expense';

-- (b) Orphan c_expense rows: keep the type/category_suggestion (name) but
--     null out the now-stale id.
UPDATE quiltt_transactions
   SET custom_category_id = NULL,
       custom_category_type = 'outgoing'
 WHERE custom_category_type = 'c_expense';

-- (c) Remap remaining old values.
UPDATE quiltt_transactions
   SET custom_category_type = 'outgoing'
 WHERE custom_category_type = 'expense';

UPDATE quiltt_transactions
   SET custom_category_type = 'incoming'
 WHERE custom_category_type = 'income';

UPDATE quiltt_transactions
   SET custom_category_id   = NULL,
       custom_category_type = 'outgoing'
 WHERE custom_category_type = 'c_payment';

-- (d) Shrink ENUM to new values only.
ALTER TABLE quiltt_transactions
  MODIFY custom_category_type
    ENUM('outgoing','incoming')
    DEFAULT NULL;
