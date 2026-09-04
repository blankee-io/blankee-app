-- =========================================================================
-- Migration: Let the user choose where a balance correction lands
-- =========================================================================
-- Purpose: Balancing writes the difference between the app and a real balance
--          as one entry, and that entry has always gone to Uncategorized.
--          That is a sensible default and a poor requirement: someone who
--          reconciles regularly wants the corrections somewhere they can see
--          them, without dredging them out of the category everything else
--          uncategorised also lands in.
--
-- TWO COLUMNS, NOT ONE
--   A correction is income when the bank holds more than the app expected and
--   an expense when it holds less, and it is written to income_entries or
--   expense_entries accordingly. One setting cannot serve both: an expense
--   category cannot hold an income entry. So the user chooses a category for
--   each direction, and either can be left alone.
--
-- NULL MEANS UNCATEGORIZED
--   Not "the id of the Uncategorized category". Storing that id would freeze a
--   decision the user never made, and it would need backfilling for every
--   existing row. NULL is the honest representation of "I have not chosen",
--   and the code resolves it to Uncategorized at the point of use - so a user
--   who never opens this setting keeps exactly the behaviour they have now.
--
-- ON DELETE SET NULL, deliberately.
--   Deleting a category the user had chosen returns the setting to the default
--   rather than leaving a dangling id. The alternative is balancing failing at
--   the moment it is needed, with an error about a category that no longer
--   exists - and a correction that does not happen is worse than one that
--   lands in Uncategorized.
--
-- NOT a new category. install/sql/remove_autobalance_categories.sql undid
-- exactly that: a dedicated Autobalance category gave the app two conventions
-- for one idea, and its is_auto_adjustment flag broke the credit branch of the
-- bank reconciliation, which picks its target by scanning for that flag. This
-- adds no category and sets no flag - it only lets the user point the existing
-- convention at a category they already have.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE autobalance_settings
  ADD COLUMN income_category_id INT DEFAULT NULL,
  ADD COLUMN expense_category_id INT DEFAULT NULL,
  ADD KEY autobalance_settings_income_fk (income_category_id),
  ADD KEY autobalance_settings_expense_fk (expense_category_id);

ALTER TABLE autobalance_settings
  ADD CONSTRAINT autobalance_settings_income_fk
      FOREIGN KEY (income_category_id) REFERENCES income_categories (id)
      ON DELETE SET NULL;

ALTER TABLE autobalance_settings
  ADD CONSTRAINT autobalance_settings_expense_fk
      FOREIGN KEY (expense_category_id) REFERENCES expense_categories (id)
      ON DELETE SET NULL;
