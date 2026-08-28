-- =========================================================================
-- Migration: Remove the Autobalance category
-- =========================================================================
-- Purpose: Undo add_autobalance_category.sql. Balance corrections go where the
--          app already puts automatic adjustments.
--
--          The app has reconciled against bank balances since long before the
--          balance reminder existed - see _webhook_autobalance - and it never
--          used a category of its own. A checking adjustment goes to the user's
--          auto-adjustment category, which is Uncategorized; a credit
--          adjustment goes to that card's; and savings has no category at all,
--          only savings_adjustments rows.
--
--          Adding an Autobalance category gave the app two conventions for one
--          idea. Worse, it carried is_auto_adjustment = 1, and the credit branch
--          of the bank autobalance picks its target by scanning for that flag -
--          so which category a bank adjustment landed in would have depended on
--          the order rows came back.
--
-- Entries move before their categories go. Anything already written is the
-- difference between the app and a real balance; deleting it would put the two
-- back out of step by exactly that amount.
--
-- Idempotent: every statement no-ops once no Autobalance category remains.
--
-- Run on: each environment in turn, production last
-- =========================================================================

UPDATE income_entries e
  JOIN income_categories old ON old.id = e.category_id AND old.name = 'Autobalance'
  JOIN income_categories dest ON dest.user_id = old.user_id
                             AND dest.name = 'Uncategorized'
   SET e.category_id = dest.id;

UPDATE expense_entries e
  JOIN expense_categories old ON old.id = e.category_id AND old.name = 'Autobalance'
  JOIN expense_categories dest ON dest.user_id = old.user_id
                              AND dest.name = 'Uncategorized'
   SET e.category_id = dest.id;

UPDATE c_expense_entries e
  JOIN c_expense_categories old ON old.id = e.category_id AND old.name = 'Autobalance'
  JOIN c_expense_categories dest ON dest.account_id = old.account_id
                                AND dest.name = 'Uncategorized'
   SET e.category_id = dest.id;

DELETE FROM income_categories WHERE name = 'Autobalance';
DELETE FROM expense_categories WHERE name = 'Autobalance';
DELETE FROM c_expense_categories WHERE name = 'Autobalance';
