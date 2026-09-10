-- =========================================================================
-- Migration: holidays Loaf has never heard of
-- =========================================================================
-- Purpose: the built-in list is the US federal set plus the four an employer
--          most often adds - Good Friday, Christmas Eve, New Year's Eve and
--          the day after Thanksgiving. That covers a lot of people and not
--          everybody: a company founders' day, a floating religious
--          observance kept on a fixed date, or any national holiday outside
--          the US has nowhere to go.
--
--          So a basket can name its own, and they behave exactly like the
--          built-in ones: not bookable, and on a basket that accrues per
--          hour worked they reduce the accrual.
--
-- THE FORMAT
--   Entries separated by ';', each one 'MM-DD|Name'. Both delimiters are
--   stripped out of a name on the way in, so neither can be smuggled back to
--   split a record in two.
--
--   Comma-separated would have matched `holidays` and `weekdays` beside it,
--   and was rejected for exactly that reason: those hold single tokens with
--   no user text in them, and a holiday called "Founders, Day" would quietly
--   become two. A different delimiter says the contents are different.
--
-- ANNUAL, AND NOT SHIFTED OFF A WEEKEND
--   A month and a day, repeating every year - which is what a company
--   holiday is. There is no year, so no entry can be forgotten in January.
--
--   And unlike the fixed federal holidays, one landing on a Saturday is NOT
--   moved to the Friday. The federal rule is a real rule and worth following
--   where it applies; inventing it here would hand somebody a day off their
--   employer never gave them. A custom holiday on a weekend simply does
--   nothing, which is the honest answer and the safe direction.
--
-- Run on: each environment in turn, production last
-- =========================================================================

ALTER TABLE `loaf_baskets`
  ADD COLUMN `custom_holidays` varchar(1000) DEFAULT NULL
    COMMENT 'Holidays this employer gives that Loaf has no rule for. MM-DD|Name, separated by semicolons. Annual, and never shifted off a weekend.'
    AFTER `holidays`;
