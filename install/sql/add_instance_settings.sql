-- Instance-wide SMTP settings, editable from the UI.
--
-- Context: notification email credentials lived only in environment variables,
-- read once at import by email_utils.py. For a self-hosted deployment the
-- operator should be able to point notifications at their own mailbox from
-- inside the app instead of editing env files and restarting.
--
-- WHY THIS TABLE IS NOT REDIS-FIRST, unlike almost everything else here:
--   1. The Redis scheme is keyed <table>:v1:{user_id}. This row is not
--      user-scoped, so it has no place in that scheme.
--   2. It holds a credential. Redis-first would copy that credential into the
--      cache, where it would sit for the 7-day TTL.
-- add_notification() already establishes the MySQL-direct precedent for
-- exactly this kind of exception. instance_settings.py therefore reads and
-- writes this table directly and nothing needs adding to the flush worker.
--
-- Single row by construction: the primary key is pinned to 1 by a CHECK, so an
-- accidental second configuration cannot exist.
--
-- The password column holds a Fernet token, not a password. Encryption is keyed
-- by the SETTINGS_ENCRYPTION_KEY environment variable; without that key the app
-- refuses to save a password rather than writing one in plaintext.
--
-- This table is the ONLY source of mail configuration. The SMTP_* environment
-- variables are no longer read at all, so until a row exists with a server,
-- address, username and password, notification email does nothing and says so
-- in the log. That is deliberate: one place to configure, one place to look
-- when it is not working, and no chance of an instance quietly sending through
-- a mailbox nobody here chose.
--
-- Safe to run on a live app - but note that after deploying the code that goes
-- with it, any deployment that was relying on SMTP_* env vars stops sending
-- until the form is filled in.

CREATE TABLE IF NOT EXISTS `instance_settings` (
  `id` tinyint NOT NULL DEFAULT '1',
  `smtp_server` varchar(255) DEFAULT NULL,
  `smtp_port` int DEFAULT NULL,
  `smtp_username` varchar(255) DEFAULT NULL,
  `smtp_password_encrypted` text COMMENT 'Fernet token, never a plaintext password',
  `from_email` varchar(255) DEFAULT NULL COMMENT 'Also the notification recipient',
  `use_tls` tinyint(1) NOT NULL DEFAULT '1',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  CONSTRAINT `instance_settings_single_row` CHECK (`id` = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Rollback:
--   DROP TABLE IF EXISTS `instance_settings`;
-- Dropping it reverts to env-only configuration with no code change needed.
