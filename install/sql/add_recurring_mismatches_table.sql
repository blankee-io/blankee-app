-- Migration: Add recurring_mismatches table
-- Tracks detected mismatches between Quiltt/Ntropy transaction data and user's recurring entries
-- Created: 2026-04-01

CREATE TABLE IF NOT EXISTS `recurring_mismatches` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `recurring_table` enum('recurring_income','recurring_expense','recurring_c_expense') NOT NULL,
  `recurring_id` int NOT NULL,
  `category_id` int NOT NULL,
  `transaction_id` varchar(255) NOT NULL COMMENT 'quiltt_transactions.transaction_id that triggered detection',
  `dismissed` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_mismatch` (`user_id`, `recurring_table`, `recurring_id`),
  KEY `idx_user_dismissed` (`user_id`, `dismissed`),
  CONSTRAINT `recurring_mismatches_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
