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

## Phase 3: Ntropy Category Suggestion

### 3.1 Research Ntropy Custom Labels API
- [ ] Review Quiltt Ntropy docs: https://www.quiltt.dev/integrations/enrichment/ntropy
- [ ] Determine if we can send user's categories to Ntropy for matching
- [ ] If not directly supported, implement local matching logic:
  - Use `ntropy_labels` from transaction
  - Map labels to user's category names (fuzzy match)

### 3.2 Create Category Suggestion Function
- [ ] Create `suggest_category_for_transaction(user_id, quiltt_transaction)` in `quiltt_utils.py`
- [ ] Logic:
  1. Get transaction's `ntropy_labels` (e.g., ["groceries"])
  2. Get user's expense/income categories
  3. Find best match using fuzzy string matching or keyword mapping
  4. Return suggested `category_id` and confidence score
- [ ] Handle case where no good match found → return "Uncategorized"

**Testing**: 
```javascript
fetch('/quiltt/suggest-category', {method: 'POST', body: JSON.stringify({transaction_id: 'txn_xxx'})})
```

### 3.3 Create Category Mapping Table (Optional)
- [ ] If needed: Create `category_label_mappings` table
  - Maps Ntropy labels to user's category IDs
  - User can customize mappings over time

---

## Phase 4: Pending Transactions UI

### 4.1 Create Backend Endpoint
- [ ] Create `/pending-transactions` route in `app.py`
- [ ] Create `get_pending_transactions(user_id)` function
  - Query `income_entries`, `expense_entries`, `c_expense_entries`, `c_payment_entries`
  - WHERE `processed = 0` AND `quiltt_transaction_id IS NOT NULL`
  - JOIN with `quiltt_transactions` to get description, merchant, ntropy_labels
  - Return list with suggested category for each

**Testing**: 
```javascript
fetch('/api/pending-transactions').then(r => r.json()).then(d => console.log(d));
```

### 4.2 Create HTML Template
- [ ] Create `templates/pending_transactions.html`
- [ ] Similar structure to `notifications.html`:
  - Card layout for each transaction
  - Show: date, amount, description, merchant, ntropy_labels
  - Dropdown for category selection (pre-selected with Ntropy suggestion)
  - Confirm button per transaction
  - "Confirm All" button for batch processing
- [ ] Add nav link to pending transactions page

**Testing**: Navigate to page and verify transactions display

### 4.3 Category Dropdown
- [ ] Populate dropdown with user's categories (income OR expense based on transaction type)
- [ ] Pre-select the Ntropy-suggested category
- [ ] Group categories by income_category_groups / expense_category_groups if applicable

**Testing**: Verify dropdown shows correct categories, correct one pre-selected

---

## Phase 5: Confirm Transaction Categorization

### 5.1 Create Confirmation Endpoint
- [ ] Create `/api/confirm-transaction` POST endpoint
- [ ] Accepts: `entry_id`, `entry_type` (income/expense/c_expense/c_payment), `new_category_id`
- [ ] Logic:
  1. Get the entry from appropriate table
  2. Update `category_id` to new value
  3. Set `processed = 1`
  4. If new category is recurring with `wage_bill=1` → call bucket reduction logic (Phase 6)
- [ ] Mark dirty in Redis for MySQL flush

**Testing**: 
```javascript
fetch('/api/confirm-transaction', {method: 'POST', body: JSON.stringify({entry_id: 123, entry_type: 'expense', new_category_id: 456})})
```
Verify entry moved to new category, processed = 1

### 5.2 Batch Confirmation
- [ ] Create `/api/confirm-transactions-batch` POST endpoint
- [ ] Accepts array of `{entry_id, entry_type, new_category_id}`
- [ ] Process all in single transaction

**Testing**: Confirm multiple transactions at once

### 5.3 UI Confirmation Flow
- [ ] Wire up "Confirm" button to API
- [ ] Show success toast on confirmation
- [ ] Remove transaction from pending list
- [ ] Update transaction count badge in nav

**Testing**: Click confirm, verify transaction disappears from list

---

## Phase 6: Bucket Reduction for Recurring Categories

### 6.1 Understand Bucket Structure
- [ ] Document current bucket entry flow:
  - `recurring_income_buckets` / `recurring_expense_buckets` / `recurring_c_expense_buckets`
  - These track expected amounts for future recurring entries
  - `income_entries` / `expense_entries` with `is_bucket=1` are generated from these

### 6.2 Find Matching Bucket Entry
- [ ] When transaction confirms to `wage_bill=1` category:
  1. Get the recurring record for that category
  2. Find bucket entry (`is_bucket=1`) for transaction date's cadence period
  3. If amount matches (within tolerance?) → reduce or remove bucket

### 6.3 Implement Bucket Reduction
- [ ] Create `reduce_bucket_for_transaction(category_id, transaction_date, amount)` function
- [ ] Logic:
  1. Find `recurring_xxx_bucket` record for this category and date
  2. Reduce `amount` by transaction amount
  3. If amount becomes 0 or negative → delete the bucket record
  4. Find corresponding `is_bucket=1` entry and reduce/delete

**Testing**: 
- Create recurring expense with bucket
- Import bank transaction to that category
- Verify bucket amount reduced

### 6.4 Edge Cases
- [ ] Transaction amount > bucket amount (overpaid bill?)
- [ ] Transaction amount < bucket amount (partial payment?)
- [ ] No bucket exists for this period (late payment?)
- [ ] Multiple transactions matching same bucket

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

**Phase**: 2 Complete, Phase 3 Next  
**Last Updated**: January 18, 2026  
**Next Step**: Wait for webhook to fire, verify auto-import works for NEW transactions only

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

## Questions & Decisions Log

| Date | Question | Decision |
|------|----------|----------|
| Jan 7, 2026 | Where do credit card transactions go? | Expenses → c_expense_entries, Payments → c_payment_entries |
| Jan 7, 2026 | How to handle bucket replacement? | Reduce bucket amount by transaction amount |
| Jan 7, 2026 | What is "Uncategorized" category? | Auto-created on user registration |
| Jan 12, 2026 | Why aren't webhooks syncing? | Debug logging added, waiting for next webhook |
| TBD | Balance reconciliation approach? | To be brainstormed in Phase 7 |

