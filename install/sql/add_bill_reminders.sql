-- Migration: bills due today
-- A reminder, at a time of day the user chooses, naming the bills that are
-- due that day - bills only, never allowances. See bill_reminders.py.
-- Created: 2026-10-10
--
-- One row per user, created on demand. last_sent is the claim: the scheduler
-- runs in more than one process (gunicorn --workers 2 in Docker), and the
-- UPDATE that moves last_sent to today is what lets exactly one of them send.

CREATE TABLE bill_reminders (
  id            INT NOT NULL AUTO_INCREMENT,
  user_id       INT NOT NULL,
  enabled       TINYINT(1) NOT NULL DEFAULT 0,
  -- The local time of day to send it, in users.timezone.
  notify_time   TIME NOT NULL DEFAULT '08:00:00',
  -- The local date it was last sent for. The claim, not a diagnostic.
  last_sent     DATE DEFAULT NULL,
  last_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_bill_reminders_user (user_id),
  CONSTRAINT bill_reminders_ibfk_1 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
