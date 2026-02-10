# Cron Scripts Migration Verification

**Date Created**: February 9, 2026  
**Check Date**: February 10, 2026

## What Changed

1. **Moved all cron scripts to `cron_scripts/` folder**
2. **Updated crontabs on all servers** to point to new folder
3. **Added log outputs** for `quiltt_connection_checker.py`
4. **Updated log rotation script** to rotate all cron logs

---

## Cron Schedule Reference

| Time (UTC) | Script | Log File |
|------------|--------|----------|
| `0 */6 * * *` (every 6 hrs) | quiltt_connection_checker.py | quiltt_checker.log |
| `0 0 * * *` (midnight) | auto_confirm_transactions.py | auto_confirm.log |
| `5 0 * * *` (00:05) | nightly_sync.py | nightly_sync.log |
| `59 23 * * *` (23:59) | rotate_blankee_logs.sh | log_rotation.log |
| `0 2 * * *` (02:00) | Backup to fs-01 (dev only) | N/A |

---

## Verification Checklist (February 10, 2026)

### 1. Verify Log Rotation Ran (23:59 UTC on Feb 9)

```bash
# Check 44 Dev
ssh root@192.0.2.44 "tail -20 /var/log/apache2/log_rotation.log"
```

- [ ] Shows rotation messages for Feb 9
- [ ] Created `blankee_app_20260209.log`
- [ ] Created `quiltt_checker_20260209.log` (if had content)
- [ ] Created `auto_confirm_20260209.log` (if had content)
- [ ] Created `nightly_sync_20260209.log` (if had content)

```bash
# Check Prod
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "tail -20 /var/log/apache2/log_rotation.log"
```

- [ ] Shows rotation messages for Feb 9

### 2. Verify auto_confirm_transactions Ran (00:00 UTC on Feb 10)

```bash
# Check 44 Dev
ssh root@192.0.2.44 "tail -50 /var/log/apache2/auto_confirm.log"
```

- [ ] Shows execution for Feb 10
- [ ] No errors

```bash
# Check Prod
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "tail -50 /var/log/apache2/auto_confirm.log"
```

- [ ] Shows execution for Feb 10
- [ ] No errors

### 3. Verify nightly_sync Ran (00:05 UTC on Feb 10)

```bash
# Check 44 Dev
ssh root@192.0.2.44 "tail -100 /var/log/apache2/nightly_sync.log"
```

- [ ] Shows execution for Feb 10
- [ ] Bank sync completed
- [ ] Bucket cleanup ran (see separate checklist)
- [ ] No errors

```bash
# Check Prod
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "tail -100 /var/log/apache2/nightly_sync.log"
```

- [ ] Shows execution for Feb 10
- [ ] No errors

### 4. Verify quiltt_connection_checker Ran (00:00, 06:00 UTC)

```bash
# Check 44 Dev (should have run at 00:00 and 06:00)
ssh root@192.0.2.44 "tail -50 /var/log/apache2/quiltt_checker.log"
```

- [ ] Shows execution at 00:00 UTC Feb 10
- [ ] Shows execution at 06:00 UTC Feb 10 (check later in day)
- [ ] No errors

```bash
# Check Prod
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "tail -50 /var/log/apache2/quiltt_checker.log"
```

- [ ] Shows execution at 00:00 UTC Feb 10
- [ ] No errors

### 5. Verify Scripts Exist in cron_scripts Folder

```bash
# 44 Dev (source of truth)
ssh root@192.0.2.44 "ls -la /var/www/html/budget/cron_scripts/"
```

- [ ] `quiltt_connection_checker.py` exists
- [ ] `auto_confirm_transactions.py` exists
- [ ] `nightly_sync.py` exists
- [ ] `rotate_blankee_logs.sh` exists

```bash
# 45 Dev (needs git sync)
ssh root@192.0.2.45 "ls -la /var/www/html/budget/cron_scripts/"
```

- [ ] Folder exists with all scripts (after git sync)

```bash
# Prod (needs git sync)
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "ls -la /var/www/blankee/cron_scripts/"
```

- [ ] Folder exists with all scripts (after git sync)

---

## Results

**Checked By**: _________________  
**Date Checked**: _________________

### Summary
- [ ] All cron jobs ran successfully
- [ ] All logs created properly
- [ ] Log rotation working
- [ ] Issues found (describe below)

### Notes
```
(Add any observations or issues here)
```

---

## Rollback (If Needed)

If scripts fail because folder doesn't exist on 45/prod:

**Option 1**: Sync via git to create the folder

**Option 2**: Temporarily revert crontab paths:
```bash
# 45 Dev
ssh root@192.0.2.45 "crontab -l | sed 's|/cron_scripts||g' | crontab -"

# Prod
ssh -i ~/.ssh/blankeeprodwest1key.pem ubuntu@ec2-13-56-79-248.us-west-1.compute.amazonaws.com "sudo crontab -l | sed 's|/cron_scripts||g' | sudo crontab -"
```

---

## Related Checklists

- [NIGHTLY_SYNC_BUCKET_CLEANUP_CHECK.md](NIGHTLY_SYNC_BUCKET_CLEANUP_CHECK.md) - Verify bucket cleanup logic
