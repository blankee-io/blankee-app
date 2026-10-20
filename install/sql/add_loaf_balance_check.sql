-- Migration: a Loaf basket checked against the employer on payday
-- Each payday of a job, Loaf asks what the employer shows for the job's
-- accruing baskets and records any difference as one correction entry - see
-- _payday_checks in loaf_app.py. This is the last payday a basket was asked
-- about, so it is asked once a payday and not again.
-- Created: 2026-10-19
--
-- Backward compatible: the previous release neither reads nor writes it, and
-- Loaf's writes name their columns.

ALTER TABLE `loaf_baskets`
  ADD COLUMN `balance_checked_on` date DEFAULT NULL
    COMMENT 'The last payday this basket was checked against the employer''s figure, or skipped.';
