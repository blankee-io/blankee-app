-- =========================================================================
-- Migration: An Autobalance category for balance corrections
-- =========================================================================
-- Purpose: Give balance corrections a category of their own instead of
--          dropping them into Uncategorized.
--
--          Uncategorized means "this needs sorting out". A correction does not:
--          it is already as sorted as it is going to get, and it is the app's
--          entry rather than the user's. Mixed in with genuinely uncategorised
--          spending it both hides the real backlog and invites someone to
--          recategorise a figure whose whole purpose is to make two balances
--          agree.
--
--          Its own category also makes the corrections addable up. A month of
--          them trending one way is the signal that something upstream is
--          wrong, and that is only visible if they sit together.
--
-- Created for every existing user and every existing credit account, matching
-- how Uncategorized, Savings and Interest Charge are seeded at signup:
-- is_system = 1 so it cannot be deleted, is_auto_adjustment = 1 because the app
-- writes it rather than the user.
--
-- Credit accounts get their own copy per account, because c_expense_categories
-- hang off an account rather than a user - the same shape the expense category
-- sync produces for a new category.
--
-- Idempotent: each insert is guarded by NOT EXISTS, so re-running changes
-- nothing.
--
-- Run on: each environment in turn, production last
-- =========================================================================

INSERT INTO income_categories
    (user_id, name, display_order, is_recurring, is_auto_adjustment, is_system)
SELECT u.id, 'Autobalance', 0.0004, 0, 1, 1
  FROM users u
 WHERE NOT EXISTS (
        SELECT 1 FROM income_categories c
         WHERE c.user_id = u.id AND c.name = 'Autobalance');

INSERT INTO expense_categories
    (user_id, name, display_order, is_recurring, is_auto_adjustment, is_system)
SELECT u.id, 'Autobalance', 0.0004, 0, 1, 1
  FROM users u
 WHERE NOT EXISTS (
        SELECT 1 FROM expense_categories c
         WHERE c.user_id = u.id AND c.name = 'Autobalance');

INSERT INTO c_expense_categories
    (account_id, name, display_order, is_recurring, is_auto_adjustment, is_system)
SELECT a.id, 'Autobalance', 0.0004, 0, 1, 1
  FROM credit_accounts a
 WHERE NOT EXISTS (
        SELECT 1 FROM c_expense_categories c
         WHERE c.account_id = a.id AND c.name = 'Autobalance');
