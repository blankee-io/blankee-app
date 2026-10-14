-- Migration: recurring income calculated from pay
-- An optional, advanced part of a Wages recurring income: instead of one fixed
-- amount, each paycheck is worked out from a job - Loaf's shelf, its working
-- week, holidays and time off - as hours x rate (hourly) or a salary docked for
-- unpaid time off, times a take-home percentage. See pay_forecast.py.
-- Created: 2026-10-13
--
-- recurring_income: what this income is paid at. pay_type NULL is every
-- recurring income there is today - one fixed amount - and stays exactly that.
-- amount keeps holding a figure for every reader of one: on a pay row it is a
-- typical paycheck (a full period, no holidays or time off).
--
-- loaf_shelves: two facts about the employer's payroll that Loaf never needed -
-- how many days after a period closes the money arrives, and which way a
-- payday falling on a weekend or bank holiday moves. Loaf writes shelves
-- straight to MySQL, so these cost the flush nothing.
--
-- Backward compatible: the previous release's flush leaves the new
-- recurring_income columns out of its INSERT (they take their defaults) and
-- out of its UPDATE (they keep their values).

ALTER TABLE `recurring_income`
  ADD COLUMN `pay_type` enum('hourly','salary') DEFAULT NULL COMMENT 'NULL = a fixed amount. Otherwise each paycheck is calculated from the job.',
  ADD COLUMN `shelf_id` int DEFAULT NULL COMMENT 'loaf_shelves.id - the job. No FK: deleting a job must not delete the income.',
  ADD COLUMN `pay_rate` decimal(15,4) DEFAULT NULL COMMENT 'Gross per hour (hourly), or the gross salary for salary_period (salary).',
  ADD COLUMN `take_home_pct` decimal(12,8) DEFAULT NULL COMMENT 'Share of gross that is deposited, to 8 places - net / gross off a pay stub, unrounded. NULL = 100.',
  ADD COLUMN `overtime` tinyint(1) NOT NULL DEFAULT 0 COMMENT 'Hourly: hours over 40 in a workweek earn 1.5x.',
  ADD COLUMN `paid_holidays` tinyint(1) NOT NULL DEFAULT 0 COMMENT 'Hourly: the job''s holidays are paid.',
  ADD COLUMN `salary_period` enum('year','month') DEFAULT NULL COMMENT 'Salary: pay_rate is a yearly or a monthly figure, spread over the job''s paychecks.',
  ADD COLUMN `take_home_dismissed` decimal(12,8) DEFAULT NULL COMMENT 'A suggested take-home % the user turned down, so it is not offered again.';

ALTER TABLE `loaf_shelves`
  ADD COLUMN `pay_lag_days` int DEFAULT NULL COMMENT 'Days between a pay period ending and its pay date. NULL = 0.',
  ADD COLUMN `payday_shift` enum('before','after','none') NOT NULL DEFAULT 'before' COMMENT 'A payday on a weekend or bank holiday moves to the business day before, after, or not at all.';
