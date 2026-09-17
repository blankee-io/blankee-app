-- Migration: a bank transaction that is an entry the person already recorded
--
-- custom_category_confidence gains 'record': the transaction matched an entry
-- the person had typed or confirmed themselves (same amount, within a few
-- days), so the importer took that entry over instead of writing a second
-- one and consuming the category's next forecast for it. A bill confirmed by
-- hand on the 14th and posted by the bank on the 16th used to count twice -
-- and eat October's forecast for September's bill.
-- Created: 2026-09-16

ALTER TABLE `linked_transactions`
  MODIFY COLUMN `custom_category_confidence` enum('high','medium','low','memory','amount','record') DEFAULT NULL;
