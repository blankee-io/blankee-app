-- =========================================================================
-- Migration: how a basket accrues, and which days the office is shut
-- =========================================================================
-- Purpose: Loaf has only ever accrued one way - a rate pro-rated by the hours
--          actually worked, so any absence shrinks the next accrual. That is
--          a real policy and a common one for hourly staff, but it is the
--          minority case: most salaried people accrue the same figure every
--          pay period whether they took leave or not.
--
--          Loaf was therefore wrong for the majority, and wrong in a way
--          nobody would notice - it under-credits, so the forecast is merely
--          pessimistic rather than visibly broken, and it drifts further the
--          more leave you take.
--
-- accrual_basis
--   'flat'   the same figure every period. The default, because it is what
--            most employers do, and because it cannot be subtly wrong: there
--            is no denominator to disagree about.
--   'worked' the rate pro-rated by hours worked, which is everything Loaf did
--            before. EXISTING ROWS ARE SET TO THIS by the statement below,
--            not left on the new default - a migration must not silently
--            change the numbers a person is already looking at.
--
-- holidays
--   Which public holidays this basket's employer closes for, as a list of
--   slugs, in the same shape as `weekdays` and `accrual_only_weekdays`. Empty
--   means Loaf does not know about any, which is what every existing row gets
--   and what keeps their forecasts identical.
--
--   A holiday is not a day off you booked - it is a day the office was shut.
--   So it costs nothing to book and cannot be spent, and on a 'worked' basket
--   it reduces the accrual exactly as absence does, because a paid holiday is
--   not an hour worked. On a 'flat' basket it changes no arithmetic at all
--   and only stops the day being booked.
--
-- WHY SLUGS AND NOT DATES
--   Christmas is the 25th but Thanksgiving is the fourth Thursday, and both
--   move when they land on a weekend. Storing the rule and computing the date
--   per year is the only version that stays right in 2031 - see
--   loaf_holidays.py. A stored date would need re-entering every January.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE `loaf_baskets`
  ADD COLUMN `accrual_basis` enum('flat','worked') NOT NULL DEFAULT 'flat'
    COMMENT 'flat accrues the same every period; worked pro-rates it by the hours actually worked.'
    AFTER `accrual_hours`,
  ADD COLUMN `holidays` varchar(255) DEFAULT NULL
    COMMENT 'Public holidays the office closes for, as slugs. Not bookable, and on a worked basis they reduce the accrual.'
    AFTER `accrual_only_weekdays`;

-- Everything that exists today was built under the pro-rated rule and is
-- showing figures based on it. Keep them there; the new default is for
-- baskets nobody has made yet.
UPDATE `loaf_baskets` SET `accrual_basis` = 'worked';
