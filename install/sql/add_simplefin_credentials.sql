-- Migration: SimpleFIN credentials, one row per user
-- The user's SimpleFIN Bridge Access URL (Basic-Auth credentials embedded),
-- Fernet-encrypted with SETTINGS_ENCRYPTION_KEY - the same key and the same
-- rule as the SMTP password: never plaintext, never in Redis, never logged.
-- The rest of the row is the request ledger the Bridge's daily budget needs.
-- Deliberately NOT Redis-first: a credential must not sit in a 7-day cache.
-- Created: 2026-09-13

CREATE TABLE IF NOT EXISTS `simplefin_credentials` (
  `user_id` int NOT NULL,
  `access_url_encrypted` text COMMENT 'Fernet token of the access URL, never plaintext',
  `claimed_at` datetime DEFAULT NULL,
  `last_pull_at` datetime DEFAULT NULL,
  `last_pull_ok` tinyint(1) DEFAULT NULL,
  `last_error_code` varchar(32) DEFAULT NULL COMMENT 'SimpleFIN errlist code, http.<status>, network, quota',
  `last_error_msg` varchar(500) DEFAULT NULL,
  `pulls_day` date DEFAULT NULL COMMENT 'The day pulls_today counts',
  `pulls_today` int NOT NULL DEFAULT 0,
  `quota_warned_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `simplefin_credentials_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
