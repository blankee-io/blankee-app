-- =========================================================================
-- Migration: End-of-day bucket confirmation
-- =========================================================================
-- Purpose: Let a user resolve the day's forecast entries ("buckets") each
--          evening - confirming what arrived, correcting the amount, deferring
--          it, or skipping it.
--
--          Before this, nothing resolved a bucket whose day had passed. The
--          only code that converted one into a real entry lived inside
--          _sync_bank_transactions_for_user, which has no callers, ran only
--          when a bank sync had already imported transactions, and covered
--          credit expenses alone. An unconfirmed forecast therefore stayed in
--          the user's totals indefinitely.
--
-- Two changes:
--
--   users.timezone
--     The IANA zone name reported by the browser, so the prompt can fire at
--     20:00 where the user is rather than where the server is. Deliberately
--     NULL-able with no default: a guessed zone sends the prompt at the wrong
--     hour, which is worse than not sending it. NULL means "not known yet" and
--     that user is skipped.
--
--   bucket_prompts
--     One row per user per local date, recording that the evening prompt has
--     been raised. The UNIQUE key is not bookkeeping - it is the concurrency
--     control. The scheduler runs inside the web application, and the Docker
--     image serves with `gunicorn --workers 2`, so the same job wakes in two
--     processes at once. Each claims the day with INSERT IGNORE and only the
--     one that gets rowcount = 1 does the work. A check-then-act would race.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE users
  ADD COLUMN timezone VARCHAR(64) DEFAULT NULL;

CREATE TABLE bucket_prompts (
  id           INT NOT NULL AUTO_INCREMENT,
  user_id      INT NOT NULL,
  -- The user's own local date, not the server's. Two users in different zones
  -- share a prompt_date only when it is genuinely the same day for both.
  prompt_date  DATE NOT NULL,
  -- How many buckets the prompt covered, for diagnosing a quiet evening.
  bucket_count INT NOT NULL DEFAULT 0,
  -- Whether the push was accepted by APNs. The in-app prompt does not depend
  -- on it; this only records whether the nudge went out.
  pushed       TINYINT(1) NOT NULL DEFAULT 0,
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_user_prompt_date (user_id, prompt_date),
  KEY idx_prompt_date (prompt_date),
  CONSTRAINT bucket_prompts_ibfk_1 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- notifications.type
--   The evening prompt compounds: if yesterday's is still sitting unread, it
--   is replaced rather than stacked, so a week away leaves one notification
--   covering every outstanding date instead of seven saying the same thing.
--   Replacing means finding the previous one, and matching on message text
--   would break the first time the wording changed - which it already has.
ALTER TABLE notifications
  ADD COLUMN type VARCHAR(32) DEFAULT NULL;

CREATE INDEX idx_notifications_user_type ON notifications (user_id, type);
