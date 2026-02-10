# Manual Entry Buckets - Implementation Checklist

**Feature Overview**: Extend bucket functionality to manual entries. Future-dated manual entries become buckets that can be reduced by entries dated today or earlier. Bucket reduction targets the very next bucket in that category.

**Date Started**: February 8, 2026

---

## Phase 1: Understand Current Bucket System ✅ COMPLETE

### 1.1 Audit Current Bucket Logic ✅ COMPLETE
- [x] Review `bucket_utils.py` - how buckets are created/reduced
- [x] Review `recurring_bucket_manager.py` - recurring bucket generation
- [x] Document current `find_bucket_for_entry()` logic (cadence-based)
- [x] Document when buckets are created vs when entries reduce buckets
- [x] Verify bucket deletion when entry is deleted (fix if broken)

**Current System Summary:**

#### Bucket Creation (Recurring Categories Only)
- When a recurring category is created, entries are generated with `is_bucket=1`
- Bucket entries stored in `income_entries`, `expense_entries`, `c_expense_entries`
- Bucket RECORDS stored separately in `recurring_income_buckets`, `recurring_expense_buckets`, `recurring_c_expense_buckets`
- Bucket records track state even when entry amount hits $0 (allows overspending tracking)

#### Bucket Reduction (Current Logic - Cadence-Based)
- `process_manual_entry_with_bucket()` calls `find_bucket_for_entry()`
- `find_bucket_for_entry()` finds bucket based on **cadence period** (e.g., entry on Feb 5 goes to bucket ending Feb 15 if monthly cadence)
- Calculates `period_start` and `period_end` based on cadence_unit/interval
- Entry must be within `period_start <= entry_date <= bucket_date`

#### Bucket Restoration on Delete
- When deleting a **non-bucket entry**: `restore_bucket_for_deleted_entry()` adds amount back to bucket
- When deleting a **bucket entry** (is_bucket=1): **NO special handling** - just deletes the entry
- ⚠️ **BUG FOUND**: Deleting a bucket entry does NOT delete the corresponding bucket RECORD in `recurring_*_buckets` tables

### 1.2 Identify All Entry Points That Need Changes ✅ COMPLETE
- [x] `/dashboard-d/add_entry` endpoint - line 2020
- [x] `/footer_add_entry` endpoint - line 14099
- [x] `/delete-entry` endpoint - line 8266
- [x] `/delete-week-entry` endpoint - line 8531
- [x] Auto-import from Quiltt transactions - uses `_update_entry_in_redis()` 
- [x] Auto-confirm cron job - `auto_confirm_transactions.py`

**Key Functions:**
- `_update_entry_in_redis()` - line 3313 - creates/updates entries
- `_delete_entry_in_redis()` - line 3442 - deletes entries, handles bucket restore for non-bucket entries
- `process_manual_entry_with_bucket()` - bucket_utils.py line 870 - reduces bucket when adding entry
- `find_bucket_for_entry()` - bucket_utils.py line 153 - **NEEDS REPLACEMENT** with new logic
- `restore_bucket_for_deleted_entry()` - bucket_utils.py line 951 - restores bucket on entry delete

**Tables Involved:**
- Entry tables: `income_entries`, `expense_entries`, `c_expense_entries`
- Bucket record tables: `recurring_income_buckets`, `recurring_expense_buckets`, `recurring_c_expense_buckets`
- Recurring definition tables: `recurring_income`, `recurring_expense`, `recurring_c_expense`

---

## Phase 2: Modify Bucket Selection Logic ✅ COMPLETE

### 2.1 New `find_next_bucket_for_category()` Function ✅ DONE
- [x] Create new function that finds the **next bucket** in a category
- [x] Logic: From today's date, find the bucket entry (is_bucket=1) with the earliest date that is > today
- [x] Works for all entry tables:
  - `income_entries`
  - `expense_entries`
  - `c_expense_entries`
- [x] Returns `None` if no future bucket exists
- [x] **Does NOT use cadence** - purely date-based (find next bucket entry after today)

**Implementation**: `find_next_bucket_for_category()` added to `bucket_utils.py` line ~148

### 2.2 Update Bucket Reduction Logic ✅ DONE
- [x] Modified `process_manual_entry_with_bucket()` to use new selection logic
- [x] Only reduce bucket if entry date ≤ today
- [x] Target the **very next bucket entry** in category (earliest date > today), not based on cadence
- [x] Handle case where no bucket exists (no reduction needed)
- [x] Keep `find_bucket_for_entry()` for backwards compatibility (cadence-based, still used by recurring system)

### 2.3 Update Bucket Restoration Logic ✅ DONE
- [x] Created `restore_bucket_for_deleted_entry_v2()` - simpler function using new logic
- [x] Updated `_delete_entry_in_redis()` in app.py to use new function
- [x] Removed `category_is_recurring` gate - now ALL non-bucket entries can restore to next bucket

**Implementation Details:**
- `find_next_bucket_for_category(table, category_id, user_id)` - bucket_utils.py line ~148
- `restore_bucket_for_deleted_entry_v2(table, category_id, amount, user_id)` - bucket_utils.py line ~1013
- `_delete_entry_in_redis()` updated at line ~3576 to use new functions

---

## Phase 3: Manual Entry → Bucket Creation ✅ COMPLETE

### 3.1 Detect Future-Dated Manual Entries ✅ DONE
- [x] When adding entry: Check if `entry_date > today`
- [x] If future-dated: Mark entry as bucket (`is_bucket=1`, `original_amount=amount`)
- [x] Do NOT reduce any existing bucket
- [x] Works for ALL categories (recurring and non-recurring)

### 3.2 Create Bucket Record for Manual Entries ✅ DONE
- [x] Add record to appropriate bucket table:
  - Income → `recurring_income_buckets`
  - Expense → `recurring_expense_buckets`
  - Credit expense → `recurring_c_expense_buckets`
- [x] Fields: `user_id`, `category_id`, `bucket_date`, `amount`, `original_amount`
- [x] **Decision**: If bucket already exists for that category+date → **add to existing amount** (merge)
- [x] Use `create_bucket_record()` from `recurring_bucket_manager.py`

### 3.3 Entry Table Updates ✅ DONE
- [x] `is_bucket` column exists in all entry tables
- [x] `original_amount` column exists in all entry tables
- [x] `_update_entry_in_redis()` passes `is_bucket` and `original_amount` for future entries

**Implementation Details:**
- `/dashboard-d/add_entry` - line ~2125-2180: Added entry_is_bucket logic
- `/footer_add_entry` - line ~14220-14280: Added same logic
- Both endpoints now:
  1. Check if entry_date > today
  2. If yes: Create entry with is_bucket=1, original_amount=amount
  3. Create bucket record via `create_bucket_record()`
  4. If no: Process bucket reduction via `process_manual_entry_with_bucket()`

---

## Phase 4: Bucket Deletion on Entry Deletion ✅ COMPLETE

### 4.1 Audit Current Deletion Behavior ✅ FIXED
- [x] Test: Delete a bucket entry (is_bucket=1) - does bucket record get deleted?
  - **FINDING**: NO - bucket record in `recurring_*_buckets` was NOT deleted
  - **FIX**: Added `delete_bucket_record_for_entry()` function
- [x] Test: Delete an entry that reduced a bucket - does bucket amount restore?
  - **FINDING**: YES - `restore_bucket_for_deleted_entry()` handles this
- [x] **FIX IMPLEMENTED**: When deleting bucket entry, also delete bucket record

### 4.2 Implement Correct Deletion Logic ✅ DONE
- [x] When deleting entry with `is_bucket=1`:
  - Delete corresponding bucket record from `recurring_*_buckets` table
  - Handles both flushed (positive ID) and unflushed (negative ID) records
  - Works in all dashboards (daily, weekly, summary, 3M, yearly)
- [x] When deleting entry that reduced a bucket:
  - Add amount back to bucket (via `restore_bucket_for_deleted_entry_v2`)
  - Uses **new bucket selection logic** (next bucket, not cadence-based)

**Implementation Details:**
- `delete_bucket_record_for_entry()` added to bucket_utils.py line ~1470
- `_delete_entry_in_redis()` updated to call delete function for bucket entries
- Handles string vs int category_id comparison issue

---

## Phase 5: Edge Cases ✅ COMPLETE

### 5.1 Multiple Future Entries Same Category ✅ VERIFIED
- [x] User adds entry for Feb 15, then Feb 9, then Feb 20
- [x] All three become buckets (is_bucket=1)
- [x] Entry on Feb 8 (today) reduces Feb 9 bucket (nearest future)
- [x] Test this scenario thoroughly

### 5.2 Entry Amount Exceeds Bucket Amount ✅ VERIFIED
- [x] Add entry with amount greater than bucket amount
- [x] Bucket is fully depleted and removed
- [x] Entry is created normally

### 5.3 No Bucket Exists ✅ VERIFIED
- [x] Category has no buckets (not recurring, no future manual entries)
- [x] User adds entry for today
- [x] Should just be a normal entry (`is_bucket=0`), no bucket interaction
- [x] This is the same as current behavior

### 5.4 Delete Bucket Entry ✅ VERIFIED
- [x] Delete a bucket entry
- [x] Both entry and bucket record are deleted

### 5.5 Non-Recurring Categories with Manual Buckets ✅ FIXED
- [x] Bug found: Bucket reduction only worked for recurring categories
- [x] Fixed: Now checks for ANY bucket via `find_next_bucket_for_category()`
- [x] Works for both recurring AND non-recurring categories

### 5.6 Bucket Goes Negative Then Restored ✅ FIXED
- [x] Entry exceeds bucket amount → bucket goes negative
- [x] Bucket entry is deleted (record tracks overspend)
- [x] Delete the reducing entry → bucket should be restored
- [x] Fixed: `restore_bucket_for_deleted_entry_v2()` now recreates entry when amount becomes positive

---

## Phase 6: UI Updates ✅ COMPLETE

### 6.1 Visual Indicator for Manual Buckets ✅ VERIFIED
- [x] **Decision**: Treat manual buckets identically to recurring buckets in UI
- [x] Both show as bucket entries with progress bar display
- [x] No need for visual distinction

### 6.2 Dashboard/Calendar Display ✅ VERIFIED
- [x] Manual bucket entries appear on their date
- [x] Progress bar shows "remaining" amount like recurring buckets
- [x] Works on daily dashboard (verified)
- [x] Weekly dashboard has same `updateBucketProgressBar()` function

---

## Phase 7: Testing ✅ COMPLETE

### 7.1 Integration Tests ✅ VERIFIED
- [x] Full flow: Add future entry → becomes bucket with progress bar
- [x] Full flow: Add today entry → reduces nearest bucket
- [x] Full flow: Add future entry → Delete future entry → Verify bucket deleted
- [x] Full flow: Bucket goes negative → Delete reducing entry → Bucket restored

### 7.2 Edge Case Tests ✅ VERIFIED
- [x] Multiple buckets same category - only nearest reduced
- [x] Entry amount > bucket amount - bucket fully depleted
- [x] No buckets exist - entry created normally
- [x] Non-recurring category with manual buckets - works correctly
- [x] Bucket negative restoration - entry recreated when positive

---

## Files Modified

| File | Changes |
|------|---------|
| `bucket_utils.py` | Added `find_next_bucket_for_category()`, `restore_bucket_for_deleted_entry_v2()`, `delete_bucket_record_for_entry()` |
| `recurring_bucket_manager.py` | Added `get_bucket_records_for_category()` |
| `app.py` | Updated `/dashboard-d/add_entry`, `/footer_add_entry`, `/update-week-entry` for bucket logic; Updated `_delete_entry_in_redis()` for bucket deletion |
| `templates/dashboard.html` | Remove progress bar on entry deletion |
| `templates/dashboard_3m.html` | Remove progress bar on entry deletion |

---

## Current Status

**Status**: ✅ FEATURE COMPLETE  
**Completed**: February 8, 2026  

### All Phases Completed:
1. ✅ Phase 1 - Understand Current Bucket System
2. ✅ Phase 2 - Modify Bucket Selection Logic (next bucket, not cadence-based)
3. ✅ Phase 3 - Manual Entry → Bucket Creation
4. ✅ Phase 4 - Bucket Deletion on Entry Deletion
5. ✅ Phase 5 - Edge Cases (including negative bucket restoration)
6. ✅ Phase 6 - UI Updates (progress bars work for manual buckets)
7. ✅ Phase 7 - Integration Testing

### Feature Summary:
- **Future-dated entries** (date > today) → automatically become buckets
- **Today/past entries** → reduce the **nearest future bucket** in that category
- Works for **ALL categories** (recurring AND non-recurring)
- Works in **ALL dashboards** (daily, weekly, 3-month views)
- Deleting a bucket entry also deletes the bucket record
- Deleting a reducing entry restores the bucket amount
- **Negative bucket restoration**: If bucket goes negative and entry is deleted, bucket entry is recreated
- UI displays progress bars for manual buckets identical to recurring buckets
- Progress bar removed immediately when entry is deleted (no reload needed)
