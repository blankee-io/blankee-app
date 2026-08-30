-- Migration: Add widget_tokens table
-- Long-lived per-device tokens for the iOS home screen widget. A widget
-- extension is its own process and cannot reach the app's web view session,
-- so it authenticates with one of these instead of a cookie.
-- Created: 2026-08-29

CREATE TABLE IF NOT EXISTS `widget_tokens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  -- SHA-256 of the token. The token itself is shown once, when it is issued,
  -- and never stored: a database copy would be a password-equivalent sitting
  -- in plain text, and nothing here needs to read it back.
  `token_hash` char(64) NOT NULL,
  `label` varchar(100) DEFAULT NULL COMMENT 'Device name, so a user can tell two apart',
  `last_used_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_widget_token_hash` (`token_hash`),
  KEY `idx_widget_tokens_user` (`user_id`),
  CONSTRAINT `widget_tokens_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
