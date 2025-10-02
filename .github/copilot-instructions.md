# Copilot Coding Instructions

## Project Data Model (from `migrations/schema.sql`)

This project uses a normalized MySQL schema for budgeting, with strong user isolation and recurring entry support.  
**Do not include inline CSS in any code suggestions—always provide separate CSS code blocks.**

### Key Tables

- **users**  
  Stores user accounts and preferences.  
  Columns: `id`, `username`, `first_name`, `last_name`, `email`, `password`, `profile_picture`, `balance_threshold`, `goofy_week_mode`, `member_since`, `landing_page`, `starting_savings`, `currency_type`, `mfa_secret`

- **income_categories / expense_categories**  
  Category definitions for income and expenses.  
  Columns: `id`, `user_id`, `name`, `display_order`, `group_id`, `is_recurring`, `is_auto_adjustment`, `no_end_date`, `hidden`  
  Linked to `users` and optional group tables.

- **income_category_groups / expense_category_groups**  
  Optional grouping for categories.  
  Columns: `id`, `user_id`, `name`, `display_order`

- **income_entries / expense_entries**  
  Individual income/expense records.  
  Columns: `id`, `category_id`, `date`, `amount`, `recurring_id`, `processed`  
  Linked to their respective categories.

- **recurring_income / recurring_expense**  
  Templates for recurring entries.  
  Columns: `id`, `user_id`, `category_id`, `amount`, `cadence_interval`, `cadence_unit`, `start_date`, `end_date`, `weekdays`, `monthly_days`, `yearly_day`, `yearly_month`

- **starting_balance**  
  Initial balance per user.  
  Columns: `id`, `user_id`, `amount`, `date`

- **totals_remainders / totals_remainders_d / totals_remainders_m**  
  Aggregated totals and remainders (weekly, daily, monthly).  
  Columns: `id`, `user_id`, `date`, `total_income`, `total_expenses`, `remainder`, `last_week_remainder`/`last_day_remainder`/`last_month_remainder`

- **savings_entries**  
  Tracks user savings over time.  
  Columns: `id`, `user_id`, `date`, `amount`, `processed`

### Relationships & Patterns

- All user data is isolated by `user_id` foreign keys.
- Categories can be grouped (see `*_category_groups`).
- Recurring entries are managed via dedicated tables and referenced by `recurring_id`.
- Aggregation tables (`totals_remainders*`) are used for fast dashboard queries.

### Example Queries

- Get all visible expense categories for a user:
  ```sql
  SELECT * FROM expense_categories WHERE user_id = ? AND hidden = 0;
  ```
- Insert a new recurring income:
  ```sql
  INSERT INTO recurring_income (user_id, category_id, amount, cadence_unit, cadence_interval, start_date)
  VALUES (?, ?, ?, 'months', 1, CURDATE());
  ```

### Where to Find Schema

- All schema definitions: [`migrations/schema.sql`](../migrations/schema.sql)

### Where to Find Redis keys

- All Redis keys: [`migrations/redis_keys.sql`](../migrations/redis_keys.sql)

---

**Reminder:**  
- Never use inline CSS. Provide all style changes as separate CSS code blocks for inclusion in a CSS file.
- Reference this section for table/column names and relationships when generating backend or template code.

---

If you need more details on a specific table or workflow, ask for clarification.