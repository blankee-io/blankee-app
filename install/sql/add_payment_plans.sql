-- Migration: a credit account's payment plan, remembered
-- The plan a person applied to a card (1.62.0): what they asked for - the
-- most they can pay a month, or how many months - and what it came to. Kept
-- so the card can be checked against it whenever its balances are worked out
-- again, and the person told once when charges or payments the plan did not
-- expect mean it no longer pays the card off; and so the plan dialog opens on
-- what they chose before. See _check_payment_plans in app.py.
-- Created: 2026-10-18
--
-- One row per card, replaced when a new plan is applied and removed when the
-- plan has run its course or the card is deleted. account_id carries no
-- foreign key: a card's id can be a temporary one in Redis until the flush,
-- and a row left by a card that has gone is removed by the check itself.
--
-- Backward compatible: a new table, which the previous release never reads.

CREATE TABLE payment_plans (
  id            INT NOT NULL AUTO_INCREMENT,
  user_id       INT NOT NULL,
  account_id    INT NOT NULL,
  -- 'payment': value is the most a month; 'months': value is the months.
  mode          ENUM('payment','months') NOT NULL,
  value         DECIMAL(15,2) NOT NULL,
  -- What the plan came to.
  payment       DECIMAL(15,2) NOT NULL,
  months        INT NOT NULL,
  pay_day       VARCHAR(8) NOT NULL,
  first_date    DATE NOT NULL,
  last_date     DATE NOT NULL,
  -- Whether the person has been told it is off. Set when the notification
  -- goes out, cleared when the card is back on the plan, so they are told
  -- once each time it goes off rather than on every recalculation.
  notified      TINYINT(1) NOT NULL DEFAULT 0,
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_modified TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_payment_plans_account (user_id, account_id),
  CONSTRAINT payment_plans_ibfk_1 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
