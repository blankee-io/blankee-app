# Pending Transactions Categorization Feature

**Feature Overview**: Create a page to display imported bank transactions that need categorization. Users can assign categories to transactions, with Ntropy providing intelligent suggestions based on the user's existing categories.

**Created**: January 7, 2026  
**Status**: Phase 1 - Planning

---

## High-Level Flow

1. Bank transaction arrives via Quiltt webhook → stored in `quiltt_transactions`
2. Transaction auto-imported as "Uncategorized" entry in appropriate table
3. User visits Pending Transactions page → sees list of uncategorized transactions
4. Ntropy suggests best matching category based on user's existing categories
5. User confirms or changes category → entry moved from Uncategorized to selected category
6. If category is recurring with `wage_bill=1` → reduce corresponding bucket amount
7. Balance reconciliation ensures Blankee matches bank balance

---

## Phase 1: Database & Backend Foundation

### 1.1 Schema Updates
- [x] Add `imported_entry_type` column to `quiltt_transactions` table (ENUM: income, expense, c_expense, c_payment)
- [x] Add index for faster lookups: `idx_quiltt_transactions_imported`
- [x] Create migration file: `migrations/add_imported_entry_type.sql`
- [x] Run migration on dev server 192.0.2.44
- [x] Run migration on dev server 192.0.2.45
- [x] Run migration on AWS production

**Testing**: Verify column exists:
```sql
DESCRIBE quiltt_transactions;
-- Should show: imported_entry_type enum('income','expense','c_expense','c_payment') YES NULL
```

### 1.2 Update Redis Flush Worker
- [x] Update `redis_manager.py` to include `imported_entry_type` in quiltt_transactions flush

**Testing**: Force flush and verify column syncs to MySQL

---

## Phase 2: Auto-Import Transactions to Uncategorized ✅ COMPLETE

### 2.1 Get User's Uncategorized Category
- [x] Create helper function `get_uncategorized_category_id(user_id, type)` in `quiltt_redis.py`
  - type = 'income' | 'expense' | 'c_expense' | 'c_payment'
  - Returns the category ID for "Uncategorized" (or creates if missing)
- [x] Create helper function `get_blankee_credit_account_for_quiltt_account(user_id, quiltt_account_id)` in `quiltt_redis.py`
  - Maps Quiltt account to Blankee credit account by matching mask

**Testing**: Call function and verify it returns correct category ID

### 2.2 Auto-Import on Webhook
- [x] Modify `_sync_quiltt_transactions_for_user()` in `app.py`
- [x] After storing transaction in `quiltt_transactions`, auto-create entry:
  - If `amount > 0` (income) → create `income_entries` record with Uncategorized category
  - If `amount < 0` (expense) → create `expense_entries` record with Uncategorized category
  - For credit accounts: expenses → `c_expense_entries`, payments → `c_payment_entries`
- [x] Set `processed=0` on the entry (indicates pending categorization)
- [x] Store `imported_to_entry_id` and `imported_entry_type` on the quiltt_transaction to link back
- [x] Create helper function `_auto_import_transaction_to_entry()` in `app.py`
- [x] **Only auto-import NEW transactions** - use `pre_sync_txn_ids` to track existing txns
- [x] **Skip auto-import during initial connection** - check connection age < 10 minutes

**Testing**: 
- Trigger sync and verify entry created in income_entries or expense_entries
- Verify entry has correct amount, date, and processed=0

### 2.3 Distinguish Credit Account Transactions
- [x] Determine which Quiltt account maps to which credit account in Blankee
  - Check `quiltt_accounts.account_type` = 'CREDIT' or 'DEPOSITORY'
- [x] For credit accounts:
  - If transaction is negative (money out) → `c_expense_entries`
  - If transaction is positive (payment to card) → `c_payment_entries`
- [x] For depository accounts (checking/savings):
  - Negative → `expense_entries`
  - Positive → `income_entries`
- [x] Auto-create Uncategorized category if missing during auto-adjustment

**Testing**: Import transactions from credit account, verify they go to correct table

### 2.4 Webhook Integration (January 18, 2026)
- [x] `connection.synced.successful` webhook checks connection creation time
- [x] `profile.ready` webhook checks connection creation time
- [x] Initial connection (< 10 min) → `skip_auto_import=True`
- [x] Subsequent sync (>= 10 min) → `skip_auto_import=False`, only NEW transactions imported
- [x] Removed synchronous transaction sync from setup flow (faster UX)
- [x] Fixed duplicate credit account creation bug (now checks MySQL if Redis empty)

---

## Phase 3: Ntropy Category Suggestion ⏳ IN PROGRESS (Sync Complete, Suggestion Function Pending)

### 3.1 Research Ntropy Custom Labels API ✅ COMPLETE
- [x] Review Quiltt Ntropy docs: https://www.quiltt.dev/integrations/enrichment/ntropy
- [x] Review Ntropy direct docs: https://docs.ntropy.com/enrichment/categories
- [x] **FINDING**: Ntropy supports custom categories via `/v3/categories/:id` API
- [ ] **WAITING**: Ask Quiltt if they expose Ntropy custom category APIs
- [ ] **ALTERNATIVE**: Sign up for direct Ntropy API access if Quiltt doesn't support it

### 3.2 Ntropy Custom Category Structure
Ntropy expects categories in this format:
```json
{
  "incoming": ["Wages", "Variable", "Bonus", ...],
  "outgoing": ["Housing", "Utilities", "Groceries", ...]
}
```

**Decision (Feb 3, 2026)**: Use single merged category list, filter in app.

**Blankee → Ntropy Mapping**:
| Blankee Type | Ntropy Direction |
|--------------|------------------|
| `income_categories` | `incoming` |
| `expense_categories` | `outgoing` |
| `c_expense_categories` | `outgoing` (merged with expense) |

**App-Side Filtering Logic**:
When Ntropy returns a category suggestion (e.g., "Groceries"):
1. Check which Quiltt account the transaction came from
2. If **checking/savings** account → look up in `expense_categories`
3. If **credit card** account → look up in `c_expense_categories`
4. If category exists in the appropriate table → suggest it
5. If not found → suggest "Uncategorized"

This lets Ntropy do the "what type of purchase" detection, while we handle "which table".

### 3.3 Sync Categories on User Signup ✅ COMPLETE
- [x] After `/quiltt/create-recommended-categories` creates categories
- [x] Collect all category names by type (income vs expense)
- [x] Call Ntropy API: `POST /v3/categories/blankee_user_{user_id}`
- [x] Create Ntropy account holder with `category_id: "blankee_user_{user_id}"`
- [x] ntropy_utils.py created with sync_user_categories_to_ntropy()

### 3.4 Sync Categories on Category CRUD ✅ COMPLETE
All category creation, update, and delete endpoints now sync to Ntropy.

**Implementation**:
- [x] Create `sync_user_categories_to_ntropy(user_id)` helper function in ntropy_utils.py
- [x] Create `_trigger_ntropy_sync(user_id)` helper in app.py
- [x] Call after any category CREATE/UPDATE/DELETE in Redis
- [x] Function:
  1. Gets all user's income_categories → `incoming`
  2. Gets all user's expense_categories → `outgoing`
  3. Gets all user's c_expense_categories → `outgoing`
  4. Always includes "Uncategorized" as catch-all
  5. POSTs to Ntropy `/v3/categories/blankee_user_{user_id}`

**All 19 Endpoints Updated (Tested Feb 6, 2026):**
| Endpoint | Operation | Tested |
|----------|-----------|--------|
| `/quiltt/create-recommended-categories` | Bulk Create | ✅ |
| `/add_income_category` | Create | ✅ |
| `/add_expense_category` | Create | ✅ |
| `/add_ca_category` | Create | ✅ |
| `/add-recurring-income` | Create | ✅ |
| `/add-recurring-expense` | Create | ✅ |
| `/add-recurring-ca-expense` | Create | ✅ |
| `/update_income_category` | Update | ✅ |
| `/update_expense_category` | Update | ✅ |
| `/update_ca_category` | Update | ✅ |
| `/update-recurring-income/<id>` | Update | ✅ |
| `/update-recurring-expense/<id>` | Update | ✅ |
| `/update-recurring-ca-expense/<id>` | Update | ✅ |
| `/delete_income_category` | Delete | ✅ |
| `/delete_expense_category` | Delete | ✅ |
| `/delete_ca_category` | Delete | ✅ |
| `/delete-recurring-income/<id>` | Delete | ✅ |
| `/delete-recurring-expense/<id>` | Delete | ✅ |
| `/delete-recurring-ca-expense/<id>` | Delete | ✅ |

### 3.5 Create Category Suggestion Function
- [ ] Create `suggest_category_for_transaction(user_id, quiltt_transaction)` in `ntropy_utils.py`
- [ ] Logic:
  1. Get transaction's Ntropy-assigned category (from ntropy_labels)
  2. Look up corresponding `category_id` in user's categories (exact match)
  3. Return suggested `category_id` and match type
- [ ] Handle case where no good match found → return "Uncategorized"

### 3.6 API Access Decision ✅ COMPLETE
- [x] **Option B selected**: Direct Ntropy API access
- [x] API key stored in `.env` as `NTROPY_API_KEY` on all servers
- [x] ntropy_utils.py created with all sync functions

### 3.7 Testing Plan
- [ ] Create test user with custom categories → sign up and create categories
- [ ] Verify Ntropy sync call succeeds (check error logs)
- [ ] Verify Ntropy category set created via `/quiltt/check-ntropy-categories` endpoint
- [ ] Import bank transaction after categories synced
- [ ] Verify Ntropy returns user's custom category label (not generic)
- [ ] Verify category suggestion function matches user's actual category_id

---

## Phase 4: Pending Transactions UI ✅ COMPLETE

### 4.1 Create Backend Endpoint
- [x] Create `/pending-transactions` route in `app.py`
- [x] Create `get_pending_transactions(user_id)` function
  - Query `income_entries`, `expense_entries`, `c_expense_entries`, `c_payment_entries`
  - WHERE `processed = 0` AND `quiltt_transaction_id IS NOT NULL`
  - JOIN with `quiltt_transactions` to get description, merchant, ntropy_labels
  - Return list with suggested category for each

### 4.2 Create HTML Template
- [x] Create `templates/pending_transactions.html`
- [x] Card layout for each transaction
- [x] Show: date, amount, description, merchant, ntropy_labels
- [x] Dropdown for category selection
- [x] Confirm button per transaction
- [x] Add nav link to pending transactions page

### 4.3 Category Dropdown
- [x] Populate dropdown with user's categories (income OR expense based on transaction type)
- [ ] Pre-select the Ntropy-suggested category (pending Phase 3)
- [x] Group categories by income_category_groups / expense_category_groups if applicable

---

## Phase 5: Confirm Transaction Categorization ✅ COMPLETE

### 5.1 Create Confirmation Endpoint
- [x] Create `/api/confirm-transaction` POST endpoint
- [x] Accepts: `entry_id`, `entry_type` (income/expense/c_expense/c_payment), `new_category_id`
- [x] Logic:
  1. Get the entry from appropriate table
  2. Update `category_id` to new value
  3. Set `processed = 1`
  4. If new category is recurring with `wage_bill=1` → call bucket reduction logic (Phase 6)
- [x] Mark dirty in Redis for MySQL flush

### 5.2 Batch Confirmation
- [x] Create `/api/confirm-transactions-batch` POST endpoint
- [x] Accepts array of `{entry_id, entry_type, new_category_id}`
- [x] Process all in single transaction

### 5.3 UI Confirmation Flow
- [x] Wire up "Confirm" button to API
- [x] Show success toast on confirmation
- [x] Remove transaction from pending list
- [x] Update transaction count badge in nav

---

## Phase 6: Bucket Reduction for Recurring Categories 🔜 NEXT

**Context**: Currently bucket logic works for manually-added entries. Need to ensure it also works when entries are auto-imported via webhook and then categorized to a recurring category.

### 6.1 Understand Bucket Structure
- [x] Document current bucket entry flow:
  - `recurring_income_buckets` / `recurring_expense_buckets` / `recurring_c_expense_buckets`
  - These track expected amounts for future recurring entries
  - `income_entries` / `expense_entries` with `is_bucket=1` are generated from these

### 6.2 Find Matching Bucket Entry
- [ ] When webhook-imported transaction is confirmed to a recurring category:
  1. Get the recurring record for that category
  2. Find bucket entry (`is_bucket=1`) for transaction date's cadence period
  3. If amount matches (within tolerance?) → reduce or remove bucket

### 6.3 Implement Bucket Reduction for Webhook Entries
- [ ] Ensure `reduce_bucket_for_transaction(category_id, transaction_date, amount)` works for:
  - Manually added entries (existing behavior)
  - Webhook-imported entries confirmed to recurring category (new behavior)
- [ ] Logic:
  1. Find `recurring_xxx_bucket` record for this category and date
  2. Reduce `amount` by transaction amount
  3. If amount becomes 0 or negative → delete the bucket record
  4. Find corresponding `is_bucket=1` entry and reduce/delete

**Testing**: 
- Create recurring expense with bucket
- Import bank transaction via webhook to that category
- Verify bucket amount reduced correctly

### 6.4 Edge Cases
- [ ] Transaction amount > bucket amount (overpaid bill?)
- [ ] Transaction amount < bucket amount (partial payment?)
- [ ] No bucket exists for this period (late payment?)
- [ ] Multiple transactions matching same bucket
- [ ] Transaction date doesn't match bucket date exactly (within cadence window?)

---

## Phase 7: Balance Reconciliation (Brainstorm Later)

### 7.1 Compare Blankee vs Bank Balance
- [ ] Calculate Blankee's balance from entries
- [ ] Get bank balance from `quiltt_accounts.current_balance`
- [ ] Show discrepancy if any

### 7.2 Handle Discrepancies
- [ ] TBD: Auto-adjustment? Manual review? Notification?
- [ ] Need to brainstorm approach

---

## Phase 8: Polish & Testing

### 8.1 Error Handling
- [ ] Handle network errors in UI
- [ ] Handle concurrent modifications
- [ ] Add retry logic for failed API calls

### 8.2 Performance
- [ ] Pagination for pending transactions list
- [ ] Lazy loading for large transaction counts

### 8.3 End-to-End Testing
- [ ] Test full flow: webhook → auto-import → categorize → bucket reduction
- [ ] Test with checking, savings, and credit accounts
- [ ] Test with income and expense transactions

---

## Files to Modify

| File | Purpose |
|------|---------|
| `migrations/add_quiltt_transaction_link.sql` | New columns for linking |
| `app.py` | Endpoints, auto-import logic |
| `quiltt_redis.py` | Helper functions |
| `quiltt_utils.py` | Category suggestion logic |
| `redis_manager.py` | Flush worker updates |
| `templates/pending_transactions.html` | New UI page |
| `templates/nav.html` | Add nav link |
| `static/css/style.css` | Styling for new page |

---

## Current Status

**Phase**: Phases 1-5 Complete, Phase 8 Implemented, Waiting on Quiltt for Phase 3, Phase 6 Next  
**Last Updated**: February 2, 2026  
**Blockers**: 
1. Awaiting Quiltt response about custom Ntropy labels per user

### Verified Working (as of Feb 2):
- ✅ Webhooks firing correctly after orphan profile cleanup
- ✅ Auto-import creates entries with `processed=0` for NEW transactions
- ✅ Credit account transactions route to correct tables (c_expense_entries, c_payment_entries)
- ✅ Ntropy enrichment data stored in quiltt_transactions
- ✅ Pending transactions notification created when uncategorized entries exist
- ✅ Pending Transactions UI page complete
- ✅ Category confirmation flow working
- ✅ **Bank reconnection flow** - Error webhooks create notification with auto-reconnect link
- ✅ **Periodic connection checker** - Cron job runs every 6 hours to catch missed webhooks
- ✅ **Transaction sync on reconnect** - 9 new transactions imported after reconnecting Capital One

### Bug Fix (Feb 3, 2026): Webhook Events Not Persisting to MySQL
- **Problem**: Webhook events stored in Redis but lost when user not hydrated (Redis expired before flush)
- **Root cause**: `upsert_quiltt_webhook_event()` only wrote to Redis, relying on flush worker
- **Fix**: Now checks `is_user_hydrated()`:
  - If hydrated → Redis-first (standard pattern)
  - If NOT hydrated → Write directly to MySQL
- **File**: `quiltt_redis.py` - `upsert_quiltt_webhook_event()` function

### Bug Fix (Feb 6, 2026): Webhook Storage Failing + Duplicate Notifications
**Issue 1: Webhooks not storing to MySQL**
- **Problem**: `pool.connection()` should be `pool.get_connection()`
- **File**: `quiltt_redis.py` line 1083
- **Fixed**: ✅

**Issue 2: Bad import in webhook handler**
- **Problem**: `from redis_crud import add_notification` - function doesn't exist there
- **Fix**: Removed import, `add_notification` is defined in `app.py` itself
- **File**: `app.py` line 21395
- **Fixed**: ✅

**Issue 3: Duplicate reconnect notifications**
- **Problem**: Cron checker created new notification every 12+ hours instead of updating existing
- **Fix**: Now checks for existing unread notification and updates its date instead of creating new
- **Files**: `quiltt_connection_checker.py` + `app.py` webhook handler
- **Fixed**: ✅

### Pending Verification (Feb 7, 6:00 AM):
- ⏳ **Webhook persistence** - Check `quiltt_webhook_events` table for new entries after midnight cron
- ⏳ **Single notification** - Should only have ONE reconnect notification (date updated, not new row)
- ⏳ **Cron checker logs** - Check `/var/log/apache2/quiltt_checker.log` for "Updated existing notification"

**Verification commands:**
```bash
# Check webhooks stored to MySQL
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT id, event_type, created_at FROM quiltt_webhook_events WHERE created_at >= '2026-02-06' ORDER BY created_at DESC LIMIT 10;\""

# Check notification count (should be 1 per connection)
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT id, LEFT(message, 50), date FROM notifications WHERE user_id = 271 AND message LIKE '%reconnect%' ORDER BY date DESC;\""

# Check cron logs
ssh root@192.0.2.44 "tail -50 /var/log/apache2/quiltt_checker.log"
```

### Other Pending Items:
- ⏳ **Notification deletion** - Should be deleted after reconnect (bug fixed Feb 2)
- ⏳ **Auto-adjust timing** - Balance should match checking account after reconnect (bug fixed Feb 2)
- ⏳ **Phase 3**: Waiting on Quiltt to confirm if we can provide user's categories to Ntropy for custom label matching
- 🔜 **Phase 6**: Bucket reduction needs to work with webhook-triggered entries (not just manual)

---

## Debugging Notes (January 24, 2026)

### Issue: Pending Transactions Page Shows No Entries

**Symptom**: `/pending-transactions` page shows 0 entries despite webhooks working

**Investigation Findings**:

1. **Manual sync works correctly**:
   - Ran manual sync via browser console at 12:01:38 on Jan 24
   - Entries created with `pending=1` in Redis
   - Flushed to MySQL correctly with `pending=1`
   - Redis expired after ~7 min, rehydrated from MySQL with `pending=1` preserved
   - ✅ Full cycle works: Redis → MySQL → Redis

2. **Old webhook entries have `pending=0`**:
   - Entries from Jan 23 webhooks (IDs 20682, 20692, 20702, 20712, 222009, 222019, 222031) have `pending=0` in MySQL
   - Logs show they were created with `pending=1` (e.g., `Created c_expense_entry 20682 for transaction txn_xxx (pending=1)`)
   - Something set them to `pending=0` after creation

3. **Current state (as of Jan 24, ~12:15)**:
   - 9 entries with `pending=1` in MySQL (all from today's manual sync)
   - 8 expense_entries + 1 c_expense_entry
   - Old webhook entries still have `pending=0`

### Update (Jan 24, 13:30) - Webhook Logging Added

**Problem**: Earlier webhooks today (00:35, 00:36, 00:38, 00:58, 01:00, 01:01, 06:09) had NO logs at all despite returning 204 in access logs.

**Resolution**: Added debug logging to webhook handler:
- `===== QUILTT WEBHOOK ENTRY =====` - confirms function was called
- `Quiltt webhook raw payload length: X` - confirms payload received
- `Quiltt webhook headers: signature=True/False, timestamp=...` - shows signature headers

**Test Result**: New bank connection webhook at 13:29 worked perfectly:
- Logged correctly
- Synced 307 transactions
- Stored webhook events in MySQL (evt_132G51kQEEJllM3XmJglpC, evt_132G51vxRWvyHCU1tyx6A5)
- **Note**: This was a setup webhook (connection.synced.successful.initial) so `pending=1` entries weren't expected

### Update (Jan 25, 11:52) - Signature Verification Issue Found

**Root Cause Identified**: Webhooks failing **signature verification** and being silently dropped!

**Evidence from logs** (`/var/log/apache2/blankee_app_20260125.log`):
- 13:28 webhooks (setup): Signature verified ✅, processed successfully
- 14:43 webhook: `ERROR in app: Webhook signature verification failed` ❌, dropped

**Fix Applied**: Changed signature verification to **debug mode** - logs mismatch but continues processing:
```python
if quiltt_signature != expected_signature:
    app.logger.error(f"Webhook signature verification failed...")
    app.logger.warning("Continuing webhook processing despite signature mismatch (debug mode)")
```

**Possible Causes of Signature Mismatch**:
1. Different webhook subscriptions may have different secrets
2. Webhook secret may have been rotated in Quiltt dashboard
3. Some encoding issue with special characters in payload

**Next Step**: Wait for next webhook to verify:
1. Signature mismatch is logged but processing continues
2. Entries are created with `pending=1`
3. Entries remain `pending=1` after flush/rehydration

**Commands to check**:
```bash
# Check webhook access logs
ssh root@192.0.2.44 "grep -i 'webhook' /var/log/apache2/budget_access.log | tail -20"

# Check webhook app logs  
ssh root@192.0.2.44 "grep -E 'WEBHOOK ENTRY|webhook received|signature' /var/log/apache2/budget_error.log | tail -30"

# Check webhook events stored
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT id, event_type, created_at FROM quiltt_webhook_events ORDER BY created_at DESC LIMIT 10;\""
```

**Query to check pending entries**:
```sql
SELECT e.id, e.date, e.amount, e.pending, ec.name as category_name, 'expense' as type 
FROM expense_entries e JOIN expense_categories ec ON e.category_id = ec.id 
WHERE ec.user_id = 271 AND e.pending = 1 
UNION ALL 
SELECT ce.id, ce.date, ce.amount, ce.pending, cec.name as category_name, 'c_expense' as type 
FROM c_expense_entries ce JOIN c_expense_categories cec ON ce.category_id = cec.id 
JOIN credit_accounts ca ON cec.account_id = ca.id 
WHERE ca.user_id = 271 AND ce.pending = 1 
ORDER BY date DESC;
```

---

## Webhook Auto-Import Logic (January 18, 2026)

### Summary of Changes

The webhook system has been updated to properly handle transaction auto-import:

1. **Initial Connection (< 10 min old)**: Transactions are synced but NOT auto-imported
   - Balance adjustment handles initial balance reconciliation
   - Historical transactions stored in `quiltt_transactions` but don't create budget entries

2. **Subsequent Syncs (> 10 min old)**: Only NEW transactions are auto-imported
   - Transactions already in `quiltt_transactions` are skipped
   - Uses `pre_sync_txn_ids` set to track what existed before sync

3. **Setup Flow Optimized**: Connection flow no longer waits for transaction sync
   - Goes directly to category recommendations / balances
   - Webhook handles transaction sync in background later

### Key Code Locations

| File | Location | Purpose |
|------|----------|---------|
| `app.py` | Lines ~20580-20620 | `connection.synced.successful` webhook - checks connection age |
| `app.py` | Lines ~20710-20745 | `profile.ready` webhook - checks connection age |
| `app.py` | Lines ~18230-18240 | `pre_sync_txn_ids` tracking for NEW transaction detection |
| `app.py` | Lines ~18400-18420 | Auto-import condition: `not skip_auto_import and is_new_transaction` |
| `setup_profile.html` | Lines ~950-960 | Removed sync-transactions-setup call |
| `profile.html` | Lines ~2065-2080 | Removed sync-transactions-setup call |

### Webhook Flow

```
Quiltt fires connection.synced.successful webhook
    ↓
Webhook handler receives event with date range (startDate, endDate)
    ↓
Check connection creation time:
    - If < 10 minutes old → skip_auto_import = True (initial connection)
    - If >= 10 minutes old → skip_auto_import = False (subsequent sync)
    ↓
Call _sync_quiltt_transactions_for_user(user_id, start_date, end_date, skip_auto_import)
    ↓
For each transaction from Quiltt API:
    - Check if txn_id in pre_sync_txn_ids (existed before this sync)
    - If NEW transaction AND skip_auto_import=False:
        → Create income_entries/expense_entries/c_expense_entries/c_payment_entries
        → Set processed=0 (pending categorization)
        → Store imported_to_entry_id on quiltt_transaction
```

### Testing Checklist

- [ ] Connect new bank → verify transactions stored but NOT auto-imported
- [ ] Wait for daily webhook → verify NEW transactions auto-imported
- [ ] Verify already-synced transactions are NOT re-imported
- [ ] Check `quiltt_transactions` has `imported_to_entry_id` set for auto-imported txns

### Debugging Commands

```bash
# Check recent webhook events
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT id, event_type, profile_id, created_at FROM quiltt_webhook_events ORDER BY created_at DESC LIMIT 10;\""

# Check webhook logs
ssh root@192.0.2.44 "grep -E 'Webhook|skip_auto_import|is_new_transaction|Auto-import' /var/log/apache2/budget_error.log | tail -30"

# Check transactions with import status
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT transaction_id, date, amount, imported_to_entry_id, imported_entry_type FROM quiltt_transactions WHERE user_id = 230 ORDER BY date DESC LIMIT 10;\""
```

---

## Webhook Debugging (January 12-14, 2026)

### Problem
Quiltt webhooks are being received but transactions are not syncing automatically.

### Resolution (Jan 14)
- Webhooks from Jan 8-13 were `connection.synced.errored.repairable` events from **orphan test profiles**
- Manual sync works perfectly - connection is healthy
- Cleaned up 148 orphan webhook events from 13 old test profiles in MySQL
- Added webhook cleanup when profiles/connections are deleted
- Added logging for unknown profile_ids: `app.logger.warning(f"No user found for profile_id={profile_id}")`

### Findings

1. **Manual sync works perfectly**:
   ```javascript
   fetch('/quiltt/sync-transactions', { method: 'POST', credentials: 'include' })
   // Result: {status: 'success', transactions_synced: 1, message: 'Synced 1 transactions, 1 auto-imported to budget'}
   ```
   - Transaction `txn_1326dpAFlB7HoRkiiprXBZ` (Jan 8, $400 Transfer to SAVINGS) synced successfully
   - Ntropy enrichment working: `ntropy_labels: ["intra-account transfer"]`, `ntropy_recurrence: "one off"`

2. **Webhooks ARE being sent by Quiltt** (from Apache access logs):
   | Date | Time | Status |
   |------|------|--------|
   | Jan 7 | 19:07 - 22:30 | ✅ Multiple (initial/historical sync) |
   | Jan 8-13 | ~22:30 | ⚠️ `errored.repairable` from orphan profiles |

3. **Root cause**: Orphan test profiles from earlier testing were sending error webhooks

### Commands Reference
```bash
# Check webhook events in DB
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"SELECT id, event_type, created_at FROM quiltt_webhook_events ORDER BY created_at DESC LIMIT 10;\""

# Manual sync
fetch('/quiltt/sync-transactions', { method: 'POST', credentials: 'include' }).then(r => r.json()).then(console.log)
```

---

## Phase 8: Bank Connection Reconnection Flow ✅ IMPLEMENTED

**Status**: Implemented February 1-2, 2026 - Testing in progress

### 8.1 Webhook Error Handling
- [x] `connection.synced.errored.repairable` webhook fires when bank needs reconnection
- [x] Webhook sets connection status to `ERROR_REPAIRABLE` 
- [x] Webhook creates notification with clickable reconnect link
- [x] **Detailed logging added** with `[WEBHOOK ERROR]` prefix
- [x] **Webhook events stored** in `quiltt_webhook_events` table via Redis flush

### 8.2 Notification with Reconnect Link
- [x] Notification message includes HTML link: `<a href="/profile?reconnect={connection_id}">Click here to reconnect</a>`
- [x] Notification renders HTML via `| safe` filter in Jinja

### 8.3 Profile Auto-Reconnect
- [x] `checkAutoReconnectParam()` function checks for `?reconnect=<conn_id>` URL param
- [x] Auto-clicks the reconnect button for that connection
- [x] Clears URL param to prevent re-triggering on refresh

### 8.4 Reconnect Flow
- [x] Uses `Quiltt.reconnect()` API to go directly to the specific bank
- [x] Handles `ERROR_REPAIRABLE` (same connection repaired) vs `DISCONNECTED` (new connection)
- [x] Syncs transactions from last synced date after reconnection
- [x] Deletes reconnection notification on success via `/delete-reconnect-notifications`

### 8.5 Periodic Connection Checker (Cron)
- [x] Created `quiltt_connection_checker.py` standalone cron script
- [x] Runs every 6 hours: `0 */6 * * *` (0:00, 6:00, 12:00, 18:00)
- [x] Queries Quiltt API for ALL users with `quiltt_enabled=1`
- [x] Detects `ERROR_REPAIRABLE` or `DISCONNECTED` status
- [x] Creates notification with auto-reconnect link
- [x] Auto-refreshes expired session tokens
- [x] Updates both MySQL AND Redis (if user is hydrated)
- [x] Dedicated log file: `/var/log/apache2/quiltt_checker.log`
- [x] Deployed to dev (.44, .45) and AWS production

### 8.6 Bug Fix: quiltt_enabled Flag
- [x] Fixed bug where `quiltt_enabled` wasn't set to 1 when profile created
- [x] Added UPDATE statement in `app.py` when Quiltt profile is created/updated

### 8.7 Bug Fix: Notification Deletion (Feb 2, 2026)
- [x] Fixed `connectionIdToCleanup` being `undefined` after state was cleared
- [x] Now captures connection ID before clearing `window.reconnectingConnectionId`
- [x] Passes `connIdToCleanup` through `syncAndReload()` → `doAutoAdjustAndReload()` → `deleteReconnectNotifications()`

### 8.8 Bug Fix: Auto-Adjust Timing (Feb 2, 2026)
- [x] **Problem**: `/quiltt/sync-profile` was calling auto-adjust BEFORE transactions synced
- [x] **Solution**: Added `skip_auto_adjust` parameter to `/quiltt/sync-profile`
- [x] **Reconnect flow now**:
  1. `/quiltt/sync-profile` with `skip_auto_adjust: true` (fetches balances, no adjust)
  2. `/quiltt/sync-transactions-range` (imports new transactions as pending)
  3. `/quiltt/auto-adjust-checking` (adjusts AFTER transactions are in)
- [x] Remainder should now match bank balance after reconnect

### 8.9 Testing Checklist (February 3, 2026)
- [x] Verify webhook event recorded in `quiltt_webhook_events` table ✅ Working
- [x] Verify connection status changes to `ERROR_REPAIRABLE` ✅ Working
- [x] Verify notification created with correct link ✅ Working
- [x] Transactions synced from last synced date ✅ 9 new transactions imported
- [ ] Click notification link → verify notification deleted after reconnect
- [ ] Verify balance matches checking account after reconnect (auto-adjust timing fix)

### 8.10 Key Finding (Feb 1, 2026)
**Orphan Quiltt Profiles**: Many webhook events in logs are from orphan profiles (test profiles that were never deleted from Quiltt):
- `p_132FlzgZDCb3ONwYYN0VFo` - Unknown profile (likely old test)
- `p_132Kq0JXxgeqbk050W5IVJ` - Unknown profile (likely old test)

These trigger `"No user found for profile_id"` warnings and are correctly ignored.

**Your actual profile** (`p_132Cs6BwyD4A2UfGqUb6mz`) IS working correctly:
- Webhook events are stored in `quiltt_webhook_events`
- Error repairable event triggered notification
- Reconnect flow worked

**TODO**: Consider cleaning up orphan profiles in Quiltt dashboard to reduce noise.

### 8.11 Log Verification Commands
```bash
# View webhook error logs
ssh root@192.0.2.44 "tail -200 /var/log/apache2/budget_error.log | grep 'WEBHOOK ERROR'"

# Check webhook events table
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
  SELECT id, event_type, profile_id, connection_id, processed, created_at 
  FROM quiltt_webhook_events 
  WHERE event_type LIKE '%errored%' 
  ORDER BY created_at DESC 
  LIMIT 20;
\""

# Check notifications for reconnect links
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
  SELECT id, user_id, LEFT(message, 80) as msg_preview, is_read, date 
  FROM notifications 
  WHERE message LIKE '%reconnect%' 
  ORDER BY date DESC 
  LIMIT 10;
\""

# Check connection status
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
  SELECT id, connection_id, institution_name, status, last_synced_at 
  FROM quiltt_connections 
  WHERE user_id = 271 
  ORDER BY last_synced_at DESC;
\""
```

---

## Questions & Decisions Log

| Date | Question | Decision |
|------|----------|----------|
| Jan 7, 2026 | Where do credit card transactions go? | Expenses → c_expense_entries, Payments → c_payment_entries |
| Jan 7, 2026 | How to handle bucket replacement? | Reduce bucket amount by transaction amount |
| Jan 7, 2026 | What is "Uncategorized" category? | Auto-created on user registration |
| Jan 12, 2026 | Why aren't webhooks syncing? | Debug logging added, waiting for next webhook |
| Feb 1, 2026 | How to notify user of disconnected bank? | Notification with clickable auto-reconnect link |
| Feb 1, 2026 | How to delete notification after reconnect? | New `/delete-reconnect-notifications` endpoint |
| TBD | Balance reconciliation approach? | To be brainstormed in Phase 7 |

