-- Migration: transaction import from the bank feed
--
-- custom_category_confidence gains the two values the importer writes that
-- are not Claude's three levels: 'memory' when the merchant memory chose the
-- category, 'amount' when a forecast entry of exactly this amount did. The
-- old importer wrote 'memory' into an enum that did not hold it, and under
-- STRICT_TRANS_TABLES that failed the whole row - every remembered merchant
-- silently never stored.
--
-- matched_pending_id: a transaction the bank first reported as pending may
-- post under a new id. The posted row records which pending row it replaced,
-- so the swap can be seen afterwards.
--
-- depleted_bucket: the guess is applied on arrival and consumes a forecast
-- entry at once. If the person then moves the transaction to another
-- category, that forecast has to come back - this is the snapshot it comes
-- back from.
--
-- bank_pulls: one row per user per day, claimed with INSERT IGNORE by the
-- daily pull so that only one process pulls, whatever the worker count.
-- Same shape and reason as bucket_prompts.
-- Created: 2026-09-14

ALTER TABLE `linked_transactions`
  MODIFY COLUMN `custom_category_confidence` enum('high','medium','low','memory','amount') DEFAULT NULL;

ALTER TABLE `linked_transactions`
  ADD COLUMN `matched_pending_id` varchar(255) DEFAULT NULL COMMENT 'provider id of the pending row this posted row replaced',
  ADD COLUMN `depleted_bucket` json DEFAULT NULL COMMENT 'the forecast entry the guess consumed, for restore on change';

CREATE TABLE IF NOT EXISTS `bank_pulls` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `pull_date` date NOT NULL COMMENT 'the local day the pull was for',
  `started_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finished_at` datetime DEFAULT NULL,
  `ok` tinyint(1) DEFAULT NULL,
  `fetched` int NOT NULL DEFAULT 0,
  `imported` int NOT NULL DEFAULT 0,
  `reconciled` tinyint(1) NOT NULL DEFAULT 0,
  `pushed` tinyint(1) NOT NULL DEFAULT 0,
  `error_code` varchar(32) DEFAULT NULL,
  `error_msg` varchar(500) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_user_pull_date` (`user_id`, `pull_date`),
  CONSTRAINT `bank_pulls_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
