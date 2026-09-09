-- =========================================================================
-- Migration: an entry can add hours as well as spend them
-- =========================================================================
-- Purpose: loaf_entries has only ever recorded time off - a range, costed
--          against the working week, taken off the balance. Hours arrive by
--          exactly two routes, an accrual on a pay date and a grant when the
--          year turns, and both are properties of the basket rather than
--          things a person can record.
--
--          That leaves nowhere to put "my manager gave me eight hours" or
--          "payroll corrected last month". The only workaround is to edit
--          starting_hours, which re-dates the whole projection to today and
--          loses every reason for the change.
--
-- WHAT THE COLUMN HOLDS
--   Which way the entry moves the balance. 'use' is everything the table has
--   held until now and is the default, so every existing row keeps its
--   meaning without being touched. 'accrue' is a credit: a figure on a date,
--   added rather than subtracted.
--
-- WHY AN ENUM AND NOT A NEGATIVE `hours`
--   Storing a credit as hours = -8 needs no migration at all and was the
--   obvious shortcut. It was rejected: the column's comment says what is
--   deducted, a signed figure makes every SUM and every display ambiguous,
--   and absence_by_date would read the negative as anti-absence and push
--   hours worked above hours scheduled. An enum says the thing out loud and
--   lets each reader decide what to do about it - the projection adds it, the
--   accrual pro-rate ignores it entirely, because being given hours is not
--   the same as being at work.
--
-- WHY IT IS NOT A THIRD `status`
--   status already answers a different question - planned, taken, cancelled -
--   and a cancelled accrual is a perfectly sensible thing to want. Folding
--   direction into it would make the two unrepresentable together.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE `loaf_entries`
  ADD COLUMN `direction` enum('use','accrue') NOT NULL DEFAULT 'use'
    COMMENT 'use spends hours, accrue adds them. An accrual is one date and a figure, never costed against the working week.'
    AFTER `status`;
