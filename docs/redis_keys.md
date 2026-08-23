# Redis Key Reference

All Redis keys used by the application. Keys are scoped per user unless noted otherwise.

**Version prefix**: `v1`  
**Default TTL**: 604800 seconds (7 days)

---

## User Data Keys

These keys are hydrated from MySQL when a user becomes active, and flushed back to MySQL by the background flush worker every 15 seconds.

| Key Pattern | Type | Description |
|-------------|------|-------------|
| `users:v1:{user_id}` | STRING (JSON object) | User profile and preferences |
| `income_categories:v1:{user_id}` | STRING (JSON array) | Income category definitions |
| `income_category_groups:v1:{user_id}` | STRING (JSON array) | Income category groups |
| `expense_categories:v1:{user_id}` | STRING (JSON array) | Expense category definitions |
| `expense_category_groups:v1:{user_id}` | STRING (JSON array) | Expense category groups |
| `c_expense_category_groups:v1:{user_id}` | STRING (JSON array) | Credit account expense category groups (mirrors expense_category_groups via source_group_id) |
| `income_entries:v1:{user_id}` | STRING (JSON array) | All income entries |
| `expense_entries:v1:{user_id}` | STRING (JSON array) | All expense entries |
| `recurring_income:v1:{user_id}` | STRING (JSON array) | Recurring income templates |
| `recurring_expense:v1:{user_id}` | STRING (JSON array) | Recurring expense templates |
| `recurring_c_expense:v1:{user_id}` | STRING (JSON array) | Recurring credit expense templates |
| `recurring_income_buckets:v1:{user_id}` | STRING (JSON array) | Bucket records for recurring income |
| `recurring_expense_buckets:v1:{user_id}` | STRING (JSON array) | Bucket records for recurring expenses |
| `recurring_c_expense_buckets:v1:{user_id}` | STRING (JSON array) | Bucket records for recurring credit expenses |
| `starting_balance:v1:{user_id}` | STRING (JSON array) | User's starting balance |
| `totals_remainders:v1:{user_id}` | STRING (JSON array) | Weekly aggregated totals and remainders |
| `totals_remainders_d:v1:{user_id}` | STRING (JSON array) | Daily aggregated totals and remainders |
| `totals_remainders_m:v1:{user_id}` | STRING (JSON array) | Monthly aggregated totals and remainders |
| `savings_entries:v1:{user_id}` | STRING (JSON array) | Savings balance entries |
| `savings_adjustments:v1:{user_id}` | STRING (JSON array) | Bank balance adjustments for savings (from the bank provider) |
| `credit_accounts:v1:{user_id}` | STRING (JSON array) | Credit cards and lines of credit |
| `c_expense_categories:v1:{user_id}` | STRING (JSON array) | Credit account expense categories |
| `c_expense_entries:v1:{user_id}` | STRING (JSON array) | Credit account expense entries |
| `c_payment_entries:v1:{user_id}` | STRING (JSON array) | Credit account payment entries |
| `c_a_balances:v1:{user_id}` | STRING (JSON array) | Credit account weekly balances |
| `c_a_balances_d:v1:{user_id}` | STRING (JSON array) | Credit account daily balances |
| `c_a_balances_m:v1:{user_id}` | STRING (JSON array) | Credit account monthly balances |
| `buds:v1:{user_id}` | STRING (JSON array) | Budget projects |
| `bud_items:v1:{user_id}` | STRING (JSON array) | Budget project line items |
| `notifications:v1:{user_id}` | STRING (JSON array) | User notifications |
| `password_resets:v1:{user_id}` | STRING (JSON array) | Password reset tokens |
| `recurring_mismatches:v1:{user_id}` | STRING (JSON array) | Detected recurring bill/wage mismatches from provider enrichment |
| `recurring_suggestions:v1:{user_id}` | STRING (JSON array) | Suggested recurring entries based on enriched transaction patterns |
| `setup_state:v1:{user_id}` | STRING (JSON object) | Profile setup wizard state |

---

## Bank Link Keys

| Key Pattern | Type | Description |
|-------------|------|-------------|
| `linked_provider_profiles:v1:{user_id}` | STRING (JSON array) | Bank-provider profile reference (which provider, and its handle for this user) |
| `linked_connections:v1:{user_id}` | STRING (JSON array) | Bank connections (institution, status) |
| `linked_accounts:v1:{user_id}` | STRING (JSON array) | Individual linked bank accounts |
| `linked_transactions:v1:{user_id}` | STRING (JSON array) | Imported bank transactions, with provider enrichment fields |
| `category_memory:v1:{user_id}` | STRING (JSON array) | Category memory — maps merchant/description to budget categories |
| `linked_last_txn_date:v1:{user_id}` | STRING | Cached latest transaction date; also the entry-locking cutoff |

---

## System / Control Keys

| Key Pattern | Type | TTL | Description |
|-------------|------|-----|-------------|
| `dirty_tables:{user_id}` | SET | 7 days | Table names that have been modified in Redis and need flushing to MySQL |
| `pending_deletes:{table_name}:{user_id}` | SET | 7 days | IDs of records deleted in Redis, pending MySQL deletion during flush |
| `force_refresh:{user_id}` | STRING ("1") | 60s | Signal for frontend to auto-refresh dashboard after a provider sync. NOTE: written by the sync engine, but no consumer was found in app.py, middleware.py, redis_manager.py or the templates — verify before relying on it |
| `provider_sync_lock:{user_id}` | STRING ("1") | 300s (5 min) | Per-user mutex preventing concurrent provider sync processing (SETNX). Reserved: no producer until a bank provider is configured |
| `linked_connections_to_delete:{user_id}` | SET | — | Connection IDs recently deleted by user; webhook handlers skip events for these |

---

## Dirty Tables Reference

The `dirty_tables:{user_id}` SET can contain any of these table names, indicating that the corresponding `{table}:v1:{user_id}` key has pending changes:

```
income_categories, income_category_groups, expense_categories, expense_category_groups,
income_entries, expense_entries, recurring_income, recurring_expense, recurring_c_expense,
recurring_income_buckets, recurring_expense_buckets, recurring_c_expense_buckets,
starting_balance, totals_remainders, totals_remainders_d, totals_remainders_m,
savings_entries, savings_adjustments, credit_accounts, c_expense_categories,
c_expense_entries, c_payment_entries, c_a_balances, c_a_balances_d, c_a_balances_m,
buds, bud_items, linked_provider_profiles, linked_connections, linked_accounts,
linked_transactions, category_memory, notifications, password_resets,
setup_state, users, recurring_mismatches, recurring_suggestions
```

## Pending Deletes Reference

The `pending_deletes:{table}:{user_id}` SET can exist for these tables:

```
income_entries, expense_entries, c_expense_entries, c_payment_entries,
recurring_income, recurring_expense, recurring_c_expense,
recurring_income_buckets, recurring_expense_buckets, recurring_c_expense_buckets,
income_categories, expense_categories, c_expense_categories,
credit_accounts, buds, bud_items, notifications, recurring_mismatches, recurring_suggestions
```
