-- =========================================================================
-- Migration: a shelf holds the job, baskets hold the hours
-- =========================================================================
-- Purpose: a basket carries both a pool of hours AND the employment those
--          hours belong to - the working week, the holidays, the pay
--          cadence, the hours payroll counts in a period. That conflation
--          is fine while somebody has one basket and breaks as soon as
--          they have two.
--
--          add_loaf_tables.sql anticipated this: "If shared schedule
--          templates are ever wanted, the escape is a loaf_schedules table
--          plus a schedule_id; these columns are contiguous." This is that
--          escape, taken for a reason sharper than templates.
--
-- WHAT WENT WRONG WITHOUT IT
--   A real employer runs two pools against one job: PTO accrued per hour
--   worked, and UTO granted once a year. Both reduce hours worked, so both
--   have to feed the accrual denominator - and loaf_forecast's
--   absence_by_date already spans every basket for exactly that reason:
--
--       "it deliberately does not care which basket the time came out of:
--        an hour not worked is an hour not worked"
--
--   That is right only while every basket is the same job, and nothing
--   enforced it. Two failures followed, both silent:
--
--     1. Each entry is costed against ITS OWN basket's week, so a PTO/UTO
--        pair only costs correctly while both weeks are kept identical by
--        hand. Nothing checked.
--     2. A second job would suppress the first one's accrual, because
--        every basket counted toward every other.
--
--   A shelf is the job. Absence is scoped to it and the week is read from
--   it, so neither can happen.
--
-- ONE SHELF PER EXISTING BASKET
--   The backfill is 1:1, so behaviour the day this lands is identical to
--   the day before. Merging two baskets onto one shelf is then something
--   the owner does deliberately, not something a migration guesses at.
--
--   The shelf takes the basket's name, because that is the only name there
--   is. "PTO" is a poor name for a job and the owner will rename it; a
--   generic "My job" would throw away the one piece of information
--   available.
--
-- source_basket_id IS KEPT, NOT DROPPED
--   It records which basket a shelf was split out of. Cheap, nullable, and
--   the only way to answer "where did this come from" once the duplicate
--   columns on loaf_baskets are dropped in a later release.
--
-- shelf_id IS NULLABLE THIS RELEASE
--   An update applies schema before code, so for a moment the new columns
--   serve the previous release. NOT NULL here would make that window an
--   outage. It is tightened once nothing writes the old shape.
--
-- THE OLD COLUMNS STAY ON loaf_baskets
--   Per docs/RELEASING.md a migration must stay backward-compatible with
--   the previous release: add columns, do not drop them in the version
--   that stops using them. Their removal is a later release's job.
--
-- Run on: each environment in turn, production last
-- =========================================================================

CREATE TABLE `loaf_shelves` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `name` varchar(255) NOT NULL,
  `source_basket_id` int DEFAULT NULL
    COMMENT 'The basket this shelf was split out of when shelves arrived. Provenance only.',

  -- how the employer counts a period
  `period_hours` decimal(7,2) DEFAULT NULL
    COMMENT 'Hours the employer counts in one pay period, the pro-rate denominator. NULL walks the working week instead.',

  -- when pay lands
  `cadence_interval` int NOT NULL DEFAULT '2',
  `cadence_unit` enum('days','weeks','months','years') NOT NULL DEFAULT 'weeks',
  `weekdays` varchar(255) DEFAULT NULL COMMENT 'Days PAY lands, lowercase names. Not the work week.',
  `monthly_days` varchar(255) DEFAULT NULL COMMENT '1-31 and/or the literal Last Day',
  `yearly_day` int DEFAULT NULL,
  `yearly_month` int DEFAULT NULL,
  `accrual_anchor_date` date DEFAULT NULL COMMENT 'Which week a bi-weekly period lands on',

  -- When the leave year turns. The carryover BEHAVIOUR stays on the basket:
  -- one job has one year boundary, but PTO may carry over where UTO resets.
  `year_start_month` tinyint NOT NULL DEFAULT '1',
  `year_start_day` tinyint NOT NULL DEFAULT '1',

  -- when the office is shut
  `holidays` varchar(255) DEFAULT NULL COMMENT 'Public holidays the office closes for, as slugs.',
  `custom_holidays` varchar(1000) DEFAULT NULL COMMENT 'Holidays this employer gives that Loaf has no rule for.',

  -- the working week
  `accrual_only_weekdays` varchar(255) DEFAULT NULL
    COMMENT 'Weekdays worked on paper only: cost 0 to book, still counted for accrual.',
  `mon_start` time DEFAULT NULL, `mon_end` time DEFAULT NULL, `mon_break_minutes` int NOT NULL DEFAULT '0',
  `tue_start` time DEFAULT NULL, `tue_end` time DEFAULT NULL, `tue_break_minutes` int NOT NULL DEFAULT '0',
  `wed_start` time DEFAULT NULL, `wed_end` time DEFAULT NULL, `wed_break_minutes` int NOT NULL DEFAULT '0',
  `thu_start` time DEFAULT NULL, `thu_end` time DEFAULT NULL, `thu_break_minutes` int NOT NULL DEFAULT '0',
  `fri_start` time DEFAULT NULL, `fri_end` time DEFAULT NULL, `fri_break_minutes` int NOT NULL DEFAULT '0',
  `sat_start` time DEFAULT NULL, `sat_end` time DEFAULT NULL, `sat_break_minutes` int NOT NULL DEFAULT '0',
  `sun_start` time DEFAULT NULL, `sun_end` time DEFAULT NULL, `sun_break_minutes` int NOT NULL DEFAULT '0',

  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_loaf_shelves_user` (`user_id`),
  CONSTRAINT `loaf_shelves_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE `loaf_baskets`
  ADD COLUMN `shelf_id` int DEFAULT NULL
    COMMENT 'The job this pool belongs to. Its week, holidays and pay cadence are read from there.'
    AFTER `user_id`;

INSERT INTO `loaf_shelves` (
  `user_id`, `name`, `source_basket_id`, `period_hours`,
  `cadence_interval`, `cadence_unit`, `weekdays`, `monthly_days`,
  `yearly_day`, `yearly_month`, `accrual_anchor_date`,
  `year_start_month`, `year_start_day`,
  `holidays`, `custom_holidays`, `accrual_only_weekdays`,
  `mon_start`, `mon_end`, `mon_break_minutes`,
  `tue_start`, `tue_end`, `tue_break_minutes`,
  `wed_start`, `wed_end`, `wed_break_minutes`,
  `thu_start`, `thu_end`, `thu_break_minutes`,
  `fri_start`, `fri_end`, `fri_break_minutes`,
  `sat_start`, `sat_end`, `sat_break_minutes`,
  `sun_start`, `sun_end`, `sun_break_minutes`
)
SELECT
  `user_id`, `name`, `id`, `period_hours`,
  `cadence_interval`, `cadence_unit`, `weekdays`, `monthly_days`,
  `yearly_day`, `yearly_month`, `accrual_anchor_date`,
  `year_start_month`, `year_start_day`,
  `holidays`, `custom_holidays`, `accrual_only_weekdays`,
  `mon_start`, `mon_end`, `mon_break_minutes`,
  `tue_start`, `tue_end`, `tue_break_minutes`,
  `wed_start`, `wed_end`, `wed_break_minutes`,
  `thu_start`, `thu_end`, `thu_break_minutes`,
  `fri_start`, `fri_end`, `fri_break_minutes`,
  `sat_start`, `sat_end`, `sat_break_minutes`,
  `sun_start`, `sun_end`, `sun_break_minutes`
FROM `loaf_baskets`
-- Re-runnable on purpose. apply_file runs with --force and continues
-- past errors, so a migration that is run twice must not quietly give
-- every basket a second shelf.
WHERE `shelf_id` IS NULL;

UPDATE `loaf_baskets` `b`
  JOIN `loaf_shelves` `s` ON `s`.`source_basket_id` = `b`.`id`
   SET `b`.`shelf_id` = `s`.`id`;

ALTER TABLE `loaf_baskets`
  ADD CONSTRAINT `loaf_baskets_shelf_fk`
  FOREIGN KEY (`shelf_id`) REFERENCES `loaf_shelves` (`id`) ON DELETE CASCADE;
