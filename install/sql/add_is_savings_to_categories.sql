-- =========================================================================
-- Migration: Add is_savings flag to income_categories and expense_categories
-- =========================================================================
-- Purpose: Identify the "Savings" category by an explicit boolean flag
--          instead of by name. Used by the Ntropy unification work to
--          exclude savings categories from outgoing-category suggestions.
--
-- Pattern:
--   - Column is TINYINT(1) DEFAULT NULL (NULL means "not the savings row")
--   - Only the row(s) with is_savings = 1 are constrained
--   - UNIQUE INDEX (user_id, is_savings) enforces "at most one savings
--     category per user", because MySQL UNIQUE indexes allow multiple NULLs
--     but only one non-NULL value per group.
--
-- Run on: each environment in turn, production last
-- =========================================================================

-- ----- expense_categories -----
ALTER TABLE expense_categories
  ADD COLUMN is_savings TINYINT(1) DEFAULT NULL;

UPDATE expense_categories
   SET is_savings = 1
 WHERE name = 'Savings';

CREATE UNIQUE INDEX idx_expense_categories_user_savings
  ON expense_categories (user_id, is_savings);

-- ----- income_categories -----
ALTER TABLE income_categories
  ADD COLUMN is_savings TINYINT(1) DEFAULT NULL;

UPDATE income_categories
   SET is_savings = 1
 WHERE name = 'Savings';

CREATE UNIQUE INDEX idx_income_categories_user_savings
  ON income_categories (user_id, is_savings);
