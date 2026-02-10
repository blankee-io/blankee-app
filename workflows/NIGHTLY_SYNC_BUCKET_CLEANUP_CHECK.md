# Nightly Sync Bucket Cleanup Verification

**Date Created**: February 9, 2026  
**Check Date**: February 10, 2026 (after nightly sync runs at 00:05)

## What Changed

The `cleanup_expired_bucket_entries` function in `nightly_sync.py` was updated to:

1. **Quiltt-linked accounts**: DELETE bucket entries from yesterday (real transactions come from bank sync)
2. **Non-Quiltt accounts**: CONVERT bucket entries by setting `is_bucket = 0` (keep as regular entries)

### Logic Details

| Account Type | Quiltt-Linked? | Action |
|--------------|----------------|--------|
| Income/Expense (depository) | User has Quiltt depository accounts | DELETE bucket entries |
| Income/Expense (depository) | No Quiltt accounts | SET is_bucket = 0 |
| Credit Account Expense | `credit_accounts.is_quiltt = 1` | DELETE bucket entries |
| Credit Account Expense | `credit_accounts.is_quiltt = 0` | SET is_bucket = 0 |

---

## Verification Checklist (February 10, 2026)

### 1. Check Nightly Sync Log
```bash
ssh root@192.0.2.44 "tail -100 /var/log/apache2/nightly_sync.log | grep -E 'bucket|Deleted|Converted'"
```

- [ ] Log shows "Deleted X bucket entries" for Quiltt-linked accounts
- [ ] Log shows "Converted X bucket entries" for non-Quiltt accounts
- [ ] No errors related to bucket cleanup

### 2. Verify Non-Quiltt User Buckets Were Converted (Not Deleted)

Find a user WITHOUT Quiltt (quiltt_enabled = 0):
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT u.id, u.first_name, u.quiltt_enabled 
FROM users u 
WHERE u.quiltt_enabled = 0 
LIMIT 5
\" 2>/dev/null"
```

Then check if their bucket entries from Feb 9 were converted (is_bucket=0), not deleted:
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT 'income' as type, COUNT(*) as count 
FROM income_entries ie 
JOIN income_categories ic ON ie.category_id = ic.id 
WHERE ic.user_id = <USER_ID> AND ie.date = '2026-02-09' AND ie.is_bucket = 0 AND ie.recurring_id IS NOT NULL
UNION ALL
SELECT 'expense' as type, COUNT(*) as count 
FROM expense_entries ee 
JOIN expense_categories ec ON ee.category_id = ec.id 
WHERE ec.user_id = <USER_ID> AND ee.date = '2026-02-09' AND ee.is_bucket = 0 AND ee.recurring_id IS NOT NULL
\" 2>/dev/null"
```

- [ ] Non-Quiltt users have entries with `is_bucket=0` and `recurring_id IS NOT NULL` for Feb 9
- [ ] These entries were NOT deleted

### 3. Verify Quiltt User Buckets Were Deleted

Find a user WITH Quiltt depository accounts:
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT DISTINCT qa.user_id, u.first_name
FROM quiltt_accounts qa
JOIN users u ON qa.user_id = u.id
WHERE qa.account_type = 'DEPOSITORY' AND qa.is_active = 1
LIMIT 5
\" 2>/dev/null"
```

Check that bucket entries from Feb 9 were DELETED (should be 0):
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT 'income' as type, COUNT(*) as count 
FROM income_entries ie 
JOIN income_categories ic ON ie.category_id = ic.id 
WHERE ic.user_id = <USER_ID> AND ie.date = '2026-02-09' AND ie.is_bucket = 1
UNION ALL
SELECT 'expense' as type, COUNT(*) as count 
FROM expense_entries ee 
JOIN expense_categories ec ON ee.category_id = ec.id 
WHERE ec.user_id = <USER_ID> AND ee.date = '2026-02-09' AND ee.is_bucket = 1
\" 2>/dev/null"
```

- [ ] Quiltt users have NO bucket entries (is_bucket=1) for Feb 9

### 4. Verify Credit Account Bucket Handling

Check a non-Quiltt credit account:
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT ca.id, ca.user_id, ca.name, ca.is_quiltt
FROM credit_accounts ca
WHERE ca.is_quiltt = 0
LIMIT 5
\" 2>/dev/null"
```

Check their c_expense_entries were converted:
```bash
ssh root@192.0.2.44 "mysql -u ms_admin -p'dune6MEANTIME.ching_reek' budget -e \"
SELECT COUNT(*) as converted_count
FROM c_expense_entries cee
JOIN c_expense_categories cec ON cee.category_id = cec.id
WHERE cec.account_id = <ACCOUNT_ID> AND cee.date = '2026-02-09' AND cee.is_bucket = 0 AND cee.recurring_id IS NOT NULL
\" 2>/dev/null"
```

- [ ] Non-Quiltt credit accounts have converted entries (is_bucket=0)

---

## Results

**Checked By**: _________________  
**Date Checked**: _________________

### Summary
- [ ] All tests passed
- [ ] Issues found (describe below)

### Notes
```
(Add any observations or issues here)
```

---

## Rollback (If Needed)

If something went wrong, the old logic was:
- DELETE all bucket entries regardless of Quiltt status

To rollback, revert `nightly_sync.py` to previous version.
