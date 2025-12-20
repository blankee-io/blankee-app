# Copilot Coding Instructions

## Redis-First Architecture (CRITICAL)

**Redis is the PRIMARY data store. MySQL is the persistent backup.**

### Core Principles
1. **All data modifications MUST happen in Redis first**
2. **Background flush worker syncs Redis → MySQL every 15 seconds**
3. **Never modify MySQL directly unless absolutely necessary**
4. **Use helper functions like `_update_entry_in_redis()`, `_delete_entry_in_redis()`, etc.**

### Redis → MySQL Flush Process
- User modifies data → Update Redis immediately
- Mark table as "dirty" via `dirty_tables:{user_id}` SET
- Background worker (`redis_manager.py`) flushes dirty tables every 15 seconds
- Flush logic: Compare Redis vs MySQL, INSERT/UPDATE/DELETE as needed
- Orphan detection: Delete MySQL records not present in Redis

### Key Redis Patterns
- All keys: `<table_name>:v1:{user_id}` (or `{account_id}` for credit accounts)
- TTL: 604800 seconds (7 days)
- Data format: JSON arrays with DecimalEncoder
- See [`migrations/redis_keys.sql`](../migrations/redis_keys.sql) for complete reference

### Example Workflow
```python
# ✅ CORRECT: Update Redis first
_update_entry_in_redis('income_entries', user_id, category_id, date, amount)
# Redis marks dirty, flush worker handles MySQL later

# ❌ WRONG: Never update MySQL directly
cursor.execute("UPDATE income_entries SET amount = %s WHERE id = %s", (amount, entry_id))
```

---

## Project Data Model (from `migrations/schema.sql`)

This project uses a normalized MySQL schema for budgeting, with strong user isolation and recurring entry support.

### Key Tables

- **users**  
  Stores user accounts and preferences.  
  Columns: `id`, `username`, `first_name`, `last_name`, `email`, `password`, `profile_picture`, `balance_threshold`, `goofy_week_mode`, `member_since`, `landing_page`, `starting_savings`, `currency_type`, `mfa_secret`, `email_notifications`, `quiltt_enabled`, `quiltt_auto_import`

- **income_categories / expense_categories**  
  Category definitions for income and expenses.  
  Columns: `id`, `user_id`, `name`, `display_order`, `group_id`, `is_recurring`, `is_auto_adjustment`, `no_end_date`, `hidden`, `is_bud` (expense only), `is_credit_account` (expense only)  
  Linked to `users` and optional group tables.

- **income_category_groups / expense_category_groups**  
  Optional grouping for categories.  
  Columns: `id`, `user_id`, `name`, `display_order`

- **income_entries / expense_entries**  
  Individual income/expense records.  
  Columns: `id`, `category_id`, `date`, `amount`, `recurring_id`, `is_bucket`, `original_amount`, `processed`, `bud_item_id` (expense only)  
  Linked to their respective categories.

- **recurring_income / recurring_expense**  
  Templates for recurring entries.  
  Columns: `id`, `user_id`, `category_id`, `amount`, `cadence_interval`, `cadence_unit`, `start_date`, `end_date`, `weekdays`, `monthly_days`, `yearly_day`, `yearly_month`

- **credit_accounts**  
  User's credit cards and lines of credit.  
  Columns: `id`, `user_id`, `name`, `interest_rate`, `starting_balance`, `is_card`, `is_line`

- **c_expense_categories**  
  Expense categories specific to credit accounts.  
  Columns: `id`, `account_id`, `name`, `display_order`, `group_id`, `is_recurring`, `no_end_date`, `hidden`, `is_bud`, `is_interest`, `is_auto_adjustment`

- **c_expense_entries**  
  Expense entries charged to credit accounts.  
  Columns: `id`, `category_id`, `date`, `amount`, `recurring_id`, `is_bucket`, `original_amount`, `processed`, `bud_item_id`

- **recurring_c_expense**  
  Recurring expenses for credit accounts.  
  Columns: `id`, `user_id`, `category_id`, `amount`, `cadence_interval`, `cadence_unit`, `start_date`, `end_date`, `weekdays`, `monthly_days`, `yearly_day`, `yearly_month`

- **c_payment_entries**  
  Payments made to credit accounts.  
  Columns: `id`, `account_id`, `date`, `amount`, `recurring_id`, `processed`

- **c_a_balances / c_a_balances_d / c_a_balances_m**  
  Credit account balances aggregated (weekly, daily, monthly).  
  Columns: `id`, `account_id`, `date`, `total_expenses`, `balance`, `total_payments`

- **starting_balance**  
  Initial balance per user.  
  Columns: `id`, `user_id`, `amount`, `date`

- **totals_remainders / totals_remainders_d / totals_remainders_m**  
  Aggregated totals and remainders (weekly, daily, monthly).  
  Columns: `id`, `user_id`, `date`, `total_income`, `total_expenses`, `remainder`, `last_week_remainder`/`last_day_remainder`/`last_month_remainder`

- **savings_entries**  
  Tracks user savings over time.  
  Columns: `id`, `user_id`, `date`, `amount`, `processed`

- **buds**  
  Budget projects for tracking specific spending goals.  
  Columns: `id`, `user_id`, `expense_category_id`, `name`, `created_at`, `active`

- **bud_items**  
  Line items within budget projects.  
  Columns: `id`, `bud_id`, `account`, `name`, `value`, `date`, `description`

- **notifications**  
  User notifications/alerts.  
  Columns: `id`, `user_id`, `date`, `message`, `is_read`

- **password_resets**  
  Password reset tokens and tracking.  
  Columns: `id`, `user_id`, `token`, `created_at`, `expires_at`, `used`

- **quiltt_profiles**  
  Quiltt API profiles for bank connections.  
  Columns: `id`, `user_id`, `profile_id`, `session_token`, `session_expires_at`, `metadata`

- **quiltt_connections**  
  Bank connections via Quiltt.  
  Columns: `id`, `user_id`, `connection_id`, `institution_name`, `institution_id`, `status`, `last_synced_at`, `error_code`, `metadata`

- **quiltt_accounts**  
  Individual bank accounts linked via Quiltt.  
  Columns: `id`, `user_id`, `connection_id`, `account_id`, `account_name`, `account_type`, `account_subtype`, `mask`, `current_balance`, `available_balance`, `currency`, `is_active`, `sync_transactions`, `metadata`

- **quiltt_transactions**  
  Transactions imported from bank accounts.  
  Columns: `id`, `user_id`, `account_id`, `transaction_id`, `amount`, `date`, `description`, `merchant_name`, `category`, `transaction_type`, `pending`, `imported_to_entry_id`, `imported_at`

- **quiltt_category_mappings**  
  Maps Quiltt transaction categories to budget categories.  
  Columns: `id`, `user_id`, `quiltt_category`, `budget_category_id`, `is_expense`

- **quiltt_webhook_events**  
  Webhook events from Quiltt for processing.  
  Columns: `id`, `event_id`, `event_type`, `profile_id`, `connection_id`, `payload`, `processed`, `processed_at`, `error_message`

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

### How to connect to MySQL

In 192.0.2.44 through root ssh. User is ms_admin pw: dune6MEANTIME.ching_reek

---

## Deployment & File Structure

**The local VS Code files ARE the production files via network mount.**  
- VS Code path: `/srv/blankee/`
- Server path: `root@192.0.2.44:/var/www/html/budget/`
- **These are the SAME files** — `/srv/blankee/` is a network-mounted folder pointing directly to `/var/www/html/budget/` on the server
- Any edits in VS Code are immediately live on the server (no copy/deploy needed)
- **Do NOT use `scp` or copy files** — just edit locally and reload Apache

### Error Logs
To check application errors:
```bash
ssh root@192.0.2.44
tail -f /var/log/apache2/budget_error.log
```

After code changes, reload Apache:
```bash
ssh root@192.0.2.44 "systemctl reload apache2"
```

---

## CSS Guidelines (CRITICAL)

**Never use inline CSS unless absolutely unavoidable.**

### CSS File Structure
- Main stylesheet: `static/css/style.css`
- All style changes should be added to this file
- Use semantic class names

### Example
```html
<!-- ❌ WRONG: Inline styles -->
<div style="color: red; margin: 10px;">Content</div>

<!-- ✅ CORRECT: Use classes -->
<div class="error-message">Content</div>
```

```css
/* Add to static/css/style.css */
.error-message {
    color: red;
    margin: 10px;
}
```

---

## Quiltt Integration (Bank Connections)

The application integrates with Quiltt for Open Banking / bank account connectivity.

### What is Quiltt?
Quiltt provides:
- **Bank connection management** - Users link their bank accounts
- **Transaction sync** - Automatic import of transactions from connected accounts
- **Balance tracking** - Real-time account balance updates
- **Multi-institution support** - Connect to thousands of banks

### Key Tables
- `quiltt_profiles` - User's Quiltt profile and session tokens
- `quiltt_connections` - Bank connections (institution, status, last sync)
- `quiltt_accounts` - Individual bank accounts (checking, savings, credit cards)
- `quiltt_transactions` - Imported transactions from banks
- `quiltt_category_mappings` - Map Quiltt categories to budget categories

### User Flow
1. User enables Quiltt in settings (`quiltt_enabled = 1`)
2. User connects bank via Quiltt Connector UI
3. Quiltt syncs transactions automatically
4. Transactions can be imported into budget entries via UI
5. Category mappings allow auto-categorization

### Files
- `quiltt_utils.py` - API integration, profile management, transaction sync
- Integration guides in `integrations/` directory

---

**Reminder:**  
- **Redis-first**: All modifications go through Redis, never MySQL directly
- **CSS in style.css**: No inline styles
- **Local = Production**: Edits in VS Code are live on server
- **Error logs**: `ssh root@192.0.2.44` then check `/var/log/apache2/budget_error.log`
- **Do not use git commands at all**

---

If you need more details on a specific table or workflow, ask for clarification.