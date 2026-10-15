-- Migration: the Summary page, arranged per person
-- Which sections the Summary page shows and in what order, and which Upcoming
-- Bills are pinned to the top or filtered out - one JSON document per user.
-- See summary_layout.py for its shape.
-- Created: 2026-10-14
--
-- Backward compatible: the previous release neither reads nor writes the
-- column, and its users flush names its columns, so it leaves this one alone.

ALTER TABLE `users`
  ADD COLUMN `summary_layout` text COMMENT 'The Summary page as this person arranged it: section order, hidden sections, pinned and filtered bills. JSON - summary_layout.py.';
