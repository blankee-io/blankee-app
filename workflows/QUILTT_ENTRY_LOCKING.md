# Quiltt Entry Locking Feature

**Feature Overview**: Prevent users from editing entries that would desync their Blankee balance from their linked bank accounts. Entries from today and before are locked based on which Quiltt accounts the user has connected.

**Created**: February 8, 2026  
**Status**: Phase 1 - Planning

---

## Requirements Summary

### Lock Rules by Account Type

| Quiltt Account Type | What Gets Locked | Time Period |
|---------------------|-----------------|-------------|
| Checking (DEPOSITORY) | ALL income_entries + expense_entries | Today and before |
| Savings (DEPOSITORY) | Entries in "Savings" category only | Today and before |
| Credit Card (CREDIT) | c_expense_entries for that credit account | Today and before |

### What "Locked" Means
- ❌ Can't change amount
- ❌ Can't change date  
- ❌ Can't change category
- ❌ Can't delete
- 🔘 Grey out appearance (same as member_since locking)

### Future Entries
- ✅ Entries dated tomorrow and beyond can still be edited

---

## Phase 1: Backend - Quiltt Account Detection ✅ COMPLETE

### 1.1 Create Helper Functions ✅ DONE
- [x] Create `get_user_quiltt_account_flags(user_id)` in `quiltt_redis.py`
  - Returns: `{ has_checking: bool, has_savings: bool, quiltt_credit_ids: [list], savings_income_category_id, savings_expense_category_id }`
  - Queries `quiltt_accounts` table
  - Checks `account_type = 'DEPOSITORY'` and `account_name` to distinguish checking vs savings
  - Checks `account_type = 'CREDIT'` for credit cards

### 1.2 Map Quiltt Credit to Blankee Credit ✅ DONE
- [x] Create `get_quiltt_credit_account_ids(user_id)` in `quiltt_redis.py`
  - Returns list of Blankee `credit_accounts.id` where `is_quiltt = 1`
  - Used to identify which credit accounts should be locked

### 1.3 Find Savings Category IDs ✅ DONE
- [x] Create `get_savings_category_ids(user_id)` helper
  - Returns: `{ income_savings_id: int|None, expense_savings_id: int|None }`
  - Queries income_categories and expense_categories for `name = 'Savings'`

**Testing Results (User 271)**: 
```python
get_user_quiltt_account_flags(271):
{
  "has_checking": true,
  "has_savings": true,
  "quiltt_credit_ids": [252, 254],
  "savings_income_category_id": 1356,
  "savings_expense_category_id": 1150
}
```

---

## Phase 2: Pass Data to Templates ✅ COMPLETE

### 2.1 Update Dashboard Routes ✅ DONE
- [x] Update `/dashboard` route in `app.py`
  - Call `get_user_quiltt_account_flags(user_id)`
  - Pass to template as `quiltt_flags`
  
- [x] Update `/dashboard_d` route in `app.py`
  - Same: pass `quiltt_flags`

- [x] Update `/dashboard_3m` route in `app.py`
  - Same: pass `quiltt_flags`

### 2.2 Template Variables ✅ DONE
Each dashboard template now receives:
```javascript
let quiltt_flags = {
    has_checking: true/false,
    has_savings: true/false,
    quiltt_credit_ids: [252, 254],  // Blankee credit_accounts.id with is_quiltt=1
    savings_income_category_id: 1356,
    savings_expense_category_id: 1150
};
```

**Testing**: Visit any dashboard and run in browser console:
```javascript
console.log(quiltt_flags);
```

---

## Phase 3: Dashboard Entry Locking (UI) ✅ COMPLETE

### 3.1 Create Locking Helper Functions ✅ DONE
Reuse/extend existing `isBeforeMemberSince()` pattern:

- [x] Create `isEntryLockedByQuiltt(entryDate, categoryId, entryType, accountId)` function
  - `entryType` = 'income' | 'expense' | 'ca'
  - Returns `true` if entry should be locked based on:
    1. Date is today or before AND
    2. One of the following conditions:
       - `has_checking` AND `entryType` is 'income' or 'expense'
       - `has_savings` AND `categoryId` matches savings category
       - `entryType` is `ca` AND `accountId` is in `quiltt_credit_ids`

### 3.2 Update dashboard.html (Week View) ✅ DONE
- [x] Add `quiltt_flags` JavaScript variable from template (Phase 2)
- [x] Add `isEntryLockedByQuiltt()` helper function
- [x] Modify income input creation to check `isEntryLockedByQuiltt()`
- [x] Modify expense input creation to check `isEntryLockedByQuiltt()`
- [x] Modify credit account input creation to check `isEntryLockedByQuiltt()`
- [x] Apply same styling: `input.disabled = true`, `td.classList.add('readonly-cell')`, `td.classList.add('quiltt-locked')`

### 3.3 Update dashboard_d.html (Day View) ✅ DONE
- [x] Add `quiltt_flags` JavaScript variable from template (Phase 2)
- [x] Add `isEntryLockedByQuiltt()` helper function
- [x] Modify income input creation - adds `readonly`, `quiltt-locked` class, hides move button
- [x] Modify expense input creation - adds `readonly`, `quiltt-locked` class, hides move button
- [x] Modify credit account input creation - adds `readonly`, `quiltt-locked` class, hides move button
- [x] Add `shouldShowAddButton()` helper function - checks if add button should appear based on Quiltt flags
- [x] Modify `populateEntryTypeDropdown(date)` - filters Income/Expense based on checking, filters credit accounts based on Quiltt linking
- [x] Update add button creation - hides button if no valid entry types available for locked dates

### 3.4 Update dashboard_3m.html (Month View) ✅ DONE
- [x] Add `quiltt_flags` JavaScript variable from template (Phase 2)
- [x] Add `isEntryLockedByQuiltt()` helper function
- [x] Modify income input creation to check `isEntryLockedByQuiltt()`
- [x] Modify expense input creation to check `isEntryLockedByQuiltt()`
- [x] Modify credit account input creation to check `isEntryLockedByQuiltt()`

### 3.5 CSS Styling ✅ DONE
- [x] Added `.quiltt-locked` class styling to `static/css/style.css`
- [x] Teal corner indicator (6px triangle) for locked cells
- [x] Subtle background tint for dashboard_d locked cells
- [x] Dimmed text color for locked inputs

**Testing**: 
- User with checking connected: ALL income/expense inputs locked for today and before
- User with savings connected: Only "Savings" category locked for today and before
- User with credit connected: Only that credit account's entries locked for today and before

---

## Phase 4: Footer Quick Entry Restrictions ✅ COMPLETE

### 4.1 Pass Quiltt Flags to Footer
- [x] `quiltt_flags` is available globally from dashboard template scripts

### 4.2 Hide Quick Income/Expense Buttons
- [x] If `quiltt_flags.has_checking` is true:
  - Hide `#footer-income-button` and `#footer-income-label`
  - Hide `#footer-expense-button` and `#footer-expense-label`
  - Hide `#footer-autobalance-button` and `#footer-autobalance-label`
- [x] Implemented via `applyQuilttFooterRestrictions()` function in footer.html

### 4.3 Filter Credit Account Dropdown
- [x] Modified `populateCaAccountDropdown()` function:
  - Filters out accounts whose ID is in `quiltt_flags.quiltt_credit_ids`

### 4.4 Hide Credit Entry Button If No Non-Quiltt Accounts
- [x] After filtering, if all credit accounts are Quiltt-linked:
  - Hide `#footer-ca-button` and `#footer-ca-label`

**Testing**:
- User with checking: Income, Expense, Auto Balance buttons hidden ✅
- User with all credit accounts linked to Quiltt: Credit Entry button hidden ✅
- User with mix: Only non-Quiltt credit accounts shown in dropdown ✅

---

## Phase 5: Backend Validation (Safety Net) - SKIPPED

> Decided not to implement - UI locking is sufficient protection.

## Phase 6: Delete Button Visibility - SKIPPED

> Decided not to implement - follows same locking behavior as editing.

---

## ✅ FEATURE COMPLETE

**Completed**: February 8, 2026

**Summary of Implementation**:
- Phase 1: Backend helper functions in `quiltt_redis.py`
- Phase 2: `quiltt_flags` passed to all dashboard templates
- Phase 3: Dashboard entry locking UI (week, day, month views)
- Phase 4: Footer quick entry restrictions

**Behavior**:
- Users with Quiltt checking account: All income/expense entries locked for today and past dates
- Users with Quiltt savings account: Only Savings category entries locked
- Users with Quiltt credit accounts: Only those specific credit account entries locked
- Future-dated entries: Always editable regardless of Quiltt status
- Footer buttons hidden when applicable to prevent new entries that would desync

---

## Phase 7: Testing & Edge Cases

### 7.1 Test Scenarios
- [ ] User with no Quiltt accounts: Everything editable
- [ ] User with only checking: Income/expense locked, credit accounts editable
- [ ] User with only savings: Only Savings category locked
- [ ] User with only credit cards: Only those credit accounts locked
- [ ] User with all account types: Combined locking rules
- [ ] Future-dated entries: Always editable regardless of Quiltt status

### 7.2 Edge Cases
- [ ] Entry dated exactly today: Should be locked
- [ ] Entry dated tomorrow: Should be editable
- [ ] User disconnects Quiltt account: Should entries unlock? (TBD)
- [ ] Pending transactions page: These are already auto-imported, handled separately

---

## Files to Modify

| File | Purpose |
|------|---------|
| `quiltt_redis.py` | Add helper functions for account detection |
| `app.py` | Update dashboard routes to pass quiltt_flags |
| `templates/dashboard.html` | Add locking logic for week view |
| `templates/dashboard_d.html` | Add locking logic for day view |
| `templates/dashboard_3m.html` | Add locking logic for month view |
| `templates/footer.html` | Hide/filter quick entry buttons |

---

## Current Status

**Phase**: Phase 2 Complete, Ready for Phase 3  
**Last Updated**: February 8, 2026  
**Next Step**: Phase 3 - Dashboard Entry Locking (UI)

---

## Technical Notes

### Existing Member Since Locking Pattern
The existing `isBeforeMemberSince()` function provides the pattern to follow:
```javascript
// Helper function to check if a Friday's week is before member_since
function isBeforeMemberSince(dateString) {
    if (!member_since) return false;
    const memberSinceDate = new Date(member_since);
    // ... date comparison logic
}
```

We'll create similar `isEntryLockedByQuiltt()` that combines:
1. Date check (today or before)
2. Account type check (based on quiltt_flags)
3. Category check (for savings-specific locking)

### Credit Account Filtering
Current `populateCaAccountDropdown()`:
```javascript
function populateCaAccountDropdown() {
    caAccountDropdown.innerHTML = '<option value="">Select Account</option>';
    credit_accounts.forEach(account => {
        const option = document.createElement('option');
        option.value = account.id;
        option.textContent = account.name;
        caAccountDropdown.appendChild(option);
    });
}
```

Will become:
```javascript
function populateCaAccountDropdown() {
    caAccountDropdown.innerHTML = '<option value="">Select Account</option>';
    let hasNonQuilttAccounts = false;
    credit_accounts.forEach(account => {
        // Skip Quiltt-linked accounts
        if (account.is_quiltt || quiltt_flags.quiltt_credit_ids.includes(account.id)) {
            return;
        }
        hasNonQuilttAccounts = true;
        const option = document.createElement('option');
        option.value = account.id;
        option.textContent = account.name;
        caAccountDropdown.appendChild(option);
    });
    
    // Hide credit entry button if no non-Quiltt accounts
    if (!hasNonQuilttAccounts) {
        document.getElementById('footer-ca-button').style.display = 'none';
        document.getElementById('footer-ca-label').style.display = 'none';
    }
}
```
