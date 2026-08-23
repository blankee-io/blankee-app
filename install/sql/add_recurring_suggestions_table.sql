-- Migration: Add recurring_suggestions table
-- Tracks suggested recurring entries based on Ntropy enrichment of confirmed transactions
-- Created: 2026-04-02

CREATE TABLE IF NOT EXISTS `recurring_suggestions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `suggestion_type` enum('recurring_income','recurring_expense','recurring_c_expense') NOT NULL,
  `category_id` int NOT NULL,
  `transaction_id` varchar(255) NOT NULL COMMENT 'quiltt_transactions.transaction_id that triggered suggestion',
  `detected_amount` decimal(12,2) NOT NULL,
  `detected_cadence_interval` int DEFAULT NULL,
  `detected_cadence_unit` varchar(20) DEFAULT NULL COMMENT 'days, weeks, months, years',
  `detected_weekday` varchar(20) DEFAULT NULL COMMENT 'e.g. thursday',
  `detected_monthly_day` int DEFAULT NULL COMMENT 'e.g. 15',
  `dismissed` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_type_cat` (`user_id`, `suggestion_type`, `category_id`),
  KEY `idx_user_dismissed` (`user_id`, `dismissed`),
  CONSTRAINT `recurring_suggestions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
