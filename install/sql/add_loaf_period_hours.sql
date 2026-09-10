-- =========================================================================
-- Migration: the hours an employer counts in a pay period
-- =========================================================================
-- Purpose: a basket that accrues per hour worked pro-rates by
--          worked / scheduled, and Loaf works `scheduled` out by walking the
--          working week across the period. That is the honest answer to
--          "how many hours were in this period" and it is not the one
--          payroll uses.
--
--          A semi-monthly period has ten, eleven or twelve weekdays in it
--          depending on the month, so Loaf's denominator swings between 80
--          and 96 hours. Employers overwhelmingly use one flat figure -
--          2080/24 = 86.67 for semi-monthly, 2080/26 = 80 for fortnightly -
--          and apply it to every period of the year.
--
--          The difference is small per period and compounds. On a real
--          ledger checked against this, sixteen periods drifted about five
--          and a half hours, always in the employee's favour.
--
-- WHAT IT DOES
--   Set it, and it replaces the walked figure as the denominator. Absence
--   is still counted from the calendar - real hours away on real days - so
--   only the divisor changes. That is exactly the shape payroll uses:
--
--       earned = rate x (period_hours - absent) / period_hours
--
--   With 86.67 and the 6.65 rate that ledger showed, 8 hours away gives
--   6.03 and 24 hours away gives 4.81. Both are what the ledger says, to
--   the penny, where the walked denominator gave 6.10 and 4.99.
--
-- NULL MEANS WALK THE WEEK
--   Which is what every existing basket does today, so nothing changes for
--   anybody who does not set it. It is also the right default: a figure
--   nobody has been told cannot be guessed, and guessing 86.67 for someone
--   paid fortnightly would be wrong by eight percent.
--
-- ONLY WHEN THE ACCRUAL IS PER HOUR WORKED
--   A flat accrual has no denominator to disagree about, so this is ignored
--   there - and the form only offers it once the basis says otherwise.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE `loaf_baskets`
  ADD COLUMN `period_hours` decimal(7,2) DEFAULT NULL
    COMMENT 'The hours the employer counts in one pay period, used as the pro-rate denominator. NULL walks the working week instead. Ignored on a flat accrual.'
    AFTER `accrual_basis`;
