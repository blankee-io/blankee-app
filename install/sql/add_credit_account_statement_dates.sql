-- =========================================================================
-- Migration: Give a credit account its billing cycle
-- =========================================================================
-- Purpose: credit_accounts.interest_rate has existed since the table was
--          created and both card modals collect it, but nothing reads it -
--          so every card is projected as though it were interest-free, which
--          understates the balance by a compounding amount over a year-long
--          forecast.
--
--          A rate on its own is not enough to place a charge. Interest is
--          assessed when the billing cycle closes, and it is only charged at
--          all when the previous statement went unpaid past its due date. So
--          the account needs to know both days.
--
-- WHY VARCHAR AND NOT AN INT
--   "the last day of the month" is a real answer and is not a number - a cycle
--   closing on the 31st closes on the 30th in April. The recurring forms have
--   always handled this by storing day lists as text, with the literal
--   LAST_DAY alongside 1-31 (see recurring_expense.monthly_days and
--   _clean_monthly_days in auto_balance.py). Same convention here, so the
--   day-picker markup and its "Last Day" option are reusable rather than this
--   becoming a second spelling of the same idea.
--
-- NULL MEANS NO INTEREST
--   Not zero, and not the rate. Every existing row gets NULL and therefore
--   keeps exactly the projection it has today; the feature switches on when a
--   user fills the days in. Deciding it on the rate instead would turn it on
--   for every card already carrying one, silently, on upgrade.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE credit_accounts
  ADD COLUMN statement_day VARCHAR(8) DEFAULT NULL,
  ADD COLUMN payment_due_day VARCHAR(8) DEFAULT NULL;

-- The form has offered step="0.001" since it was written while the column kept
-- two decimals, so a rate of 19.995 has been silently stored as 20.00. Widening
-- rather than narrowing the form: 0.001 is a real precision on a promotional or
-- variable rate, and the arithmetic below is about to start depending on it.
ALTER TABLE credit_accounts
  MODIFY COLUMN interest_rate DECIMAL(6,3) DEFAULT NULL;
