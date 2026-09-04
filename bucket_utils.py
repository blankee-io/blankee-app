"""
Bucket utilities for recurring entry management.

This module provides functions to manage "bucket" entries - recurring entries that act as
allowances which get depleted by manual entries in the same category.
"""

from datetime import date, timedelta
from decimal import Decimal
import calendar
from db_connections import get_db_pool
import pymysql
import json

# Import Redis manager module to access the client
import redis_manager
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)


WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                 'saturday', 'sunday']


def recurring_occurrence_dates(cadence_interval, cadence_unit, start_date, end_date,
                               weekdays=None, monthly_days=None,
                               yearly_day=None, yearly_month=None):
    """
    The dates a recurring entry would land on, without creating anything.

    A read-only twin of the walk inside generate_expense_entries and its income and
    credit siblings, which decide dates and write rows in the same loop and so
    cannot be asked what they would do. Written to match them exactly, quirks
    included - a projection that disagrees with what the app will actually generate
    is worse than no projection, because it looks authoritative.

    Duplicated rather than extracted: those three generators are load-bearing write
    paths, and refactoring them to share this belongs in its own change. What keeps
    the duplication honest is that this is checked against the entries they really
    produced - see the verification note in the plan.

    Returns a sorted list of dates. Duplicates are kept: two occurrences on one day
    is two occurrences of the money.
    """
    interval = max(1, int(cadence_interval or 1))
    out = []
    current_date = start_date

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            out.append(current_date)
            delta = timedelta(days=interval)

        elif cadence_unit == 'weeks':
            for weekday in (weekdays or []):
                try:
                    weekday_num = WEEKDAY_NAMES.index(str(weekday).lower())
                except ValueError:
                    continue
                weekday_date = current_date + timedelta(
                    days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    out.append(weekday_date)
            delta = timedelta(weeks=interval)

        elif cadence_unit == 'months':
            year, month = current_date.year, current_date.month
            if monthly_days:
                cleaned = []
                for day in monthly_days:
                    if str(day).lower() == 'last day':
                        cleaned.append('Last Day')
                    else:
                        try:
                            cleaned.append(int(day))
                        except (TypeError, ValueError):
                            continue
                while True:
                    for day in cleaned:
                        try:
                            if str(day).lower() == 'last day':
                                day_num = calendar.monthrange(year, month)[1]
                            else:
                                day_num = int(day)
                                if day_num > calendar.monthrange(year, month)[1]:
                                    continue
                            occ = date(year=year, month=month, day=day_num)
                        except (TypeError, ValueError):
                            continue
                        if start_date <= occ <= end_date:
                            out.append(occ)
                    month += interval
                    while month > 12:
                        month -= 12
                        year += 1
                    if (year > end_date.year) or (year == end_date.year
                                                  and month > end_date.month):
                        break
            else:
                while True:
                    occ = date(year=year, month=month, day=1)
                    if occ > end_date:
                        break
                    if occ >= start_date:
                        out.append(occ)
                    month += interval
                    while month > 12:
                        month -= 12
                        year += 1
                    if (year > end_date.year) or (year == end_date.year
                                                  and month > end_date.month):
                        break
            break  # months are walked whole, not stepped

        elif cadence_unit == 'years':
            year = start_date.year
            while True:
                try:
                    occ = (date(year=year, month=int(yearly_month), day=int(yearly_day))
                           if (yearly_day and yearly_month)
                           else date(year=year, month=1, day=1))
                except (TypeError, ValueError):
                    year += interval
                    if year > end_date.year + 1:
                        break
                    continue
                if occ > end_date:
                    break
                if occ >= start_date:
                    out.append(occ)
                year += interval
            break

        if delta:
            current_date += delta
        else:
            break

    return sorted(out)


def get_interval_bounds(entry_date, cadence_unit, cadence_interval, start_date, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    """
    Calculate the start and end dates of the interval containing the given entry_date.
    
    Args:
        entry_date: The date of the manual entry (date object or string)
        cadence_unit: 'days', 'weeks', 'months', or 'years'
        cadence_interval: Integer interval (e.g., 1, 2, 3)
        start_date: The start date of the recurring entry (date object or string)
        weekdays: Comma-separated weekdays for weekly cadence
        monthly_days: Comma-separated days for monthly cadence
        yearly_day: Day of month for yearly cadence
        yearly_month: Month for yearly cadence
        
    Returns:
        Tuple of (interval_start_date, interval_end_date) as date objects
    """
    # Convert strings to date objects if needed
    if isinstance(entry_date, str):
        entry_date = date.fromisoformat(entry_date)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    
    if cadence_unit == 'days':
        # For daily cadence, interval is just that single day
        return (entry_date, entry_date)
    
    elif cadence_unit == 'weeks':
        # Find the most recent occurrence on or before entry_date
        # Work backwards from entry_date to find the bucket date
        current = entry_date
        while current >= start_date:
            if weekdays:
                weekday_list = [int(d.strip()) for d in weekdays.split(',')]
                if current.weekday() in weekday_list:
                    # Found the bucket date, interval is from day after previous occurrence to this occurrence
                    interval_end = current
                    # Find previous occurrence
                    prev = current - timedelta(days=1)
                    found_prev = False
                    while prev >= start_date:
                        if prev.weekday() in weekday_list:
                            interval_start = prev + timedelta(days=1)
                            found_prev = True
                            break
                        prev -= timedelta(days=1)
                    if not found_prev:
                        interval_start = start_date
                    return (interval_start, interval_end)
            current -= timedelta(days=1)
        
        # If we get here, entry_date is before any occurrences
        return (start_date, start_date)
    
    elif cadence_unit == 'months':
        # Find the occurrence in this month or the most recent previous month
        if monthly_days:
            day_list = [int(d.strip()) for d in monthly_days.split(',')]
            # Find the most recent occurrence on or before entry_date
            current_year = entry_date.year
            current_month = entry_date.month
            
            for months_back in range(24):  # Look back up to 2 years
                test_year = current_year - (current_month - 1 - months_back) // 12
                test_month = ((current_month - 1 - months_back) % 12) + 1
                test_date = date(test_year, test_month, 1)
                
                if test_date < start_date:
                    break
                
                # Find all occurrences in this month on or before entry_date
                last_day = calendar.monthrange(test_year, test_month)[1]
                for day in sorted(day_list, reverse=True):
                    if day <= last_day:
                        occurrence = date(test_year, test_month, min(day, last_day))
                        if occurrence <= entry_date and occurrence >= start_date:
                            # Found the bucket date
                            interval_end = occurrence
                            # Find previous occurrence
                            found_prev = False
                            for prev_months_back in range(months_back + 1, months_back + 25):
                                prev_year = current_year - (current_month - 1 - prev_months_back) // 12
                                prev_month = ((current_month - 1 - prev_months_back) % 12) + 1
                                prev_last_day = calendar.monthrange(prev_year, prev_month)[1]
                                for prev_day in sorted(day_list, reverse=True):
                                    if prev_day <= prev_last_day:
                                        prev_occurrence = date(prev_year, prev_month, min(prev_day, prev_last_day))
                                        if prev_occurrence >= start_date:
                                            interval_start = prev_occurrence + timedelta(days=1)
                                            found_prev = True
                                            break
                                if found_prev:
                                    break
                            if not found_prev:
                                interval_start = start_date
                            return (interval_start, interval_end)
        
        return (start_date, start_date)
    
    elif cadence_unit == 'years':
        # Find the most recent yearly occurrence on or before entry_date
        if yearly_day and yearly_month:
            current_year = entry_date.year
            for year in range(current_year, current_year - 10, -1):
                try:
                    occurrence = date(year, yearly_month, yearly_day)
                    if occurrence <= entry_date and occurrence >= start_date:
                        # Found the bucket date
                        interval_end = occurrence
                        # Find previous occurrence
                        found_prev = False
                        for prev_year in range(year - 1, year - 11, -1):
                            try:
                                prev_occurrence = date(prev_year, yearly_month, yearly_day)
                                if prev_occurrence >= start_date:
                                    interval_start = prev_occurrence + timedelta(days=1)
                                    found_prev = True
                                    break
                            except ValueError:
                                continue
                        if not found_prev:
                            interval_start = start_date
                        return (interval_start, interval_end)
                except ValueError:
                    continue
        
        return (start_date, start_date)
    
    return (start_date, entry_date)


def find_next_bucket_for_category(table, category_id, user_id, wage_bill=None):
    """
    Find the bucket entry a manual entry should deplete.

    Which way the 45-day window points depends on the kind of entry, and the
    reason is mechanical rather than stylistic.

    Wage/Bill (wage_bill=1) takes the LATEST bucket dated on or before today,
    or the earliest upcoming one when the series has not started yet. Rent is
    bucketed on the 1st and paid on the 3rd, and that payment has to consume the
    1st's bucket or the month gets recorded twice.

    No day count bounds it, because consecutive bucket dates already are the
    period boundaries - the series encodes the cadence, so nothing has to guess
    at it. A fixed 45-day look-back was wrong in both directions: on a weekly
    bill it reached back six occurrences and cleared the oldest unpaid week
    instead of this one, and on a yearly bill it was shorter than the cadence, so
    a payment two months late fell outside the window, depleted nothing, and got
    recorded twice once the prompt was answered.

    Taking the latest rather than the earliest also means that with several
    occurrences unpaid, a payment settles the current one and leaves the older
    ones to the prompt - where Skip is the right answer for something that never
    happened.

    It resolves itself cleanly because three things line up: a wage_bill
    depletion subtracts the FULL bucket amount, subtract_from_bucket deletes a
    bucket that reaches zero, and the evening prompt only lists buckets with
    amount > 0. So paying a bill late removes it from the prompt, and answering
    the prompt removes it from depletion's reach - whichever happens first, the
    other never sees it and nothing is counted twice.

    Allowance/Variable (wage_bill=0) looks FORWARD: the earliest live bucket
    dated between today and today + 45 days - the very next bucket. Depletion
    here is partial, so an overdue bucket would survive with a reduced figure
    and the prompt would go on to ask about a number that spending had already
    eaten. A period that has ended should not absorb what is spent today either;
    that belongs to the period containing today. Overdue allowance buckets are
    the prompt's business alone.

    Cadence is deliberately not consulted; this replaced the cadence-based
    selection in Feb 2026.

    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        user_id: The user ID
        wage_bill: 1, 0, or None to look it up. Callers that already know it
            should pass it; everything else gets the same answer from the
            recurring record, so a probe cannot pick a different bucket from
            the depletion that follows it, and restoring a deleted entry cannot
            put money back into a bucket other than the one it came out of.

    Returns:
        Dictionary with bucket entry data, or None if no reducible bucket exists
    """
    from flask import current_app

    if wage_bill is None:
        wage_bill = _get_wage_bill_for_category(table, category_id, user_id)
    wage_bill = int(wage_bill or 0)

    today = date.today()
    forward_window = timedelta(days=45)

    log_info(logger, 'FIND_NEXT_BUCKET',
             f"Looking for next bucket: table={table}, category_id={category_id}, "
             f"user_id={user_id}, wage_bill={wage_bill}, today={today}")

    # Get all entries from Redis
    from app import _get_entries_from_redis
    entries = _get_entries_from_redis(table, user_id)

    if not entries:
        log_info(logger, 'FIND_NEXT_BUCKET',
                 f"No entries in Redis for user {user_id}, table {table}")
        return None

    candidates = []
    for entry in entries:
        if (entry.get('category_id') == int(category_id) and
                entry.get('is_bucket') == 1 and
                float(entry.get('amount', 0)) > 0):
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                try:
                    entry_date = date.fromisoformat(entry_date[:10])
                except ValueError:
                    continue
            if entry_date is None:
                continue
            candidates.append((entry_date, entry))

    if wage_bill:
        # The occurrence whose period contains today: the most recent one due.
        due = [p for p in candidates if p[0] <= today]
        if due:
            chosen, which = max(due, key=lambda p: p[0]), 'latest due'
        else:
            # Nothing has come due yet, so the series has not started and the
            # first occurrence is what an early payment is paying. No bound is
            # needed: the earliest upcoming bucket IS that occurrence.
            upcoming = [p for p in candidates if p[0] > today]
            chosen = min(upcoming, key=lambda p: p[0]) if upcoming else None
            which = 'first upcoming (none due yet)'
    else:
        # Allowance: the period containing today, which is the next bucket
        # forward. Bounded, so spending today cannot reach a bucket months out
        # when this category simply has no near-term one.
        upcoming = [p for p in candidates
                    if today <= p[0] <= today + forward_window]
        chosen = min(upcoming, key=lambda p: p[0]) if upcoming else None
        which = 'upcoming'

    if chosen is None:
        log_info(logger, 'FIND_NEXT_BUCKET',
                 f"No bucket via {which} for category {category_id}")
        return None

    next_bucket = chosen[1]
    log_info(logger, 'FIND_NEXT_BUCKET',
             f"Found next bucket via {which}: id={next_bucket.get('id')}, "
             f"date={next_bucket.get('date')}, amount={next_bucket.get('amount')}")

    return next_bucket

def find_bucket_for_entry(table, category_id, entry_date, user_id, cadence_info):
    """
    Find the active bucket entry for a given category and date by matching the entry_date
    to the bucket's interval based on cadence.
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        entry_date: The date to search for (date object or string)
        user_id: The user ID
        cadence_info: Dictionary with cadence details (cadence_unit, cadence_interval, start_date, etc.)
        
    Returns:
        Dictionary with bucket entry data, or None if no bucket found
    """
    from flask import current_app
    
    if isinstance(entry_date, str):
        entry_date = date.fromisoformat(entry_date)
    
    log_info(logger, 'FIND_BUCKET', f"Looking for bucket: table={table}, category_id={category_id}, entry_date={entry_date}, user_id={user_id}")
    
    # Get all entries from Redis (not MySQL!)
    from app import _get_entries_from_redis
    entries = _get_entries_from_redis(table, user_id)
    
    if not entries:
        log_info(logger, 'FIND_BUCKET', f"No entries in Redis for user {user_id}, table {table}")
        return None
    
    # Filter to get only bucket entries for this category with amount > 0
    buckets = [
        entry for entry in entries
        if entry.get('category_id') == int(category_id)
        and entry.get('is_bucket') == 1
        and float(entry.get('amount', 0)) > 0
    ]
    
    # Sort by date ascending
    buckets.sort(key=lambda x: x.get('date', ''))
    
    log_info(logger, 'FIND_BUCKET', f"Found {len(buckets)} buckets for category {category_id} in Redis")
    if buckets:
        for i, b in enumerate(buckets):
            log_info(logger, 'FIND_BUCKET', f"Bucket {i}: id={b['id']}, date={b['date']}, amount={b['amount']}, is_bucket={b.get('is_bucket')}")
    
    if not buckets:
        log_info(logger, 'FIND_BUCKET', f"No buckets found, returning None")
        return None
    
    # Find the bucket whose date is >= entry_date and is the closest one
    # The bucket's date represents the END of the interval, so we want the first bucket
    # whose date is on or after the entry_date
    matching_bucket = None
    for bucket in buckets:
        bucket_date = bucket['date']
        if isinstance(bucket_date, str):
            bucket_date = date.fromisoformat(bucket_date)
        
        log_info(logger, 'FIND_BUCKET', f"Checking bucket date {bucket_date} against entry_date {entry_date}")
        
        # If this bucket's date is on or after the entry_date, check if entry is within cadence period
        if bucket_date >= entry_date:
            # Calculate the interval bounds for this bucket
            cadence_unit = cadence_info.get('cadence_unit')
            cadence_interval = int(cadence_info.get('cadence_interval', 1))  # Convert to int
            start_date_str = cadence_info.get('start_date')
            
            # Get the start date of this bucket's period
            if cadence_unit == 'days':
                # For daily cadence, the period is just the bucket_date
                period_start = bucket_date - timedelta(days=cadence_interval - 1)
            elif cadence_unit == 'weeks':
                # For weekly cadence, calculate week start
                period_start = bucket_date - timedelta(days=7 * cadence_interval - 1)
            elif cadence_unit == 'months':
                # For monthly cadence, go back one month from bucket_date
                if cadence_interval == 1:
                    # Go back to previous month, same day
                    if bucket_date.month == 1:
                        prev_month = 12
                        prev_year = bucket_date.year - 1
                    else:
                        prev_month = bucket_date.month - 1
                        prev_year = bucket_date.year
                    
                    # Handle day overflow (e.g., Jan 31 -> Feb 28)
                    try:
                        period_start = bucket_date.replace(year=prev_year, month=prev_month)
                    except ValueError:
                        # Day doesn't exist in previous month, use last day of that month
                        last_day = calendar.monthrange(prev_year, prev_month)[1]
                        period_start = date(prev_year, prev_month, last_day)
                    
                    # Add 1 day to get the day after the previous bucket
                    period_start = period_start + timedelta(days=1)
                else:
                    # Multi-month intervals
                    months_back = cadence_interval
                    temp_date = bucket_date
                    for _ in range(months_back):
                        if temp_date.month == 1:
                            temp_date = temp_date.replace(year=temp_date.year - 1, month=12)
                        else:
                            try:
                                temp_date = temp_date.replace(month=temp_date.month - 1)
                            except ValueError:
                                last_day = calendar.monthrange(temp_date.year, temp_date.month - 1)[1]
                                temp_date = date(temp_date.year, temp_date.month - 1, last_day)
                    period_start = temp_date + timedelta(days=1)
            elif cadence_unit == 'years':
                # For yearly cadence, go back one year from bucket_date
                try:
                    period_start = bucket_date.replace(year=bucket_date.year - cadence_interval) + timedelta(days=1)
                except ValueError:
                    # Handle leap year edge case (Feb 29)
                    period_start = date(bucket_date.year - cadence_interval, bucket_date.month, 28) + timedelta(days=1)
            else:
                # Unknown cadence unit, default to using bucket date
                period_start = bucket_date
            
            log_info(logger, 'FIND_BUCKET', f"Bucket period: {period_start} to {bucket_date}, entry_date: {entry_date}")
            
            # Check if entry_date is within the bucket's period
            if period_start <= entry_date <= bucket_date:
                matching_bucket = bucket
                log_info(logger, 'FIND_BUCKET', f"MATCH! Bucket {bucket['id']} matches (entry within period {period_start} to {bucket_date})")
                break
            else:
                log_info(logger, 'FIND_BUCKET', f"Entry date {entry_date} outside bucket period ({period_start} to {bucket_date}), checking next bucket")
    
    if not matching_bucket:
        log_info(logger, 'FIND_BUCKET', f"No matching bucket found within valid cadence period")
    
    return matching_bucket


def _format_cadence_string(cadence_unit, cadence_interval):
    """
    Format cadence information into a human-readable string.
    
    Args:
        cadence_unit: 'days', 'weeks', 'months', or 'years'
        cadence_interval: Integer interval (e.g., 1, 2, 3)
    
    Returns:
        Human-readable string like "week", "month", "2 weeks", etc.
    """
    interval = int(cadence_interval) if cadence_interval else 1
    unit = cadence_unit or 'months'
    
    # Map plural to singular for interval of 1
    unit_singular = {
        'days': 'day',
        'weeks': 'week', 
        'months': 'month',
        'years': 'year'
    }
    
    if interval == 1:
        return unit_singular.get(unit, unit.rstrip('s'))
    else:
        return f"{interval} {unit}"


def _create_bucket_depleted_notification(user_id, table, category_id, bucket_date, original_amount, recurring_id=None):
    """
    Create a notification when a bucket is fully depleted.
    
    Args:
        user_id: The user ID
        table: Entry table name ('income_entries', 'expense_entries', 'c_expense_entries')
        category_id: The category ID of the depleted bucket
        bucket_date: The date of the bucket
        original_amount: The original amount of the bucket
        recurring_id: The recurring entry ID (to get cadence info)
    """
    from datetime import datetime
    from flask import current_app
    
    try:
        # Map entry table to category table and recurring table
        table_map = {
            'income_entries': {'category': 'income_categories', 'recurring': 'recurring_income'},
            'expense_entries': {'category': 'expense_categories', 'recurring': 'recurring_expense'},
            'c_expense_entries': {'category': 'c_expense_categories', 'recurring': 'recurring_c_expense'}
        }
        tables = table_map.get(table)
        if not tables:
            log_error(logger, 'BUCKET_NOTIFICATION', f"Unknown entry table: {table}")
            return
        
        category_table = tables['category']
        recurring_table = tables['recurring']
        
        # Get category name (and account_id for credit expenses) from Redis
        category_name = None
        account_id = None
        redis_key = f"{category_table}:v1:{user_id}"
        redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
        
        if redis_data:
            categories = json.loads(redis_data)
            for cat in categories:
                if cat.get('id') == int(category_id):
                    category_name = cat.get('name', 'Unknown Category')
                    account_id = cat.get('account_id')  # For credit expense categories
                    break
        
        # Fallback to database if not found in Redis
        if not category_name:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(f"SELECT name, account_id FROM {category_table} WHERE id = %s", (category_id,))
                result = cursor.fetchone()
                if result:
                    category_name = result.get('name', 'Unknown Category')
                    account_id = result.get('account_id')
                cursor.close()
        
        if not category_name:
            category_name = 'Unknown Category'
        
        # Get credit account name if this is a credit expense
        credit_account_name = None
        if table == 'c_expense_entries' and account_id:
            # Try Redis first
            accounts_key = f"credit_accounts:v1:{user_id}"
            accounts_data = redis_manager._redis_client.get(accounts_key) if redis_manager._redis_client else None
            
            if accounts_data:
                accounts = json.loads(accounts_data)
                for acc in accounts:
                    if acc.get('id') == int(account_id):
                        credit_account_name = acc.get('name')
                        break
            
            # Fallback to database
            if not credit_account_name:
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT name FROM credit_accounts WHERE id = %s", (account_id,))
                    result = cursor.fetchone()
                    if result:
                        credit_account_name = result.get('name')
                    cursor.close()
        
        # Get cadence info from recurring entry
        cadence_str = None
        cadence_unit = None
        cadence_interval = None
        recurring_entry = None
        
        if recurring_id:
            # Try Redis first
            recurring_key = f"{recurring_table}:v1:{user_id}"
            recurring_data = redis_manager._redis_client.get(recurring_key) if redis_manager._redis_client else None
            
            if recurring_data:
                recurring_entries = json.loads(recurring_data)
                for rec in recurring_entries:
                    if rec.get('id') == int(recurring_id):
                        recurring_entry = rec
                        cadence_unit = rec.get('cadence_unit', 'months')
                        cadence_interval = rec.get('cadence_interval', 1)
                        cadence_str = _format_cadence_string(cadence_unit, cadence_interval)
                        break
            
            # Fallback to database
            if not recurring_entry:
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute(f"SELECT * FROM {recurring_table} WHERE id = %s", (recurring_id,))
                    recurring_entry = cursor.fetchone()
                    if recurring_entry:
                        cadence_unit = recurring_entry.get('cadence_unit', 'months')
                        cadence_interval = recurring_entry.get('cadence_interval', 1)
                        cadence_str = _format_cadence_string(cadence_unit, cadence_interval)
                    cursor.close()
        
        # Format date for display - use date range for non-daily cadences
        from datetime import date as date_type
        from dateutil.relativedelta import relativedelta
        if isinstance(bucket_date, str):
            date_obj = date_type.fromisoformat(bucket_date)
        else:
            date_obj = bucket_date
        
        # Calculate date range based on cadence
        # The bucket_date is when the recurring entry occurs (end of period)
        # The period starts from the day after the previous occurrence
        formatted_date = ''
        if date_obj and cadence_unit and cadence_interval:
            interval = int(cadence_interval)
            if cadence_unit == 'days':
                if interval == 1:
                    # Daily cadence - just show the single date
                    formatted_date = date_obj.strftime('%b %d, %Y')
                else:
                    # Multi-day cadence (e.g., every 3 days)
                    interval_end = date_obj
                    interval_start = date_obj - timedelta(days=interval - 1)
                    formatted_date = f"{interval_start.strftime('%b %d')} - {interval_end.strftime('%b %d, %Y')}"
            elif cadence_unit == 'weeks':
                # Weekly cadence - period is (interval * 7) days
                interval_end = date_obj
                interval_start = date_obj - timedelta(days=(interval * 7) - 1)
                formatted_date = f"{interval_start.strftime('%b %d')} - {interval_end.strftime('%b %d, %Y')}"
            elif cadence_unit == 'months':
                # Monthly cadence - period starts from previous occurrence + 1 day
                interval_end = date_obj
                # Go back by cadence_interval months, then add 1 day
                prev_occurrence = date_obj - relativedelta(months=interval)
                interval_start = prev_occurrence + timedelta(days=1)
                formatted_date = f"{interval_start.strftime('%b %d')} - {interval_end.strftime('%b %d, %Y')}"
            elif cadence_unit == 'years':
                # Yearly cadence - period starts from previous occurrence + 1 day
                interval_end = date_obj
                prev_occurrence = date_obj - relativedelta(years=interval)
                interval_start = prev_occurrence + timedelta(days=1)
                formatted_date = f"{interval_start.strftime('%b %d, %Y')} - {interval_end.strftime('%b %d, %Y')}"
            else:
                formatted_date = date_obj.strftime('%b %d, %Y')
        elif date_obj:
            formatted_date = date_obj.strftime('%b %d, %Y')
        
        # Format the notification message based on entry type
        if table == 'income_entries':
            # Income message
            if cadence_str and formatted_date:
                message = f"Your income for \"{category_name}\" for the {cadence_str} {formatted_date} is more than expected!"
            elif cadence_str:
                message = f"Your income for \"{category_name}\" for the {cadence_str} is more than expected!"
            else:
                message = f"Your income for \"{category_name}\" is more than expected!"
        elif table == 'c_expense_entries':
            # Credit expense message - include credit account name
            account_part = f" on \"{credit_account_name}\"" if credit_account_name else ""
            if cadence_str and formatted_date:
                message = f"You've spent more than the allowance for credit expense category \"{category_name}\"{account_part} for the {cadence_str} {formatted_date}."
            elif cadence_str:
                message = f"You've spent more than the allowance for credit expense category \"{category_name}\"{account_part} for the {cadence_str}."
            else:
                message = f"You've spent more than the allowance for credit expense category \"{category_name}\"{account_part}."
        else:
            # Regular expense message
            if cadence_str and formatted_date:
                message = f"You've spent more than the allowance for expense category \"{category_name}\" for the {cadence_str} {formatted_date}."
            elif cadence_str:
                message = f"You've spent more than the allowance for expense category \"{category_name}\" for the {cadence_str}."
            else:
                message = f"You've spent more than the allowance for expense category \"{category_name}\"."
        
        # Insert notification directly into MySQL (avoid circular import with app.py)
        # First, deduplicate: delete any existing bucket-depleted notification for this category
        notification_date = datetime.now()
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            # Delete previous bucket-depleted notifications for same category
            if table == 'income_entries':
                dedup_pattern = f'%income for "{category_name}"%more than expected%'
            elif table == 'c_expense_entries':
                dedup_pattern = f'%allowance for credit expense category "{category_name}"%'
            else:
                dedup_pattern = f'%allowance for expense category "{category_name}"%'
            
            cursor.execute("""
                DELETE FROM notifications
                WHERE user_id = %s AND message LIKE %s
            """, (user_id, dedup_pattern))
            dedup_count = cursor.rowcount
            if dedup_count > 0:
                log_info(logger, 'BUCKET_NOTIFICATION', f"Deduplicated {dedup_count} old notification(s) for category \"{category_name}\"")
            
            cursor.execute("""
                INSERT INTO notifications (user_id, date, message, is_read)
                VALUES (%s, %s, %s, 0)
            """, (user_id, notification_date, message))
            notification_id = cursor.lastrowid
            
            # Check if user has email notifications enabled
            cursor.execute("""
                SELECT email, email_notifications, first_name
                FROM users
                WHERE id = %s
            """, (user_id,))
            user = cursor.fetchone()
            
            conn.commit()
            cursor.close()
        
        # Invalidate Redis notifications cache
        if redis_manager._redis_client:
            try:
                redis_manager._redis_client.delete(f"notifications:v1:{user_id}")
            except Exception:
                pass
        
        log_info(logger, 'BUCKET_NOTIFICATION', f"Created notification {notification_id}: {message}")
        
        # Send email notification if enabled - same helper as app.py's
        # add_notification, so both paths apply identical rules.
        if user:
            try:
                from email_utils import send_notification_email_for_user
                if send_notification_email_for_user(user, message, notification_date,
                                                   kind='allowance_spent'):
                    log_info(logger, 'BUCKET_NOTIFICATION', 'Notification email sent')
            except Exception as e:
                log_error(logger, 'BUCKET_NOTIFICATION', f"Failed to send email: {e}")
                
    except Exception as e:
        log_error(logger, 'BUCKET_NOTIFICATION', f"Error creating notification: {e}")


def subtract_from_bucket(table, bucket_id, subtract_amount, user_id):
    """
    Subtract an amount from a bucket entry. Deletes the bucket if it reaches <= 0.
    Works with Redis-cached entries.
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        bucket_id: The ID of the bucket entry
        subtract_amount: Amount to subtract (Decimal or float)
        user_id: The user ID
        
    Returns:
        True if bucket was depleted and deleted, False if still has amount remaining
    """
    
    subtract_amount = Decimal(str(subtract_amount))
    log_info(logger, 'SUBTRACT_BUCKET', f"Starting: table={table}, bucket_id={bucket_id}, subtract_amount={subtract_amount}, user_id={user_id}")
    
    # Get the bucket from Redis (source of truth)
    redis_key = f"{table}:v1:{user_id}"
    log_info(logger, 'SUBTRACT_BUCKET', f"Redis key: {redis_key}, redis_client exists: {redis_manager._redis_client is not None}")
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    log_info(logger, 'SUBTRACT_BUCKET', f"Redis data exists: {redis_data is not None}")
    
    if not redis_data:
        log_error(logger, 'SUBTRACT_BUCKET', f"No Redis data found for user {user_id}, table {table}")
        return False
    
    # Parse Redis data (stored as array of entry objects)
    try:
        entries_list = json.loads(redis_data)
        log_info(logger, 'SUBTRACT_BUCKET', f"Parsed Redis data as list, length={len(entries_list)}")
        
        # Find the bucket entry in the list
        bucket_entry = None
        bucket_index = None
        for i, entry in enumerate(entries_list):
            if entry.get('id') == bucket_id and entry.get('is_bucket') == 1:
                bucket_entry = entry
                bucket_index = i
                break
        
        if not bucket_entry:
            log_warning(logger, 'SUBTRACT_BUCKET', f"Bucket ID {bucket_id} not found in Redis entries list")
            return False
        
        # Bucket found in Redis
        log_info(logger, 'SUBTRACT_BUCKET', f"Found bucket in Redis at index {bucket_index}: {bucket_entry}")
        current_amount = Decimal(str(bucket_entry.get('amount', 0)))
        new_amount = current_amount - subtract_amount
        log_info(logger, 'SUBTRACT_BUCKET', f"Current amount: {current_amount}, new amount: {new_amount}")
        
        if new_amount <= 0:
            # Bucket is depleted, delete it from Redis
            category_id = bucket_entry.get('category_id')
            bucket_date = bucket_entry.get('date')
            original_amount = bucket_entry.get('original_amount', current_amount)
            recurring_id = bucket_entry.get('recurring_id')
            
            del entries_list[bucket_index]
            redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_list))
            # Mark table as dirty for flush
            redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
            redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
            # Mark entry for deletion in MySQL
            redis_manager._redis_client.sadd(f"pending_deletes:{table}:{user_id}", str(bucket_id))
            redis_manager._redis_client.expire(f"pending_deletes:{table}:{user_id}", 604800)
            log_info(logger, 'SUBTRACT_BUCKET', f"Bucket depleted and deleted from Redis")
            
            # Create notification for depleted bucket
            try:
                _create_bucket_depleted_notification(user_id, table, category_id, bucket_date, original_amount, recurring_id)
            except Exception as e:
                log_error(logger, 'SUBTRACT_BUCKET', f"Failed to create notification: {e}")
            
            return True
        else:
            # Update bucket with new amount in Redis
            entries_list[bucket_index]['amount'] = float(new_amount)
            redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_list))
            # Mark table as dirty for flush
            redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
            redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
            log_info(logger, 'SUBTRACT_BUCKET', f"Bucket updated in Redis with new amount: {new_amount}")
            return False
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_error(logger, 'SUBTRACT_BUCKET', f"Error parsing Redis data: {e}")
        return False


def add_to_bucket(table, bucket_id, add_amount, user_id):
    """
    Add amount back to a bucket entry (used when deleting manual entries).
    
    Works with Redis-cached entries.
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        bucket_id: The ID of the bucket entry
        add_amount: Amount to add back (Decimal or float)
        user_id: The user ID
        
    Returns:
        True if bucket was successfully updated
    """
    from flask import current_app
    
    add_amount = Decimal(str(add_amount))
    log_info(logger, 'ADD_TO_BUCKET', f"Starting: table={table}, bucket_id={bucket_id}, add_amount={add_amount}, user_id={user_id}")
    
    # Get the bucket from Redis (source of truth)
    redis_key = f"{table}:v1:{user_id}"
    log_info(logger, 'ADD_TO_BUCKET', f"Looking for Redis key: {redis_key}")
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    log_info(logger, 'ADD_TO_BUCKET', f"Redis data found: {redis_data is not None}, length: {len(redis_data) if redis_data else 0}")
    
    if not redis_data:
        log_error(logger, 'ADD_TO_BUCKET', f"No Redis data found for user {user_id}, table {table}")
        return False
    
    # Parse Redis data (stored as array of entry objects)
    try:
        entries_list = json.loads(redis_data)
        log_info(logger, 'ADD_TO_BUCKET', f"Parsed Redis data as list, length={len(entries_list)}, looking for bucket_id={bucket_id}")
        
        # Find the bucket entry in the list
        bucket_entry = None
        bucket_index = None
        for i, entry in enumerate(entries_list):
            entry_id = entry.get('id')
            entry_is_bucket = entry.get('is_bucket')
            log_info(logger, 'ADD_TO_BUCKET', f"Checking entry {i}: id={entry_id}, is_bucket={entry_is_bucket}")
            if entry_id == bucket_id and entry_is_bucket == 1:
                bucket_entry = entry
                bucket_index = i
                log_info(logger, 'ADD_TO_BUCKET', f"FOUND MATCH at index {i}")
                break
        
        if not bucket_entry:
            log_warning(logger, 'ADD_TO_BUCKET', f"Bucket ID {bucket_id} not found in Redis entries list")
            return False
        
        # Bucket found in Redis
        log_info(logger, 'ADD_TO_BUCKET', f"Found bucket in Redis at index {bucket_index}: {bucket_entry}")
        current_amount = Decimal(str(bucket_entry.get('amount', 0)))
        original_amount = Decimal(str(bucket_entry.get('original_amount', 0)))
        new_amount = current_amount + add_amount
        
        # Don't exceed original amount
        if new_amount > original_amount:
            new_amount = original_amount
        
        log_info(logger, 'ADD_TO_BUCKET', f"Current: {current_amount}, Add: {add_amount}, New: {new_amount}, Original: {original_amount}")
        
        # Update bucket with new amount in Redis
        entries_list[bucket_index]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_list))
        # Mark table as dirty for flush
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        log_info(logger, 'ADD_TO_BUCKET', f"Bucket updated in Redis with new amount: {new_amount}")
        return True
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_error(logger, 'ADD_TO_BUCKET', f"Error parsing Redis data: {e}")
        return False


def cleanup_unfilled_buckets(table, user_id, cutoff_date=None):
    """
    Remove bucket entries that are past their interval and still have amount > 0.
    Redis-first: removes from Redis and marks for MySQL deletion.
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: The user ID
        cutoff_date: Date before which to remove buckets (defaults to today)
        
    Returns:
        Number of buckets deleted
    """
    if cutoff_date is None:
        cutoff_date = date.today()
    elif isinstance(cutoff_date, str):
        cutoff_date = date.fromisoformat(cutoff_date)
    
    # Get entries from Redis
    redis_key = f"{table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        return 0
    
    try:
        entries_list = json.loads(redis_data)
        
        # Find buckets to delete (is_bucket=1, amount>0, date<cutoff)
        buckets_to_delete = []
        entries_to_keep = []
        
        for entry in entries_list:
            if (entry.get('is_bucket') == 1 and 
                float(entry.get('amount', 0)) > 0 and 
                date.fromisoformat(entry.get('date')) < cutoff_date):
                buckets_to_delete.append(entry['id'])
            else:
                entries_to_keep.append(entry)
        
        if not buckets_to_delete:
            return 0
        
        # Update Redis with remaining entries
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_to_keep))
        
        # Mark table as dirty for flush
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        
        # Mark buckets for deletion in MySQL
        if buckets_to_delete:
            pending_key = f"pending_deletes:{table}:{user_id}"
            for bucket_id in buckets_to_delete:
                redis_manager._redis_client.sadd(pending_key, str(bucket_id))
            redis_manager._redis_client.expire(pending_key, 604800)
        
        return len(buckets_to_delete)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_error(logger, 'CLEANUP_BUCKETS', f"Error parsing Redis data: {e}")
        return 0


def restore_bucket_for_category_change(bucket_table, category_id, entry_date, entry_amount, user_id, entry_type):
    """
    Restore bucket amount when an auto-confirmed entry's category is changed.
    
    When auto-confirm reduces a bucket for category A, and user later changes to category B,
    we need to add the amount back to category A's bucket.
    
    Args:
        bucket_table: The bucket table name (e.g., 'recurring_income_buckets')
        category_id: The OLD category ID (the one being restored)
        entry_date: Date of the entry
        entry_amount: Amount to restore to the bucket
        user_id: The user ID
        entry_type: 'income', 'expense', or 'c_expense'
        
    Returns:
        True if bucket was restored, False otherwise
    """
    from flask import current_app
    from datetime import date as date_type
    
    log_info(logger, 'RESTORE_BUCKET_CHANGE', f"Starting: bucket_table={bucket_table}, category={category_id}, date={entry_date}, amount={entry_amount}")
    
    if not bucket_table:
        log_info(logger, 'RESTORE_BUCKET_CHANGE', f"No bucket table for entry_type={entry_type}")
        return False
    
    try:
        entry_amount = Decimal(str(entry_amount))
        
        # Parse entry_date if string
        if isinstance(entry_date, str):
            entry_date = date_type.fromisoformat(entry_date)
        
        # Get bucket records from Redis
        bucket_key = f"{bucket_table}:v1:{user_id}"
        bucket_data = redis_manager._redis_client.get(bucket_key) if redis_manager._redis_client else None
        
        if not bucket_data:
            log_info(logger, 'RESTORE_BUCKET_CHANGE', f"No bucket data found for {bucket_key}")
            return False
        
        buckets = json.loads(bucket_data)
        
        # Find bucket for this category whose period contains the entry date
        bucket_found = None
        bucket_idx = None
        
        for idx, bucket in enumerate(buckets):
            if bucket.get('category_id') != int(category_id):
                continue
            
            bucket_date = bucket.get('bucket_date')
            if isinstance(bucket_date, str):
                bucket_date = date_type.fromisoformat(bucket_date)
            
            # Simple check: entry_date should be <= bucket_date
            # (bucket_date is the END of the period)
            if entry_date <= bucket_date:
                bucket_found = bucket
                bucket_idx = idx
                log_info(logger, 'RESTORE_BUCKET_CHANGE', f"Found bucket at idx {idx}, bucket_date={bucket_date}")
                break
        
        if not bucket_found:
            log_info(logger, 'RESTORE_BUCKET_CHANGE', f"No matching bucket found for category {category_id}")
            return False
        
        # Add the amount back to the bucket (cap at original_amount)
        current_amount = Decimal(str(bucket_found.get('amount', 0)))
        original_amount = Decimal(str(bucket_found.get('original_amount', 0)))
        new_amount = current_amount + entry_amount
        
        # Don't exceed original amount
        if new_amount > original_amount:
            new_amount = original_amount
        
        log_info(logger, 'RESTORE_BUCKET_CHANGE', f"Updating bucket: current={current_amount}, adding={entry_amount}, new={new_amount}, max={original_amount}")
        
        # Update the bucket in Redis
        buckets[bucket_idx]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(bucket_key, 604800, json.dumps(buckets))
        
        # Mark bucket table as dirty
        dirty_key = f"dirty_tables:{user_id}"
        redis_manager._redis_client.sadd(dirty_key, bucket_table)
        redis_manager._redis_client.expire(dirty_key, 604800)
        
        log_info(logger, 'RESTORE_BUCKET_CHANGE', f"Successfully restored {entry_amount} to bucket for category {category_id}")
        return True
        
    except Exception as e:
        log_exception(logger, 'RESTORE_BUCKET_CHANGE', f"Error: {e}")
        return False


def _is_bundle_category(entry_table, category_id, user_id):
    """Is this category one a bundle created?"""
    category_table = {
        'expense_entries': 'expense_categories',
        'c_expense_entries': 'c_expense_categories',
    }.get(entry_table)
    if not category_table:
        return False
    try:
        cached = (redis_manager._redis_client.get(f"{category_table}:v1:{user_id}")
                  if redis_manager._redis_client else None)
        if not cached:
            return False
        for cat in json.loads(cached):
            if int(cat.get('id', 0) or 0) == int(category_id):
                return bool(cat.get('is_bundle')) or cat.get('bundle_id') is not None
    except Exception:
        pass
    return False


def _get_wage_bill_for_category(entry_table, category_id, user_id):
    """Get wage_bill flag for a category's recurring record from Redis."""
    # A bundle has no recurring record, so the lookup below would return 0 -
    # and wage_bill=0 makes find_next_bucket_for_category search only
    # [today, today+45d]. A bundle item planned for last week would then be
    # unreachable, and today's purchase would deplete the NEXT item's plan
    # instead. Bundles are all-or-nothing: one plan, one purchase, gone.
    if _is_bundle_category(entry_table, category_id, user_id):
        return 1

    table_map = {
        'income_entries': 'recurring_income',
        'expense_entries': 'recurring_expense',
        'c_expense_entries': 'recurring_c_expense'
    }
    recurring_table = table_map.get(entry_table)
    if not recurring_table:
        return 0
    try:
        redis_key = f"{recurring_table}:v1:{user_id}"
        cached = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
        if cached:
            recurring_list = json.loads(cached)
            for rec in recurring_list:
                if int(rec.get('category_id', 0)) == int(category_id):
                    return int(rec.get('wage_bill', 0))
    except Exception:
        pass
    return 0


def _find_bucket_date_for_entry(entry_date, cadence_info):
    """
    Given an entry_date and cadence_info, determine which bucket_date (from
    recurring_*_buckets) this entry falls within.
    
    For monthly recurring on day X: entry on April 3 with monthly_days=1
    means the bucket_date is April 1 (the 1st of the entry's month, or the 
    previous month's Xth if entry_date < X).
    
    Returns: date object for the bucket_date, or None if can't determine.
    """
    if isinstance(entry_date, str):
        entry_date = date.fromisoformat(entry_date)
    
    cadence_unit = cadence_info.get('cadence_unit', 'months')
    
    if cadence_unit == 'months':
        monthly_days = cadence_info.get('monthly_days')
        if monthly_days:
            # Parse first monthly day
            if isinstance(monthly_days, str):
                day = int(monthly_days.split(',')[0].strip())
            else:
                day = int(monthly_days)
            
            # Try current month first
            last_day = calendar.monthrange(entry_date.year, entry_date.month)[1]
            bucket_day = min(day, last_day)
            candidate = date(entry_date.year, entry_date.month, bucket_day)
            
            if entry_date >= candidate:
                # Entry is on or after this month's bucket day — use this month
                return candidate
            else:
                # Entry is before this month's bucket day — use previous month
                if entry_date.month == 1:
                    prev_year, prev_month = entry_date.year - 1, 12
                else:
                    prev_year, prev_month = entry_date.year, entry_date.month - 1
                prev_last_day = calendar.monthrange(prev_year, prev_month)[1]
                return date(prev_year, prev_month, min(day, prev_last_day))
    
    elif cadence_unit == 'weeks':
        # For weekly, look back up to 7 days for the most recent bucket date
        weekdays = cadence_info.get('weekdays')
        if weekdays:
            weekday_list = [int(d.strip()) for d in str(weekdays).split(',')]
            for days_back in range(7):
                check_date = entry_date - timedelta(days=days_back)
                if check_date.weekday() in weekday_list:
                    return check_date
    
    elif cadence_unit == 'years':
        yearly_day = cadence_info.get('yearly_day')
        yearly_month = cadence_info.get('yearly_month')
        if yearly_day and yearly_month:
            yearly_day = int(yearly_day)
            yearly_month = int(yearly_month)
            candidate = date(entry_date.year, yearly_month, yearly_day)
            if entry_date >= candidate:
                return candidate
            else:
                return date(entry_date.year - 1, yearly_month, yearly_day)
    
    return None


def process_manual_entry_with_bucket(table, category_id, entry_date, entry_amount, user_id, cadence_info=None):
    """
    Process a manual entry by checking for and depleting bucket entries.
    
    NEW LOGIC (Feb 2026):
    - Only reduces bucket if entry_date <= today
    - Finds the NEXT bucket in the category (earliest date > today)
    - Does NOT use cadence-based period matching
    
    Wage/Bill vs Variable/Allowance:
    - wage_bill=0 (Variable/Allowance): Gradual depletion by entry_amount
    - wage_bill=1 (Wage/Bill): Complete bucket removal on any entry
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        entry_date: Date of the manual entry (date object or string)
        entry_amount: Amount of the manual entry (Decimal or float)
        user_id: The user ID
        cadence_info: Dictionary with recurring info (may contain wage_bill)
    """
    from flask import current_app
    from datetime import date as date_type
    
    # Parse entry_date
    if isinstance(entry_date, str):
        entry_date = date_type.fromisoformat(entry_date)
    
    today = date_type.today()
    
    log_info(logger, 'BUCKET_DEBUG', f"process_manual_entry_with_bucket called: table={table}, category_id={category_id}, entry_date={entry_date}, entry_amount={entry_amount}, user_id={user_id}")
    
    # Determine wage_bill from cadence_info where the caller supplied it, and
    # look it up otherwise. Defaulting to 0 meant this and the probe in app.py,
    # which does look it up, could pick different buckets for the same entry -
    # exactly what find_next_bucket_for_category's docstring warns about. It
    # also silently made every bundle an allowance.
    if cadence_info and isinstance(cadence_info, dict) and 'wage_bill' in cadence_info:
        wage_bill = int(cadence_info.get('wage_bill') or 0)
    else:
        wage_bill = _get_wage_bill_for_category(table, category_id, user_id)
    
    log_info(logger, 'BUCKET_DEBUG', f"wage_bill={wage_bill}")
    
    # NEW LOGIC: Only reduce bucket if entry_date <= today
    if entry_date > today:
        log_info(logger, 'BUCKET_DEBUG', f"Entry date {entry_date} is in the future (> {today}), skipping bucket reduction")
        return
    
    # Bills reach back for the occurrence they are paying late; allowances take
    # the next bucket forward. wage_bill is passed rather than looked up again:
    # cadence_info is the caller's own answer for this entry.
    bucket = find_next_bucket_for_category(table, category_id, user_id,
                                           wage_bill=wage_bill)
    
    log_info(logger, 'BUCKET_DEBUG', f"Found next bucket: {bucket}")
    
    if bucket:
        # First subtract from the bucket RECORD (tracks overspending)
        from recurring_bucket_manager import subtract_from_bucket_record_by_category_date, get_bucket_table_for_entry_table, get_bucket_record_by_category_date
        bucket_table = get_bucket_table_for_entry_table(table)
        bucket_date = bucket.get('date')
        if isinstance(bucket_date, str):
            bucket_date = date_type.fromisoformat(bucket_date)
        
        # Get current bucket record amount BEFORE subtraction
        bucket_record_before = None
        if bucket_table:
            bucket_record_before = get_bucket_record_by_category_date(bucket_table, category_id, bucket_date, user_id)
            log_info(logger, 'BUCKET_DEBUG', f"Bucket record before subtraction: {bucket_record_before}")
        
        # For wage_bill=1: subtract the FULL bucket amount to remove it completely
        # For variable/allowance: subtract only the entry_amount (gradual depletion)
        if wage_bill:
            bucket_current_amount = Decimal(str(bucket.get('amount', 0)))
            subtract_amount_entry = bucket_current_amount
            log_info(logger, 'BUCKET_DEBUG', f"Wage/Bill mode: subtracting full bucket amount {bucket_current_amount} to remove completely")
        else:
            subtract_amount_entry = entry_amount
        
        # Subtract from bucket entry (deletes if it hits 0)
        entry_deleted = subtract_from_bucket(table, bucket['id'], subtract_amount_entry, user_id)
        log_info(logger, 'BUCKET_DEBUG', f"Bucket entry subtraction result: {entry_deleted} (True=deleted, False=still remaining)")
        
        # For wage_bill=1: subtract the FULL record amount to set it to 0
        # For variable/allowance: subtract entry_amount from record
        if wage_bill and bucket_record_before:
            record_current_amount = Decimal(str(bucket_record_before.get('amount', 0)))
            subtract_amount_record = record_current_amount
            log_info(logger, 'BUCKET_DEBUG', f"Wage/Bill mode: subtracting full record amount {record_current_amount}")
        else:
            subtract_amount_record = entry_amount
        
        # Subtract from bucket record (allows negative amounts for overspending)
        if bucket_table and bucket_record_before:
            record_depleted = subtract_from_bucket_record_by_category_date(bucket_table, category_id, bucket_date, subtract_amount_record, user_id)
            log_info(logger, 'BUCKET_DEBUG', f"Bucket record subtraction result: {record_depleted} (amount went to/below 0)")
            
            # CRITICAL: If bucket entry still exists but bucket record went negative,
            # we need to delete the bucket entry to reflect the overspending state
            if not entry_deleted and record_depleted:
                # Record went to 0 or negative, but entry wasn't fully depleted yet
                # Force delete the bucket entry since record is tracking the overspending
                log_info(logger, 'BUCKET_DEBUG', f"Bucket record went to/below 0, force deleting bucket entry {bucket['id']}")
                # Delete from Redis
                redis_key = f"{table}:v1:{user_id}"
                redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
                if redis_data:
                    entries_list = json.loads(redis_data)
                    entries_list = [e for e in entries_list if not (e.get('id') == bucket['id'] and e.get('is_bucket') == 1)]
                    redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_list))
                    redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
                    redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
                    # Mark for MySQL deletion
                    redis_manager._redis_client.sadd(f"pending_deletes:{table}:{user_id}", str(bucket['id']))
                    redis_manager._redis_client.expire(f"pending_deletes:{table}:{user_id}", 604800)
                    log_info(logger, 'BUCKET_DEBUG', f"Forced bucket entry deletion complete")
    else:
        # No bucket entry found — but there may be a bucket RECORD for the entry's period
        # (e.g., bucket entry was cleaned up by nightly sync but record still shows full amount)
        log_info(logger, 'BUCKET_DEBUG', f"No bucket entry found, checking for bucket record to reduce directly")
        try:
            from recurring_bucket_manager import subtract_from_bucket_record_by_category_date, get_bucket_table_for_entry_table, get_bucket_record_by_category_date
            bucket_table = get_bucket_table_for_entry_table(table)
            if bucket_table and cadence_info:
                # Use cadence info to find the bucket record for this entry's period
                cadence_unit = cadence_info.get('cadence_unit', 'months')
                monthly_days = cadence_info.get('monthly_days')
                start_date_raw = cadence_info.get('start_date')
                
                # Determine the bucket_date for the entry's billing period
                bucket_date_for_record = _find_bucket_date_for_entry(entry_date, cadence_info)
                
                if bucket_date_for_record:
                    record = get_bucket_record_by_category_date(bucket_table, category_id, bucket_date_for_record, user_id)
                    if record and float(record.get('amount', 0)) > 0:
                        if wage_bill:
                            subtract_amount = Decimal(str(record.get('amount', 0)))
                        else:
                            subtract_amount = entry_amount
                        log_info(logger, 'BUCKET_DEBUG', f"Found bucket record at {bucket_date_for_record}, reducing by {subtract_amount} (wage_bill={wage_bill})")
                        subtract_from_bucket_record_by_category_date(bucket_table, category_id, bucket_date_for_record, float(subtract_amount), user_id)
                    else:
                        log_info(logger, 'BUCKET_DEBUG', f"No bucket record found at {bucket_date_for_record} or already at 0")
                else:
                    log_info(logger, 'BUCKET_DEBUG', f"Could not determine bucket_date for entry_date={entry_date}")
        except Exception as record_err:
            log_warning(logger, 'BUCKET_DEBUG', f"Error reducing bucket record directly: {record_err}")


def restore_bucket_for_deleted_entry_v2(table, category_id, deleted_entry_amount, user_id,
                                        only_future=False):
    """
    Simplified bucket restoration when a manual entry is deleted.
    
    NEW LOGIC (Feb 2026):
    - Finds the NEXT bucket in the category (earliest date > today)
    - Adds the deleted amount back to that bucket
    - If bucket entry was deleted (went negative), recreate it from bucket record
    - Does NOT use cadence-based logic
    
    Wage/Bill vs Variable/Allowance:
    - wage_bill=0: Restore deleted_entry_amount (gradual)
    - wage_bill=1: Restore full original_amount (binary paid/unpaid)
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        deleted_entry_amount: Amount of the deleted entry
        user_id: The user ID
        only_future: Restore only a bucket still ahead of the user. Used when an
            entry is moved into the future rather than deleted: a bucket whose date
            has passed belongs to a period that is over, and putting money back into
            it would raise a forecast nobody can act on. Deletion keeps the old
            behaviour and restores the earliest depleted one, past or not.
        
    Returns:
        Tuple of (success: bool, bucket: dict or None)
    """
    from flask import current_app
    from datetime import date as date_type
    from recurring_bucket_manager import add_to_bucket_record_by_category_date, get_bucket_table_for_entry_table, get_bucket_records_for_category
    
    log_info(logger, 'RESTORE_BUCKET_V2', f"Attempting restore: table={table}, category={category_id}, amount={deleted_entry_amount}")
    
    # Determine wage_bill for this category
    wage_bill = _get_wage_bill_for_category(table, category_id, user_id)
    log_info(logger, 'RESTORE_BUCKET_V2', f"wage_bill={wage_bill}")
    
    bucket_table = get_bucket_table_for_entry_table(table)
    # The user's day, not the server's: whether a bucket counts as past decides
    # whether it is restored at all, and a server ahead of the user would call
    # today's bucket yesterday's for several hours every evening.
    from bucket_confirmation import _user_today
    today = _user_today(user_id)
    
    # FIRST: Check for depleted bucket records (amount < original_amount).
    # When there are multiple future buckets, the depleted one is the correct
    # target — not the next undepleted one that find_next_bucket_for_category returns.
    # Use 45-day lookback to catch current-period records.
    if bucket_table:
        all_records = get_bucket_records_for_category(bucket_table, category_id, user_id)
        lookback_date = today - timedelta(days=45)
        depleted_records = []
        for record in all_records:
            record_date = record.get('bucket_date')
            if isinstance(record_date, str):
                record_date = date_type.fromisoformat(record_date)
            record_amount = float(record.get('amount', 0))
            record_original = float(record.get('original_amount', 0))
            if only_future and record_date <= today:
                continue
            if record_date >= lookback_date and record_amount < record_original:
                depleted_records.append((record_date, record))
        
        if depleted_records:
            # Restore the earliest depleted record
            depleted_records.sort(key=lambda x: x[0])
            bucket_date, depleted_record = depleted_records[0]
            log_info(logger, 'RESTORE_BUCKET_V2', f"Found depleted bucket record: date={bucket_date}, amount={depleted_record.get('amount')}, original={depleted_record.get('original_amount')}")
            
            # Determine restore amount
            if wage_bill:
                original_amount_val = Decimal(str(depleted_record.get('original_amount', 0)))
                current_amount_val = Decimal(str(depleted_record.get('amount', 0)))
                restore_amount = original_amount_val - current_amount_val
                log_info(logger, 'RESTORE_BUCKET_V2', f"Wage/Bill: restoring to original={original_amount_val} (adding {restore_amount})")
            else:
                restore_amount = Decimal(str(deleted_entry_amount))
            
            if restore_amount > 0:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, float(restore_amount), user_id)
            
            # Check if a bucket entry exists for this record's date
            bucket_entry = None
            from app import _get_entries_from_redis
            entries = _get_entries_from_redis(table, user_id)
            if entries:
                for entry in entries:
                    if (entry.get('category_id') == int(category_id) and
                        entry.get('is_bucket') == 1 and
                        entry.get('date') == bucket_date.isoformat()):
                        bucket_entry = entry
                        break
            
            if bucket_entry:
                # Entry exists — add to it
                bucket_id = bucket_entry.get('id')
                if wage_bill:
                    original_amount = Decimal(str(bucket_entry.get('original_amount', bucket_entry.get('amount', 0))))
                    current_amount = Decimal(str(bucket_entry.get('amount', 0)))
                    entry_restore = original_amount - current_amount
                    if entry_restore > 0:
                        add_to_bucket(table, bucket_id, entry_restore, user_id)
                else:
                    add_to_bucket(table, bucket_id, Decimal(str(deleted_entry_amount)), user_id)
                log_info(logger, 'RESTORE_BUCKET_V2', f"Restored existing bucket entry {bucket_id}")
            else:
                # Entry was deleted (fully depleted) — recreate if record is now positive
                updated_records = get_bucket_records_for_category(bucket_table, category_id, user_id)
                updated_record = next((r for r in updated_records if r.get('bucket_date') == bucket_date.isoformat() or r.get('bucket_date') == bucket_date), None)
                if updated_record:
                    new_amount = float(updated_record.get('amount', 0))
                    original_amount = float(updated_record.get('original_amount', new_amount))
                    if new_amount > 0:
                        from app import _update_entry_in_redis
                        _update_entry_in_redis(
                            table, user_id, int(category_id),
                            bucket_date.isoformat() if isinstance(bucket_date, date_type) else bucket_date,
                            new_amount,
                            is_bucket=True,
                            original_amount=original_amount
                        )
                        log_info(logger, 'RESTORE_BUCKET_V2', f"Recreated bucket entry for {bucket_date} with amount {new_amount}")
                    else:
                        log_info(logger, 'RESTORE_BUCKET_V2', f"Amount still non-positive ({new_amount}), not recreating entry")
            
            updated_bucket = find_next_bucket_for_category(table, category_id, user_id)
            return (True, updated_bucket)
    
    # FALLBACK: No depleted records found. Try adding to an existing undepleted bucket entry.
    bucket = find_next_bucket_for_category(table, category_id, user_id)
    
    if bucket:
        bucket_id = bucket.get('id')
        bucket_date = bucket.get('date')
        if isinstance(bucket_date, str):
            bucket_date = date_type.fromisoformat(bucket_date)
        
        log_info(logger, 'RESTORE_BUCKET_V2', f"No depleted records. Found undepleted bucket entry: id={bucket_id}, date={bucket_date}")
        
        if wage_bill:
            original_amount = Decimal(str(bucket.get('original_amount', bucket.get('amount', 0))))
            current_amount = Decimal(str(bucket.get('amount', 0)))
            restore_amount = original_amount - current_amount
            if restore_amount > 0:
                add_to_bucket(table, bucket_id, restore_amount, user_id)
            if bucket_table:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, float(restore_amount), user_id)
        else:
            add_to_bucket(table, bucket_id, Decimal(str(deleted_entry_amount)), user_id)
            if bucket_table:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, deleted_entry_amount, user_id)
        
        updated_bucket = find_next_bucket_for_category(table, category_id, user_id)
        return (True, updated_bucket)
    
    else:
        # No bucket entry AND no depleted records — check for any future record
        log_info(logger, 'RESTORE_BUCKET_V2', f"No bucket entry or depleted records found, checking for any future records...")
        
        if not bucket_table:
            return (False, None)
        
        all_records = get_bucket_records_for_category(bucket_table, category_id, user_id)
        if not all_records:
            log_info(logger, 'RESTORE_BUCKET_V2', f"No bucket records found")
            return (False, None)
        
        lookback_date_fb = today - timedelta(days=45)
        future_records = []
        for record in all_records:
            record_date = record.get('bucket_date')
            if isinstance(record_date, str):
                record_date = date_type.fromisoformat(record_date)
            if record_date >= lookback_date_fb:
                future_records.append((record_date, record))
        
        if not future_records:
            log_info(logger, 'RESTORE_BUCKET_V2', f"No future bucket records found")
            return (False, None)
        
        future_records.sort(key=lambda x: x[0])
        bucket_date, bucket_record = future_records[0]
        
        if wage_bill:
            original_amount_val = Decimal(str(bucket_record.get('original_amount', 0)))
            current_amount_val = Decimal(str(bucket_record.get('amount', 0)))
            restore_amount = original_amount_val - current_amount_val
            if restore_amount > 0:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, float(restore_amount), user_id)
        else:
            add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, deleted_entry_amount, user_id)
        
        updated_records = get_bucket_records_for_category(bucket_table, category_id, user_id)
        updated_record = next((r for r in updated_records if r.get('bucket_date') == bucket_date.isoformat() or r.get('bucket_date') == bucket_date), None)
        if updated_record:
            new_amount = float(updated_record.get('amount', 0))
            original_amount = float(updated_record.get('original_amount', new_amount))
            if new_amount > 0:
                from app import _update_entry_in_redis
                _update_entry_in_redis(
                    table, user_id, int(category_id),
                    bucket_date.isoformat() if isinstance(bucket_date, date_type) else bucket_date,
                    new_amount,
                    is_bucket=True,
                    original_amount=original_amount
                )
                log_info(logger, 'RESTORE_BUCKET_V2', f"Recreated bucket entry for {bucket_date} with amount {new_amount}")
                updated_bucket = find_next_bucket_for_category(table, category_id, user_id)
                return (True, updated_bucket)
        
        return (False, None)


def restore_bucket_for_deleted_entry(table, category_id, deleted_entry_date, deleted_entry_amount, user_id, cadence_info):
    """
    Smart bucket restoration when a manual entry is deleted.
    
    - If today is within the bucket's cadence period: restore/recreate bucket with correct amount
    - If today is past the cadence period: keep bucket deleted
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        deleted_entry_date: Date of the deleted entry
        deleted_entry_amount: Amount of the deleted entry
        user_id: The user ID
        cadence_info: Dictionary with cadence configuration
        
    Returns:
        True if bucket was restored/recreated, False otherwise
    """
    from flask import current_app
    from datetime import date as date_type, timedelta
    import calendar
    from app import _get_entries_from_redis
    
    log_info(logger, 'RESTORE_BUCKET', f"Attempting restore: table={table}, category={category_id}, entry_date={deleted_entry_date}, amount={deleted_entry_amount}")
    
    # All bucket tables now use user_id consistently for Redis key
    
    # Find the bucket that should exist for this entry (even if it was deleted)
    # First try to find an existing bucket
    bucket = find_bucket_for_entry(table, category_id, deleted_entry_date, user_id, cadence_info)
    
    # If no active bucket found, we need to find what bucket WOULD exist for this date
    # by looking at all buckets (including deleted ones) for this category
    if not bucket:
        log_info(logger, 'RESTORE_BUCKET', f"No active bucket found, checking for deleted buckets")
        
        # Get all entries including those with amount=0 or deleted
        entries = _get_entries_from_redis(table, user_id)
        if not entries:
            log_info(logger, 'RESTORE_BUCKET', f"No entries found in Redis")
            return False
        
        # Find ALL bucket entries for this category (including amount=0)
        all_buckets = [
            entry for entry in entries
            if entry.get('category_id') == int(category_id)
            and entry.get('is_bucket') == 1
        ]
        
        # Also check pending deletes for this table
        pending_key = f"pending_deletes:{table}:{user_id}"
        pending_deletes = redis_manager._redis_client.smembers(pending_key) if redis_manager._redis_client else set()
        pending_delete_ids = {int(x) if isinstance(x, (str, bytes)) else x for x in pending_deletes}
        
        log_info(logger, 'RESTORE_BUCKET', f"Found {len(all_buckets)} total buckets (active+deleted), {len(pending_delete_ids)} pending deletes")
        
        # If no bucket entries found, try looking at bucket RECORDS instead
        if not all_buckets:
            log_info(logger, 'RESTORE_BUCKET', f"No bucket entries found, checking bucket records")
            from recurring_bucket_manager import get_bucket_table_for_entry_table
            bucket_table = get_bucket_table_for_entry_table(table)
            if bucket_table:
                # Get bucket records from Redis - now uses user_id for all bucket tables
                bucket_records_key = f"{bucket_table}:v1:{user_id}"
                bucket_records_data = redis_manager._redis_client.get(bucket_records_key) if redis_manager._redis_client else None
                if bucket_records_data:
                    bucket_records = json.loads(bucket_records_data)
                    # Find records for this category
                    category_records = [r for r in bucket_records if r.get('category_id') == int(category_id)]
                    log_info(logger, 'RESTORE_BUCKET', f"Found {len(category_records)} bucket records for category {category_id}")
                    
                    # Find the record whose period would contain this entry date
                    for record in category_records:
                        record_bucket_date = record.get('bucket_date')
                        if isinstance(record_bucket_date, str):
                            record_bucket_date = date_type.fromisoformat(record_bucket_date)
                        
                        # Calculate period for this bucket record
                        period_start = None
                        if cadence_unit == 'months' and cadence_interval == 1:
                            if record_bucket_date.month == 1:
                                prev_month = 12
                                prev_year = record_bucket_date.year - 1
                            else:
                                prev_month = record_bucket_date.month - 1
                                prev_year = record_bucket_date.year
                            try:
                                period_start = record_bucket_date.replace(year=prev_year, month=prev_month)
                            except ValueError:
                                last_day = calendar.monthrange(prev_year, prev_month)[1]
                                period_start = date_type(prev_year, prev_month, last_day)
                            period_start = period_start + timedelta(days=1)
                        
                        if period_start and period_start <= deleted_entry_date <= record_bucket_date:
                            log_info(logger, 'RESTORE_BUCKET', f"Found bucket record for date {record_bucket_date}, period {period_start} to {record_bucket_date}")
                            # Create a pseudo-bucket entry structure for the restore logic
                            bucket = {
                                'id': record.get('id'),
                                'category_id': int(category_id),
                                'date': record_bucket_date.isoformat() if isinstance(record_bucket_date, date_type) else record_bucket_date,
                                'amount': 0,  # Entry doesn't exist
                                'is_bucket': 1,
                                'original_amount': record.get('original_amount', 0)
                            }
                            break
            
            if not bucket:
                log_info(logger, 'RESTORE_BUCKET', f"No bucket entries or records found for category {category_id}")
                return False
        
        # Find the bucket whose period would contain this entry date
        # Sort by date and find the one that matches
        all_buckets.sort(key=lambda x: x.get('date', ''))
        
        cadence_unit = cadence_info.get('cadence_unit')
        cadence_interval = int(cadence_info.get('cadence_interval', 1))
        
        for potential_bucket in all_buckets:
            bucket_date = potential_bucket['date']
            if isinstance(bucket_date, str):
                bucket_date = date_type.fromisoformat(bucket_date)
            
            log_info(logger, 'RESTORE_BUCKET_DEBUG', f"Checking bucket: date={bucket_date}, deleted_entry_date={deleted_entry_date}")
            
            # Calculate period for this bucket
            period_start = None
            if cadence_unit == 'days':
                period_start = bucket_date - timedelta(days=cadence_interval - 1)
            elif cadence_unit == 'weeks':
                period_start = bucket_date - timedelta(days=7 * cadence_interval - 1)
            elif cadence_unit == 'months':
                if cadence_interval == 1:
                    if bucket_date.month == 1:
                        prev_month = 12
                        prev_year = bucket_date.year - 1
                    else:
                        prev_month = bucket_date.month - 1
                        prev_year = bucket_date.year
                    try:
                        period_start = bucket_date.replace(year=prev_year, month=prev_month)
                    except ValueError:
                        last_day = calendar.monthrange(prev_year, prev_month)[1]
                        period_start = date_type(prev_year, prev_month, last_day)
                    period_start = period_start + timedelta(days=1)
            elif cadence_unit == 'years':
                new_year = bucket_date.year - cadence_interval
                try:
                    period_start = bucket_date.replace(year=new_year)
                except ValueError:
                    period_start = date_type(new_year, 2, 28)
                period_start = period_start + timedelta(days=1)
            
            log_info(logger, 'RESTORE_BUCKET_DEBUG', f"Period: {period_start} to {bucket_date}, contains entry? {period_start and period_start <= deleted_entry_date <= bucket_date if period_start else False}")
            
            if period_start and period_start <= deleted_entry_date <= bucket_date:
                # This is the bucket that should cover this entry
                bucket = potential_bucket
                log_info(logger, 'RESTORE_BUCKET', f"Found matching deleted bucket: id={bucket['id']}, date={bucket['date']}, amount={bucket.get('amount', 0)}")
                break
        
        # If still no bucket found from entries, check bucket RECORDS
        # This handles the case where the bucket entry was deleted but the record still exists
        if not bucket:
            log_info(logger, 'RESTORE_BUCKET', f"No bucket entry found covering date {deleted_entry_date}, checking bucket records")
            from recurring_bucket_manager import get_bucket_table_for_entry_table
            bucket_table = get_bucket_table_for_entry_table(table)
            if bucket_table:
                # Get bucket records from Redis - now uses user_id for all bucket tables
                bucket_records_key = f"{bucket_table}:v1:{user_id}"
                bucket_records_data = redis_manager._redis_client.get(bucket_records_key) if redis_manager._redis_client else None
                if bucket_records_data:
                    bucket_records = json.loads(bucket_records_data)
                    # Find records for this category
                    category_records = [r for r in bucket_records if r.get('category_id') == int(category_id)]
                    log_info(logger, 'RESTORE_BUCKET', f"Found {len(category_records)} bucket records for category {category_id}")
                    
                    # Find the record whose period would contain this entry date
                    for record in category_records:
                        record_bucket_date = record.get('bucket_date')
                        if isinstance(record_bucket_date, str):
                            record_bucket_date = date_type.fromisoformat(record_bucket_date)
                        
                        # Calculate period for this bucket record
                        rec_period_start = None
                        if cadence_unit == 'months' and cadence_interval == 1:
                            if record_bucket_date.month == 1:
                                prev_month = 12
                                prev_year = record_bucket_date.year - 1
                            else:
                                prev_month = record_bucket_date.month - 1
                                prev_year = record_bucket_date.year
                            try:
                                rec_period_start = record_bucket_date.replace(year=prev_year, month=prev_month)
                            except ValueError:
                                last_day = calendar.monthrange(prev_year, prev_month)[1]
                                rec_period_start = date_type(prev_year, prev_month, last_day)
                            rec_period_start = rec_period_start + timedelta(days=1)
                        elif cadence_unit == 'weeks':
                            rec_period_start = record_bucket_date - timedelta(days=7 * cadence_interval - 1)
                        elif cadence_unit == 'days':
                            rec_period_start = record_bucket_date - timedelta(days=cadence_interval - 1)
                        
                        log_info(logger, 'RESTORE_BUCKET', f"Checking record: date={record_bucket_date}, period={rec_period_start} to {record_bucket_date}, contains? {rec_period_start and rec_period_start <= deleted_entry_date <= record_bucket_date if rec_period_start else False}")
                        
                        if rec_period_start and rec_period_start <= deleted_entry_date <= record_bucket_date:
                            log_info(logger, 'RESTORE_BUCKET', f"Found bucket record for date {record_bucket_date}, period {rec_period_start} to {record_bucket_date}")
                            # Create a pseudo-bucket entry structure for the restore logic
                            bucket = {
                                'id': record.get('id'),
                                'category_id': int(category_id),
                                'date': record_bucket_date.isoformat() if isinstance(record_bucket_date, date_type) else record_bucket_date,
                                'amount': 0,  # Entry doesn't exist
                                'is_bucket': 1,
                                'original_amount': record.get('original_amount', 0)
                            }
                            break
        
        if not bucket:
            log_info(logger, 'RESTORE_BUCKET', f"No bucket (active or deleted) found for entry date {deleted_entry_date}")
            return False
    
    bucket_date = bucket['date']
    if isinstance(bucket_date, str):
        bucket_date = date_type.fromisoformat(bucket_date)
    
    # Calculate the cadence period for this bucket
    cadence_unit = cadence_info.get('cadence_unit')
    cadence_interval = int(cadence_info.get('cadence_interval', 1))
    
    period_start = None
    if cadence_unit == 'days':
        period_start = bucket_date - timedelta(days=cadence_interval - 1)
    elif cadence_unit == 'weeks':
        period_start = bucket_date - timedelta(days=7 * cadence_interval - 1)
    elif cadence_unit == 'months':
        if cadence_interval == 1:
            if bucket_date.month == 1:
                prev_month = 12
                prev_year = bucket_date.year - 1
            else:
                prev_month = bucket_date.month - 1
                prev_year = bucket_date.year
            try:
                period_start = bucket_date.replace(year=prev_year, month=prev_month)
            except ValueError:
                last_day = calendar.monthrange(prev_year, prev_month)[1]
                period_start = date_type(prev_year, prev_month, last_day)
            period_start = period_start + timedelta(days=1)
        else:
            months_back = cadence_interval
            new_month = bucket_date.month - months_back
            new_year = bucket_date.year
            while new_month <= 0:
                new_month += 12
                new_year -= 1
            try:
                period_start = bucket_date.replace(year=new_year, month=new_month)
            except ValueError:
                last_day = calendar.monthrange(new_year, new_month)[1]
                period_start = date_type(new_year, new_month, last_day)
            period_start = period_start + timedelta(days=1)
    elif cadence_unit == 'years':
        new_year = bucket_date.year - cadence_interval
        try:
            period_start = bucket_date.replace(year=new_year)
        except ValueError:
            period_start = date_type(new_year, 2, 28)
        period_start = period_start + timedelta(days=1)
    
    if not period_start:
        log_warning(logger, 'RESTORE_BUCKET', f"Could not calculate period_start for cadence_unit={cadence_unit}")
        return False
    
    today = date_type.today()
    log_info(logger, 'RESTORE_BUCKET', f"Period: {period_start} to {bucket_date}, Today: {today}")
    
    # Determine wage_bill from cadence_info
    wage_bill = int(cadence_info.get('wage_bill', 0))
    log_info(logger, 'RESTORE_BUCKET', f"wage_bill={wage_bill}")
    
    # Allow bucket restoration regardless of whether today is in the period
    # The bucket should be restored to maintain accurate historical data
    log_info(logger, 'RESTORE_BUCKET', f"Proceeding with restoration")
    
    # Get original amount from cadence_info
    original_amount = Decimal(str(cadence_info.get('amount', 0)))
    
    # Calculate sum of all remaining manual entries in this category within this cadence period
    entries = _get_entries_from_redis(table, user_id)
    if entries is None:
        entries = []
    
    manual_entries_sum = Decimal('0')
    for entry in entries:
        if (int(entry.get('category_id', 0)) == int(category_id) and 
            not entry.get('is_bucket') and
            period_start <= date_type.fromisoformat(entry.get('date')) <= bucket_date):
            manual_entries_sum += Decimal(str(entry.get('amount', 0)))
    
    # Calculate what the bucket amount should be
    # For wage_bill: always use original_amount (binary paid/unpaid)
    # For variable/allowance: subtract remaining manual entries
    if wage_bill:
        new_bucket_amount = original_amount
        log_info(logger, 'RESTORE_BUCKET', f"Wage/Bill: using full original_amount={original_amount} for restoration")
    else:
        new_bucket_amount = original_amount - manual_entries_sum
    log_info(logger, 'RESTORE_BUCKET', f"Calculated new bucket amount: original={original_amount}, manual_sum={manual_entries_sum}, new_amount={new_bucket_amount}")
    
    # All bucket tables now use user_id consistently for Redis key
    
    # Check if bucket actually exists in Redis currently
    redis_key = f"{table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    bucket_exists = False
    
    if redis_data:
        entries_list = json.loads(redis_data)
        for entry in entries_list:
            if entry.get('id') == bucket['id'] and entry.get('is_bucket') == 1:
                bucket_exists = True
                log_info(logger, 'RESTORE_BUCKET', f"Bucket {bucket['id']} currently exists in Redis with amount={entry.get('amount')}")
                break
    
    if bucket_exists:
        # Bucket still exists, add back amount
        from recurring_bucket_manager import add_to_bucket_record_by_category_date, get_bucket_table_for_entry_table
        bucket_table = get_bucket_table_for_entry_table(table)
        
        if wage_bill:
            # Wage/Bill: restore to full original_amount
            # Find current entry amount in Redis to calculate how much to add
            current_bucket_amount = Decimal('0')
            if redis_data:
                for entry in json.loads(redis_data):
                    if entry.get('id') == bucket['id'] and entry.get('is_bucket') == 1:
                        current_bucket_amount = Decimal(str(entry.get('amount', 0)))
                        break
            restore_amount = original_amount - current_bucket_amount
            log_info(logger, 'RESTORE_BUCKET', f"Wage/Bill: restoring to original={original_amount} (current={current_bucket_amount}, adding {restore_amount})")
            if restore_amount > 0:
                add_to_bucket(table, bucket['id'], restore_amount, user_id)
            if bucket_table:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, float(restore_amount), user_id)
        else:
            # Variable/Allowance: just add back the deleted amount
            log_info(logger, 'RESTORE_BUCKET', f"Bucket exists, adding back {deleted_entry_amount}")
            add_to_bucket(table, bucket['id'], Decimal(str(deleted_entry_amount)), user_id)
            if bucket_table:
                add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, deleted_entry_amount, user_id)
                log_info(logger, 'RESTORE_BUCKET', f"Also restored bucket record for category {category_id}, date {bucket_date}")
        return True
    
    # Bucket was deleted, need to recreate it if new_amount > 0
    if new_bucket_amount > 0:
        log_info(logger, 'RESTORE_BUCKET', f"Recreating deleted bucket with amount={new_bucket_amount}")
        # Recreate the bucket entry
        if redis_data:
            entries_list = json.loads(redis_data)
            # Create new bucket entry
            new_bucket = {
                'id': bucket['id'],  # Reuse the same bucket ID
                'category_id': category_id,
                'date': bucket_date.isoformat(),
                'amount': float(new_bucket_amount),
                'is_bucket': 1,
                'original_amount': float(original_amount),
                'processed': 0
            }
            entries_list.append(new_bucket)
            redis_manager._redis_client.setex(redis_key, 604800, json.dumps(entries_list))
            redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", table)
            redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
            # Remove from pending deletes if it was there
            redis_manager._redis_client.srem(f"pending_deletes:{table}:{user_id}", bucket['id'])
            log_info(logger, 'RESTORE_BUCKET', f"Bucket entry recreated successfully in Redis")
            
            # CRITICAL: Also restore the bucket record to match
            from recurring_bucket_manager import add_to_bucket_record_by_category_date, get_bucket_table_for_entry_table
            bucket_table = get_bucket_table_for_entry_table(table)
            if bucket_table:
                if wage_bill:
                    # Wage/Bill: restore record to original_amount
                    add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, float(original_amount), user_id)
                    log_info(logger, 'RESTORE_BUCKET', f"Wage/Bill: restored bucket record with original_amount={original_amount}")
                else:
                    # Variable/Allowance: add back the deleted entry amount
                    add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, deleted_entry_amount, user_id)
                    log_info(logger, 'RESTORE_BUCKET', f"Also restored bucket record for category {category_id}, date {bucket_date}")
            
            return True
    else:
        log_info(logger, 'RESTORE_BUCKET', f"New amount would be {new_bucket_amount} (<=0), keeping bucket deleted")
        
        # CRITICAL: Even if bucket stays deleted, still restore the bucket record
        # The record tracks negative amounts (overspending)
        # If the record goes from negative to positive, an entry will be recreated
        from recurring_bucket_manager import add_to_bucket_record_by_category_date, get_bucket_table_for_entry_table
        bucket_table = get_bucket_table_for_entry_table(table)
        entry_was_recreated = False
        if bucket_table:
            # Determine restore amount for the record
            if wage_bill:
                record_restore_amount = float(original_amount)
            else:
                record_restore_amount = deleted_entry_amount
            # This function returns True if it recreated a bucket entry (negative→positive transition)
            result = add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, record_restore_amount, user_id)
            # Check if an entry was recreated by checking the return value or looking for specific log
            # For now, we need to check if the bucket record went positive
            # The function logs "Bucket went from negative/zero to positive" when it recreates
            log_info(logger, 'RESTORE_BUCKET', f"Restored bucket record (result={result}) for category {category_id}, date {bucket_date}")
            # If result is True, it means the bucket record was successfully updated
            # But we need to know if an ENTRY was recreated, so let's check if bucket now exists
            redis_key = f"{table}:v1:{user_id}"
            redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
            if redis_data:
                entries_list = json.loads(redis_data)
                for entry in entries_list:
                    # Look for a bucket entry for this category and date
                    if (entry.get('category_id') == int(category_id) and 
                        entry.get('date') == bucket_date.isoformat() and
                        entry.get('is_bucket') == 1):
                        entry_was_recreated = True
                        log_info(logger, 'RESTORE_BUCKET', f"Bucket entry WAS recreated: id={entry.get('id')}")
                        break
        
        return entry_was_recreated


def delete_bucket_record_for_entry(table, category_id, entry_date, user_id):
    """
    PHASE 4 (Feb 2026): Delete the bucket record when a bucket entry is deleted.
    
    When a user deletes an entry that was created as a bucket (is_bucket=1),
    we also need to delete the corresponding record in recurring_*_buckets.
    
    Args:
        table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        category_id: The category ID
        entry_date: Date of the bucket entry (string or date object)
        user_id: The user ID
        
    Returns:
        True if bucket record was deleted, False otherwise
    """
    from flask import current_app
    from datetime import date as date_type
    import json
    
    log_info(logger, 'DELETE_BUCKET_RECORD', f"Starting: table={table}, category={category_id}, date={entry_date}")
    
    # Get the bucket table name
    from recurring_bucket_manager import get_bucket_table_for_entry_table
    bucket_table = get_bucket_table_for_entry_table(table)
    
    if not bucket_table:
        log_warning(logger, 'DELETE_BUCKET_RECORD', f"No bucket table found for {table}")
        return False
    
    # Normalize entry_date to string
    if isinstance(entry_date, date_type):
        entry_date_str = entry_date.isoformat()
    else:
        entry_date_str = str(entry_date)
    
    # Get bucket records from Redis
    bucket_records_key = f"{bucket_table}:v1:{user_id}"
    bucket_records_data = redis_manager._redis_client.get(bucket_records_key) if redis_manager._redis_client else None
    
    if not bucket_records_data:
        log_info(logger, 'DELETE_BUCKET_RECORD', f"No bucket records found in Redis for {bucket_table}")
        return False
    
    bucket_records = json.loads(bucket_records_data)
    original_count = len(bucket_records)
    
    # Find and remove ALL bucket records that match category_id and bucket_date
    filtered_records = []
    deleted_record_ids = []
    
    for record in bucket_records:
        record_cat = int(record.get('category_id', 0))
        record_date = record.get('bucket_date', '')
        
        if record_cat == int(category_id) and record_date == entry_date_str:
            deleted_record_ids.append(record.get('id'))
            log_info(logger, 'DELETE_BUCKET_RECORD', f"Found matching record: id={record.get('id')}")
            # Skip this record (don't add to filtered list)
        else:
            filtered_records.append(record)
    
    if not deleted_record_ids:
        log_info(logger, 'DELETE_BUCKET_RECORD', f"No matching bucket record found for category {category_id}, date {entry_date_str}")
        return False
    
    # Save filtered records back to Redis
    CACHE_TTL = 604800  # 7 days
    redis_manager._redis_client.setex(
        bucket_records_key,
        CACHE_TTL,
        json.dumps(filtered_records, cls=redis_manager.DecimalEncoder)
    )
    
    # Mark as dirty for flush
    redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
    redis_manager._redis_client.expire(f"dirty_tables:{user_id}", CACHE_TTL)
    
    # Track pending deletions (only for positive IDs that exist in MySQL)
    pending_key = f"pending_deletes:{bucket_table}:{user_id}"
    for deleted_id in deleted_record_ids:
        if deleted_id is not None and int(deleted_id) > 0:
            redis_manager._redis_client.sadd(pending_key, deleted_id)
    redis_manager._redis_client.expire(pending_key, CACHE_TTL)
    
    log_info(logger, 'DELETE_BUCKET_RECORD', f"Deleted {len(deleted_record_ids)} bucket record(s): {deleted_record_ids}, remaining={len(filtered_records)}/{original_count}")
    
    return True