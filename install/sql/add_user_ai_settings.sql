-- Migration: per-user AI categorization settings
-- Each user's own Anthropic API key (Fernet-encrypted with
-- SETTINGS_ENCRYPTION_KEY), the model, and the verification record: a
-- fingerprint of key+model taken when a test call succeeded, so changing
-- either un-verifies by arithmetic. NOT Redis-first - a credential must not
-- sit in a 7-day cache. Whether the user has switched the feature ON lives
-- on users.ai_categorization, which is Redis-first like every user setting.
-- Created: 2026-09-13

CREATE TABLE IF NOT EXISTS `user_ai_settings` (
  `user_id` int NOT NULL,
  `api_key_encrypted` text COMMENT 'Fernet token, never plaintext',
  `model` varchar(64) NOT NULL DEFAULT 'claude-haiku-4-5-20251001',
  `verified_fingerprint` char(64) DEFAULT NULL,
  `verified_at` datetime DEFAULT NULL,
  `last_error` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `user_ai_settings_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
