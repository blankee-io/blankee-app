"""
Recurring Buckets Management

This module handles the new recurring_buckets system where bucket state is tracked
separately from bucket entries. Bucket records persist even when depleted to 0,
and entry records are created/deleted based on bucket amount.
"""

from datetime import date
from decimal import Decimal
import json
import pymysql
from db_connections import get_db_pool
import redis_manager
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)


def get_bucket_table_for_entry_table(entry_table):
    """Get the corresponding bucket table for an entry table."""
    mapping = {
        'income_entries': 'recurring_income_buckets',
        'expense_entries': 'recurring_expense_buckets',
        'c_expense_entries': 'recurring_c_expense_buckets'
    }
    return mapping.get(entry_table)


def create_bucket_record(entry_table, user_id, category_id, bucket_date, original_amount, account_id=None):
    """
    Create a bucket record when a recurring category generates a bucket entry.
    
    Args:
        entry_table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID
        category_id: Category ID
        bucket_date: Date for the bucket (end of period)
        original_amount: Original bucket amount
        account_id: Account ID (required for c_expense_entries)
    
    Returns:
        The bucket ID, or None on error
    """
    from flask import current_app
    
    bucket_table = get_bucket_table_for_entry_table(entry_table)
    if not bucket_table:
        log_error(logger, 'CREATE_BUCKET', f"Invalid entry table: {entry_table}")
        return None
    
    if isinstance(bucket_date, str):
        bucket_date = date.fromisoformat(bucket_date)
    
    try:
        # Check Redis availability
        if not redis_manager._redis_client:
            log_error(logger, 'CREATE_BUCKET', f"Redis client not available")
            return None
        
        # Get or create buckets list in Redis
        # All bucket tables now use user_id consistently for Redis key
        redis_key = f"{bucket_table}:v1:{user_id}"
        log_info(logger, 'CREATE_BUCKET', f"Redis key: {redis_key}, table={entry_table}, user_id={user_id}, category_id={category_id}, date={bucket_date}")
        
        redis_data = redis_manager._redis_client.get(redis_key)
        
        buckets = []
        if redis_data:
            buckets = json.loads(redis_data)
            log_info(logger, 'CREATE_BUCKET', f"Found {len(buckets)} existing buckets in Redis")
        else:
            log_info(logger, 'CREATE_BUCKET', f"No existing buckets found, creating new list")
        
        # Check if bucket already exists
        bucket_id = None
        for bucket in buckets:
            if (bucket.get('category_id') == category_id and 
                bucket.get('bucket_date') == bucket_date.isoformat()):
                bucket_id = bucket.get('id')
                log_info(logger, 'CREATE_BUCKET', f"Bucket already exists in Redis: id={bucket_id}")
                return bucket_id
        
        # Generate a temporary negative ID for new bucket (will be replaced on flush)
        if buckets:
            # Find the lowest ID (most negative)
            existing_ids = [b.get('id', 0) for b in buckets]
            min_id = min(existing_ids)
            bucket_id = min_id - 1 if min_id < 0 else -1
        else:
            bucket_id = -1
        
        log_info(logger, 'CREATE_BUCKET', f"Generated temp bucket_id={bucket_id}")
        
        # Create new bucket record in Redis
        new_bucket = {
            'id': bucket_id,
            'user_id': user_id,
            'category_id': category_id,
            'bucket_date': bucket_date.isoformat(),
            'amount': float(original_amount),
            'original_amount': float(original_amount)
        }
        
        if entry_table == 'c_expense_entries':
            new_bucket['account_id'] = account_id
        
        buckets.append(new_bucket)
        log_info(logger, 'CREATE_BUCKET', f"Added bucket to list, now have {len(buckets)} buckets")
        
        # Save to Redis
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(buckets))
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
        
        log_info(logger, 'CREATE_BUCKET', f"✓ Created bucket record in Redis: id={bucket_id}, table={bucket_table}, category={category_id}, date={bucket_date}, amount={original_amount}")
        return bucket_id
        
    except Exception as e:
        import traceback
        log_error(logger, 'CREATE_BUCKET', f"Error creating bucket: {e}")
        log_error(logger, 'CREATE_BUCKET', f"Traceback: {traceback.format_exc()}")
        return None


def get_bucket_record_by_category_date(bucket_table, category_id, bucket_date, user_id):
    """
    Get a bucket record by category_id and bucket_date.
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        category_id: The category ID
        bucket_date: The bucket date (date object or string)
        user_id: The user ID (now always user_id for all bucket tables)
    
    Returns:
        Bucket record dict or None if not found
    """
    from flask import current_app
    
    if isinstance(bucket_date, str):
        bucket_date = date.fromisoformat(bucket_date)
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        return None
    
    try:
        bucket_list = json.loads(redis_data)
        
        # Ensure category_id is an integer for comparison
        search_category_id = int(category_id) if category_id else None
        search_date = bucket_date.isoformat()
        
        # Find the bucket record by category + date
        for record in bucket_list:
            if (record.get('category_id') == search_category_id and 
                record.get('bucket_date') == search_date):
                return record
        
        return None
        
    except Exception as e:
        log_error(logger, 'GET_BUCKET_RECORD', f"Error: {e}")
        return None


def get_bucket_records_for_category(bucket_table, category_id, user_id):
    """
    Get ALL bucket records for a category.
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        category_id: The category ID
        user_id: The user ID
    
    Returns:
        List of bucket record dicts for this category
    """
    from flask import current_app
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        return []
    
    try:
        bucket_list = json.loads(redis_data)
        
        # Ensure category_id is an integer for comparison
        search_category_id = int(category_id) if category_id else None
        
        # Find all bucket records for this category
        return [record for record in bucket_list if record.get('category_id') == search_category_id]
        
    except Exception as e:
        log_error(logger, 'GET_BUCKET_RECORDS_FOR_CATEGORY', f"Error: {e}")
        return []


def subtract_from_bucket_record(bucket_table, bucket_id, subtract_amount, user_id):
    """
    Subtract amount from a bucket record (same pattern as subtract_from_bucket).
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        bucket_id: The ID of the bucket record
        subtract_amount: Amount to subtract
        user_id: The user ID (now always user_id for all bucket tables)
    
    Returns:
        True if bucket was depleted to 0, False otherwise
    """
    from flask import current_app
    
    subtract_amount = Decimal(str(subtract_amount))
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        log_warning(logger, 'SUBTRACT_BUCKET_RECORD', f"No Redis data for {redis_key}")
        return False
    
    try:
        bucket_list = json.loads(redis_data)
        
        # Find the bucket record
        bucket_record = None
        bucket_index = None
        for i, record in enumerate(bucket_list):
            if record.get('id') == bucket_id:
                bucket_record = record
                bucket_index = i
                break
        
        if not bucket_record:
            log_warning(logger, 'SUBTRACT_BUCKET_RECORD', f"Bucket ID {bucket_id} not found in Redis")
            return False
        
        # Calculate new amount (allow negative for overspending)
        current_amount = Decimal(str(bucket_record.get('amount', 0)))
        new_amount = current_amount - subtract_amount
        
        log_info(logger, 'SUBTRACT_BUCKET_RECORD', f"Bucket {bucket_id}: current={current_amount}, subtract={subtract_amount}, new={new_amount}")
        
        # Update bucket record in Redis (ALLOW NEGATIVE AMOUNTS for overspending tracking)
        bucket_list[bucket_index]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(bucket_list))
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        
        log_info(logger, 'SUBTRACT_BUCKET_RECORD', f"Updated in Redis with new amount: {new_amount} (negative allowed)")
        return new_amount <= 0  # True if depleted
        
    except Exception as e:
        log_error(logger, 'SUBTRACT_BUCKET_RECORD', f"Error: {e}")
        return False


def subtract_from_bucket_record_by_category_date(bucket_table, category_id, bucket_date, subtract_amount, user_id):
    """
    Subtract amount from a bucket record by finding it via category_id + bucket_date.
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        category_id: The category ID
        bucket_date: The bucket date (date object or string)
        subtract_amount: Amount to subtract
        user_id: The user ID (now always user_id for all bucket tables)
    
    Returns:
        True if bucket was depleted to 0, False otherwise
    """
    from flask import current_app
    
    if isinstance(bucket_date, str):
        bucket_date = date.fromisoformat(bucket_date)
    
    subtract_amount = Decimal(str(subtract_amount))
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        log_warning(logger, 'SUBTRACT_BUCKET_RECORD', f"No Redis data for {redis_key}")
        return False
    
    try:
        bucket_list = json.loads(redis_data)
        
        # Find the bucket record by category + date\n        bucket_record = None
        bucket_index = None
        for i, record in enumerate(bucket_list):
            if (record.get('category_id') == int(category_id) and 
                record.get('bucket_date') == bucket_date.isoformat()):
                bucket_record = record
                bucket_index = i
                break
        
        if not bucket_record:
            log_warning(logger, 'SUBTRACT_BUCKET_RECORD', f"No bucket record found for category={category_id}, date={bucket_date}")
            return False
        
        # Calculate new amount (allow negative for overspending)
        current_amount = Decimal(str(bucket_record.get('amount', 0)))
        new_amount = current_amount - subtract_amount
        
        log_info(logger, 'SUBTRACT_BUCKET_RECORD', f"Bucket record {bucket_record['id']}: current={current_amount}, subtract={subtract_amount}, new={new_amount}")
        
        # Update bucket record in Redis (ALLOW NEGATIVE AMOUNTS for overspending tracking)
        bucket_list[bucket_index]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(bucket_list))
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        
        log_info(logger, 'SUBTRACT_BUCKET_RECORD', f"Updated in Redis with new amount: {new_amount} (negative allowed)")
        return new_amount <= 0  # True if depleted
        
    except Exception as e:
        log_error(logger, 'SUBTRACT_BUCKET_RECORD', f"Error: {e}")
        return False


def add_to_bucket_record(bucket_table, bucket_id, add_amount, user_id):
    """
    Add amount back to a bucket record (same pattern as add_to_bucket).
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        bucket_id: The ID of the bucket record
        add_amount: Amount to add back
        user_id: The user ID (now always user_id for all bucket tables)
    
    Returns:
        True if bucket was successfully updated
    """
    from flask import current_app
    
    add_amount = Decimal(str(add_amount))
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        log_warning(logger, 'ADD_BUCKET_RECORD', f"No Redis data for {redis_key}")
        return False
    
    try:
        bucket_list = json.loads(redis_data)
        
        # Find the bucket record
        bucket_record = None
        bucket_index = None
        for i, record in enumerate(bucket_list):
            if record.get('id') == bucket_id:
                bucket_record = record
                bucket_index = i
                break
        
        if not bucket_record:
            log_warning(logger, 'ADD_BUCKET_RECORD', f"Bucket ID {bucket_id} not found in Redis")
            return False
        
        # Calculate new amount
        current_amount = Decimal(str(bucket_record.get('amount', 0)))
        original_amount = Decimal(str(bucket_record.get('original_amount', 0)))
        was_negative_or_zero = current_amount <= 0
        new_amount = current_amount + add_amount
        
        # Don't exceed original amount
        if new_amount > original_amount:
            new_amount = original_amount
        
        now_positive = new_amount > 0
        
        log_info(logger, 'ADD_BUCKET_RECORD', f"Bucket {bucket_id}: current={current_amount}, add={add_amount}, new={new_amount}, original={original_amount}, was_negative={was_negative_or_zero}, now_positive={now_positive}")
        
        # Update bucket record in Redis
        bucket_list[bucket_index]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(bucket_list))
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        
        # CRITICAL: If bucket was negative/zero and now positive, recreate the bucket entry
        if was_negative_or_zero and now_positive:
            log_info(logger, 'ADD_BUCKET_RECORD', f"Bucket went from negative/zero to positive - need to recreate bucket entry")
            # Get the entry table for this bucket table
            entry_table = None
            if bucket_table == 'recurring_income_buckets':
                entry_table = 'income_entries'
            elif bucket_table == 'recurring_expense_buckets':
                entry_table = 'expense_entries'
            elif bucket_table == 'recurring_c_expense_buckets':
                entry_table = 'c_expense_entries'
            
            if entry_table:
                # Recreate bucket entry with the current positive amount
                from app import _update_entry_in_redis
                category_id = bucket_record.get('category_id')
                bucket_date = bucket_record.get('bucket_date')
                
                # Find the bucket entry ID (might be the same as bucket_id if structure allows)
                # For now, use bucket_id as entry_id since they're created together
                _update_entry_in_redis(
                    entry_table,
                    user_id,
                    category_id,
                    bucket_date,
                    float(new_amount),
                    recurring_id=None,  # Bucket entries don't have recurring_id typically
                    is_bucket=True,
                    original_amount=float(original_amount),
                    entry_id=bucket_id  # Use same ID as bucket record
                )
                log_info(logger, 'ADD_BUCKET_RECORD', f"Recreated bucket entry {bucket_id} in {entry_table} with amount {new_amount}")
        
        log_info(logger, 'ADD_BUCKET_RECORD', f"Updated in Redis with new amount: {new_amount}")
        return True
        
    except Exception as e:
        log_error(logger, 'ADD_BUCKET_RECORD', f"Error: {e}")
        return False


def add_to_bucket_record_by_category_date(bucket_table, category_id, bucket_date, add_amount, user_id):
    """
    Add amount back to a bucket record by finding it via category_id + bucket_date.
    
    Args:
        bucket_table: 'recurring_income_buckets', 'recurring_expense_buckets', or 'recurring_c_expense_buckets'
        category_id: The category ID
        bucket_date: The bucket date (date object or string)
        add_amount: Amount to add back
        user_id: The user ID (now always user_id for all bucket tables)
    
    Returns:
        True if bucket was successfully updated
    """
    from flask import current_app
    
    if isinstance(bucket_date, str):
        bucket_date = date.fromisoformat(bucket_date)
    
    add_amount = Decimal(str(add_amount))
    
    # Get bucket records from Redis
    redis_key = f"{bucket_table}:v1:{user_id}"
    redis_data = redis_manager._redis_client.get(redis_key) if redis_manager._redis_client else None
    
    if not redis_data:
        log_warning(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"No Redis data for {redis_key}")
        return False
    
    try:
        bucket_list = json.loads(redis_data)
        
        # DEBUG: Log what we're searching for
        log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE_DEBUG', f"Searching for category={category_id}, date={bucket_date.isoformat()} in {len(bucket_list)} records")
        log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE_DEBUG', f"All bucket records: {json.dumps(bucket_list, indent=2)}")
        
        # Find the bucket record by category + date
        # Ensure category_id is an integer for comparison
        search_category_id = int(category_id) if category_id else None
        search_date = bucket_date.isoformat()
        
        bucket_record = None
        bucket_index = None
        for i, record in enumerate(bucket_list):
            record_cat_id = record.get('category_id')
            record_date = record.get('bucket_date')
            # Compare as integers and strings
            if (record_cat_id == search_category_id and record_date == search_date):
                bucket_record = record
                bucket_index = i
                log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"FOUND bucket record at index {i}: category={record_cat_id}, date={record_date}")
                break
        
        if not bucket_record:
            log_warning(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"No bucket record found for category={category_id}, date={bucket_date}")
            return False
        
        # Calculate new amount
        current_amount = Decimal(str(bucket_record.get('amount', 0)))
        original_amount = Decimal(str(bucket_record.get('original_amount', 0)))
        was_negative_or_zero = current_amount <= 0
        new_amount = current_amount + add_amount
        
        # Don't exceed original amount
        if new_amount > original_amount:
            new_amount = original_amount
        
        now_positive = new_amount > 0
        
        log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Bucket record {bucket_record['id']}: current={current_amount}, add={add_amount}, new={new_amount}, original={original_amount}, was_negative={was_negative_or_zero}, now_positive={now_positive}")
        
        # Update bucket record in Redis
        bucket_list[bucket_index]['amount'] = float(new_amount)
        redis_manager._redis_client.setex(redis_key, 604800, json.dumps(bucket_list))
        redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
        redis_manager._redis_client.expire(f"dirty_tables:{user_id}", 604800)
        
        # CRITICAL: If bucket was negative/zero and now positive, recreate the bucket entry
        if was_negative_or_zero and now_positive:
            log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Bucket went from negative/zero to positive - need to recreate bucket entry")
            # Get the entry table for this bucket table
            entry_table = None
            recurring_table = None
            if bucket_table == 'recurring_income_buckets':
                entry_table = 'income_entries'
                recurring_table = 'recurring_income'
            elif bucket_table == 'recurring_expense_buckets':
                entry_table = 'expense_entries'
                recurring_table = 'recurring_expense'
            elif bucket_table == 'recurring_c_expense_buckets':
                entry_table = 'c_expense_entries'
                recurring_table = 'recurring_c_expense'
            
            if entry_table:
                # Recreate bucket entry with the current positive amount
                from app import _update_entry_in_redis
                import time
                
                # Look up the recurring_id from the recurring table in Redis
                recurring_id = None
                if recurring_table:
                    recurring_redis_key = f"{recurring_table}:v1:{user_id}"
                    recurring_data = redis_manager._redis_client.get(recurring_redis_key)
                    if recurring_data:
                        recurring_list = json.loads(recurring_data)
                        for rec in recurring_list:
                            if int(rec.get('category_id', 0)) == int(category_id):
                                recurring_id = rec.get('id')
                                log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Found recurring_id={recurring_id} for category {category_id}")
                                break
                
                # Generate a new temporary ID for the recreated bucket entry
                # Use negative timestamp to avoid conflicts with real IDs
                bucket_entry_id = -int(time.time() * 1000000)
                
                _update_entry_in_redis(
                    entry_table,
                    user_id,
                    category_id,
                    bucket_date.isoformat(),
                    float(new_amount),
                    recurring_id=recurring_id,
                    is_bucket=True,
                    original_amount=float(original_amount),
                    entry_id=bucket_entry_id
                )
                log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Recreated bucket entry {bucket_entry_id} in {entry_table} with amount {new_amount}, recurring_id={recurring_id}")
        
        log_info(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Updated in Redis with new amount: {new_amount}")
        return True
        
    except Exception as e:
        log_error(logger, 'ADD_BUCKET_RECORD_BY_DATE', f"Error: {e}")
        return False


def get_bucket_for_entry(entry_table, user_id, category_id, entry_date, cadence_info, account_id=None):
    """
    Find the bucket record that corresponds to a given entry date.
    
    Args:
        entry_table: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID (now always user_id for all bucket tables)
        category_id: Category ID
        entry_date: Date of the entry
        cadence_info: Cadence configuration dict
        account_id: Account ID (for c_expense_entries) - kept for backward compatibility but not used for Redis key
    
    Returns:
        Bucket dict or None if not found
    """
    from flask import current_app
    from datetime import timedelta
    import calendar as cal_module
    
    bucket_table = get_bucket_table_for_entry_table(entry_table)
    if not bucket_table:
        return None
    
    if isinstance(entry_date, str):
        entry_date = date.fromisoformat(entry_date)
    
    # All bucket tables now use user_id consistently
    try:
        # Get all bucket records for this category
        bucket_redis_key = f"{bucket_table}:v1:{user_id}"
        redis_data = redis_manager._redis_client.get(bucket_redis_key) if redis_manager._redis_client else None
        
        if not redis_data:
            return None
        
        buckets = json.loads(redis_data)
        category_buckets = [b for b in buckets if b.get('category_id') == category_id]
        
        if not category_buckets:
            return None
        
        # Find bucket whose period contains entry_date
        cadence_unit = cadence_info.get('cadence_unit')
        cadence_interval = int(cadence_info.get('cadence_interval', 1))
        
        for bucket in category_buckets:
            bucket_date = date.fromisoformat(bucket.get('bucket_date'))
            
            # Calculate period start for this bucket
            period_start = None
            if cadence_unit == 'days':
                period_start = bucket_date - timedelta(days=cadence_interval - 1)
            elif cadence_unit == 'weeks':
                period_start = bucket_date - timedelta(days=7 * cadence_interval - 1)
            elif cadence_unit == 'months':
                if bucket_date.month == 1:
                    prev_month = 12
                    prev_year = bucket_date.year - 1
                else:
                    prev_month = bucket_date.month - 1
                    prev_year = bucket_date.year
                try:
                    period_start = bucket_date.replace(year=prev_year, month=prev_month)
                except ValueError:
                    last_day = cal_module.monthrange(prev_year, prev_month)[1]
                    period_start = date(prev_year, prev_month, last_day)
                period_start = period_start + timedelta(days=1)
            elif cadence_unit == 'years':
                new_year = bucket_date.year - cadence_interval
                try:
                    period_start = bucket_date.replace(year=new_year)
                except ValueError:
                    period_start = date(new_year, 2, 28)
                period_start = period_start + timedelta(days=1)
            
            if period_start and period_start <= entry_date <= bucket_date:
                return bucket
        
        return None
        
    except Exception as e:
        log_error(logger, 'GET_BUCKET', f"Error: {e}")
        return None
