-- =========================================================================
-- Migration: Loaf keeps baskets and hours
-- =========================================================================
-- Purpose: Loaf - the time off planner - has shipped as a shell since
--          add_instance_apps.sql: a gated blueprint, a menu and an admin
--          switch, with nowhere to put a single hour. These are its first
--          two tables.
--
--          A basket is a named pool of PTO or UTO. It knows how it fills
--          (a grant, an accrual, or both), how often pay lands, what the
--          working week looks like, what the balance may reach, and what
--          happens to it when the year turns. An entry is one booking: a
--          date/time range, costed against that working week.
--
-- WHY 21 SCHEDULE COLUMNS AND NOT A CHILD TABLE
--   A loaf_basket_days table is the prettier model and it costs five
--   plumbing sites, four of which fail silently: its own _hydrate_table
--   join branch, its own _flush_table_to_mysql branch, three separate
--   tables_to_flush lists, a pending_deletes handler, and - the one
--   usually forgotten - loaf_baskets' flush branch would have to rewrite
--   basket_id inside the child's Redis array every time a temp id
--   resolved, the way redis_manager.py does for bundle_items, while the
--   child branch skipped rows whose parent id was still negative. The
--   comment at redis_manager.py:471-480 records what a missing join branch
--   actually costs: bundle_items failed to hydrate on every pass and said
--   nothing. Twenty-one scalars that never change independently of their
--   parent do not earn that. Wide tables are not foreign here -
--   linked_transactions is around sixty columns.
--   If shared schedule templates are ever wanted, the escape is a
--   loaf_schedules table plus a schedule_id; these columns are contiguous
--   and move as a block.
--
-- WHY `weekdays` IS THE PAY CADENCE AND NOT THE WORK SCHEDULE
--   Read this before touching either. `weekdays`, `monthly_days`,
--   `cadence_interval` and `cadence_unit` are the days PAY LANDS, in
--   exactly the shape recurring_expense uses, so
--   bucket_utils.recurring_occurrence_dates expands them unchanged rather
--   than Loaf growing a fourth copy of that walk. The days someone WORKS
--   are the mon_*..sun_* columns. Two plausible readings of one column
--   name on one table, and bucket_utils skips an unrecognised weekday name
--   silently - a cadence that never fires and never complains.
--
-- WHY NULL MEANS UNLIMITED
--   max_balance_hours NULL is "no ceiling", following statement_day's
--   "NULL means no interest - not zero, and not the rate". A companion
--   is_unlimited boolean would permit is_unlimited=1 AND cap=40.00, which
--   has no correct reading. 0.00 stays legal and distinct: a basket that
--   cannot accrue at all.
--
-- WHY GRANT AND ACCRUAL ARE TWO COLUMNS AND NOT A MODE
--   They compose. Grant only: "80 hours on 1 January". Accrual only:
--   "3.08 hours every other Friday". Both: front-load 40 and accrue the
--   rest, which is a real policy. Neither: a pot topped up by hand. A mode
--   enum makes the third unrepresentable and invents an invalid state
--   (mode='accrued' with a NULL rate) that this shape cannot hold.
--   accrual_hours is the figure for a FULL period - the projection
--   pro-rates it by hours actually worked. grant_hours is never pro-rated.
--
-- WHY loaf_entries CARRIES user_id
--   It could be reached through loaf_baskets. Carrying its own means
--   install/migrate.py's cascade rule is satisfied directly, and - the
--   real reason - _hydrate_table's default branch
--   (SELECT * FROM {t} WHERE user_id = %s) covers it with no new code.
--   recurring_expense and recurring_expense_buckets both carry user_id
--   beside category_id for the same reason.
--
-- WHY decimal(7,2), AND WHY time
--   Both are new conventions in this schema and are worth saying out loud.
--   There is no non-money decimal quantity here today: money is
--   decimal(15,2), rates decimal(6,3), ordering decimal(8,4). Hours get
--   one precision for balances, caps, accruals and entries alike so nobody
--   has to remember which is which; 7 caps at 99,999.99 hours, a free
--   sanity bound on a figure that should never pass a few thousand.
--   Nothing in the schema uses TIME. Storing the schedule as times rather
--   than as a total per day is what makes a partial day off computable at
--   all - "off Tuesday from 1pm" cannot be costed without them.
--
-- WHY THERE IS NO is_bucket AND NO processed
--   is_bucket means "generated from a recurring template and not yet
--   confirmed". Every Loaf entry is user-created, so the column would
--   import the name without the meaning. processed marks a row already
--   folded into a materialised aggregate, and Loaf has none - its balance
--   changes on accrual dates and days off, perhaps 150 events over four
--   years, so it is projected on demand rather than stored per day. The
--   distinction that does exist is `status`, which names three states a
--   user recognises.
--
-- Run on: each environment in turn, production last
-- =========================================================================

-- loaf_baskets first: loaf_entries has a foreign key to it.
CREATE TABLE IF NOT EXISTS `loaf_baskets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `name` varchar(255) NOT NULL,

  -- An enum rather than a tinyint: a time off planner grows a third kind
  -- (sick, bereavement, floating holiday) sooner than it grows anything
  -- else, and an enum extends with an ALTER where a boolean is replaced.
  -- A basket is wholly one kind or the other; it never holds both.
  `basket_type` enum('pto','uto') NOT NULL DEFAULT 'pto',

  -- Fractional so a drag-reorder inserts a midpoint instead of rewriting
  -- every row. Read DESC, like every other ordered list here.
  `display_order` decimal(8,4) NOT NULL DEFAULT '0.0000',
  `hidden` tinyint(1) NOT NULL DEFAULT '0',

  -- The ceiling. The form calls it "total hours per year", but it stops
  -- accrual, so it is named for what it does.
  `max_balance_hours` decimal(7,2) DEFAULT NULL,

  `grant_hours`   decimal(7,2) DEFAULT NULL,
  `accrual_hours` decimal(7,2) DEFAULT NULL,

  -- Pay cadence. NOT the work schedule - see the header.
  `cadence_interval` int NOT NULL DEFAULT '2',
  `cadence_unit` enum('days','weeks','months','years') NOT NULL DEFAULT 'weeks',
  `weekdays` varchar(255) DEFAULT NULL COMMENT 'Days PAY lands, lowercase names. Not the work week.',
  `monthly_days` varchar(255) DEFAULT NULL COMMENT '1-31 and/or the literal Last Day',
  `yearly_day` int DEFAULT NULL,
  `yearly_month` int DEFAULT NULL,
  `accrual_anchor_date` date DEFAULT NULL COMMENT 'Which week a bi-weekly period lands on',

  -- Where the year turns. A hire anniversary or a fiscal year at least as
  -- often as 1 January, and hard-coding January would be wrong for a large
  -- minority with no symptom anyone would notice.
  `year_start_month` tinyint NOT NULL DEFAULT '1',
  `year_start_day`   tinyint NOT NULL DEFAULT '1',

  -- 'capped' with a NULL cap resolves to 0 in the engine. The tidier
  -- alternative - one nullable column where NULL carries all, 0 resets and
  -- N caps - was rejected because an empty HTML number input arrives as an
  -- empty string, and coercing that to NULL would silently flip a
  -- resetting basket to carry everything: the over-crediting direction.
  `carryover_mode` enum('reset','all','capped') NOT NULL DEFAULT 'reset',
  `carryover_cap_hours` decimal(7,2) DEFAULT NULL,

  -- Warn below this. Blankee has one users.balance_threshold for
  -- everything; eight hours of sick and eight hours of vacation do not
  -- mean the same thing, so this is per basket. NULL warns only on
  -- negative.
  `low_balance_hours` decimal(7,2) DEFAULT NULL,

  -- What the user had when they started using Loaf. Without it every
  -- balance is wrong on the first screen. Precedent: users.starting_savings.
  -- Re-setting this pair is also how someone trues Loaf up against the
  -- figure their employer shows.
  `starting_hours` decimal(7,2) NOT NULL DEFAULT '0.00',
  `starting_date`  date DEFAULT NULL,

  -- The working week. A NULL start is a day not worked. break_minutes is
  -- unpaid time inside the window, per day, because a short Friday usually
  -- has no lunch and one basket-level constant would make that Friday
  -- quietly half an hour wrong.
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
  -- No UNIQUE (user_id, name): two baskets called Vacation is a user error,
  -- not corruption, and a unique index firing mid-edit is a support ticket.
  -- The house pattern is an application check returning a friendly 400, the
  -- way _category_name_exists does for categories.
  KEY `idx_loaf_baskets_user` (`user_id`),
  KEY `idx_loaf_baskets_user_display` (`user_id`,`display_order`),
  CONSTRAINT `loaf_baskets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `loaf_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `basket_id` int NOT NULL,

  -- The range as the user gave it. Two datetimes rather than date+time
  -- pairs because a range crossing midnight or spanning a week is one
  -- interval, not four columns that can disagree. A multi-day absence is
  -- ONE row; the engine walks each date in the span and intersects it with
  -- that weekday's schedule. Naive local time in users.timezone, like
  -- every other date in this application.
  `starts_at` datetime NOT NULL,
  `ends_at`   datetime NOT NULL,

  -- "These whole days off." The engine substitutes each date's full
  -- scheduled hours instead of intersecting a clock range, so a four-hour
  -- Friday costs four and not eight. Without it a whole day has to be
  -- faked as 00:00-23:59 and every consumer has to recognise the fake.
  `all_day` tinyint(1) NOT NULL DEFAULT '0',

  -- What is deducted. Always populated, so a balance query is a bare SUM.
  `hours` decimal(7,2) NOT NULL DEFAULT '0.00',

  -- What the engine produced, kept beside it for the reason
  -- expense_entries keeps original_amount beside amount: so a changed
  -- value reads as changed.
  `computed_hours` decimal(7,2) DEFAULT NULL,

  -- Not derivable from hours <> computed_hours. Overriding 8.00 with 8.00
  -- is still an override, and when a basket's schedule is edited, rows
  -- with 0 are recomputed and rows with 1 must not be. A comparison cannot
  -- answer that.
  `hours_overridden` tinyint(1) NOT NULL DEFAULT '0',

  -- planned    booked, and still deducted - projecting is the point
  -- taken      the confirmed past
  -- cancelled  kept for history, deducts nothing
  -- Three states rather than deriving from the date: "I booked it and then
  -- did not take it" is a real outcome a date comparison cannot express.
  `status` enum('planned','taken','cancelled') NOT NULL DEFAULT 'planned',

  `note` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_loaf_entries_user` (`user_id`),
  -- Basket first: every balance question is basket-scoped and date-ordered.
  KEY `idx_loaf_entries_basket_start` (`basket_id`,`starts_at`),
  -- And across baskets, for the calendar and for the accrual pro-rate,
  -- which counts absence from every basket rather than just this one.
  KEY `idx_loaf_entries_user_start` (`user_id`,`starts_at`),
  -- No index on ends_at: a range-overlap predicate cannot use one usefully,
  -- and a heavy user writes perhaps sixty rows a year.
  CONSTRAINT `loaf_entries_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `loaf_entries_basket_fk` FOREIGN KEY (`basket_id`) REFERENCES `loaf_baskets` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
