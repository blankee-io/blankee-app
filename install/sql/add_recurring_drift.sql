-- Migration: a recurring_mismatches row records the habit itself
-- It used to point at a bank enrichment provider's verdict that a merchant was
-- recurring; that provider is gone. Now it records what came through and when,
-- once the same miss has been confirmed twice - see recurring_drift.py.
-- transaction_id becomes optional: the evening prompt has no bank row.
-- Created: 2026-09-21

ALTER TABLE `recurring_mismatches`
  MODIFY COLUMN `transaction_id` varchar(255) DEFAULT NULL COMMENT 'the bank row that triggered it, when there was one',
  ADD COLUMN `entry_id` int DEFAULT NULL COMMENT 'the confirmed entry that triggered it',
  ADD COLUMN `detected_amount` decimal(15,2) DEFAULT NULL COMMENT 'what came through, when it differs from the template',
  ADD COLUMN `detected_shift` int DEFAULT NULL COMMENT 'days from the template day, when beyond the slack',
  ADD COLUMN `expected_date` date DEFAULT NULL COMMENT 'the template day of the occurrence that triggered it',
  ADD COLUMN `observed_date` date DEFAULT NULL COMMENT 'the day it actually came through';
