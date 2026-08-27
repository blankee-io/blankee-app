-- =========================================================================
-- Migration: Auto-balance
-- =========================================================================
-- Purpose: Let a user reconcile the app against their real bank balance on a
--          cadence they choose. On the due date they are notified; the next
--          time they open the app they are asked what their balance actually
--          is, and the difference is written as a single Uncategorized entry -
--          income when they have more than the app thinks, expense when less.
--
--          This exists because an installation with no bank feed has nothing
--          that ever corrects drift. Every forecast the user confirms by hand
--          is an opportunity to be slightly wrong, and nothing notices.
--
-- One table:
--
--   autobalance_settings
--     One row per user, created on demand. Holds whether the notification is on,
--     the cadence and time of day, and the two dates that drive it.
--
--     next_due is the scheduling mechanism, not a record of one. The scheduler
--     claims a user's turn with
--
--       UPDATE autobalance_settings
--          SET next_due = <following occurrence>, ...
--        WHERE user_id = %s AND next_due <= %s
--
--     and only the process that gets rowcount = 1 sends anything. That matters
--     because the scheduler runs inside the web application and the Docker
--     image serves with `gunicorn --workers 2`, so the job wakes in two
--     processes at once; a check-then-act would race and notify twice. Doing it
--     as one UPDATE also advances the cadence in the same statement, so a user
--     who ignores the prompt is simply asked again next time rather than
--     immediately.
--
--     pending_date is what makes the modal appear. The notification itself is
--     deliberately not persisted - unlike the evening bucket prompt, there is
--     no notifications row left behind - so this column is the only thing that
--     remembers a balance was asked for. Cleared when the user balances or
--     skips, and overwritten when the next occurrence comes round.
--
-- Run on: each environment in turn, production last
-- =========================================================================

CREATE TABLE autobalance_settings (
  id               INT NOT NULL AUTO_INCREMENT,
  user_id          INT NOT NULL,
  enabled          TINYINT(1) NOT NULL DEFAULT 0,
  -- Cadence in the same shape the recurring entries use - the settings UI is
  -- that same widget, so the stored shape has to match it.
  cadence_interval INT NOT NULL DEFAULT 2,
  cadence_unit     VARCHAR(10) NOT NULL DEFAULT 'weeks',
  -- Which days, in the same shape recurring_income uses, because the settings
  -- UI is the same widget: a comma-separated list of lowercase weekday names
  -- for a weekly cadence, and of day numbers for a monthly one. NULL for a
  -- daily cadence, and for weekly or monthly when the user picked no specific
  -- day - the anchor supplies it then.
  weekdays         VARCHAR(64) DEFAULT 'friday',
  monthly_days     VARCHAR(64) DEFAULT NULL,
  -- The local time of day to notify. The evening bucket prompt is fixed at
  -- 20:00; this one is the user's choice, so it has to be stored rather than
  -- assumed. Local to users.timezone, like every other date in this feature.
  notify_time      TIME NOT NULL DEFAULT '20:00:00',
  -- Where the series starts. Set when the user enables the feature.
  anchor_date      DATE DEFAULT NULL,
  -- The next date on or after which the user should be asked. See above: this
  -- is the claim, so it is written by the scheduler rather than read by it.
  next_due         DATE DEFAULT NULL,
  -- The local date a prompt was raised and not yet dealt with. NULL means
  -- nothing is waiting.
  pending_date     DATE DEFAULT NULL,
  -- Diagnostics: when the user last actually balanced, and by how much. A
  -- correction that keeps moving the same way means something upstream is
  -- wrong, and without this there is no way to notice.
  last_balanced    DATE DEFAULT NULL,
  last_adjustment  DECIMAL(12,2) DEFAULT NULL,
  last_modified    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_autobalance_user (user_id),
  KEY idx_autobalance_due (enabled, next_due),
  CONSTRAINT autobalance_settings_ibfk_1 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
