#!/usr/bin/env python3
"""
Create test pending transactions on dev for debugging.
Run on dev server: python3 /var/www/html/budget/create_test_pending.py
"""
import json
import time
import redis
import pymysql

USER_ID = 273
UNCATEGORIZED_CAT_ID = 1198  # Uncategorized expense category
CHECKING_ACCOUNT_ID = 'acct_132TvucgvjZwTyz6yOu2kT'  # Checking account

REDIS_CLIENT = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
CACHE_TTL = 604800  # 7 days

def get_db():
    return pymysql.connect(
        host='localhost', user='ms_admin', 
        password='dune6MEANTIME.ching_reek', 
        database='budget',
        cursorclass=pymysql.cursors.DictCursor
    )

def main():
    # Step 1: Insert expense entries with pending=1 into MySQL directly
    # (They'll get real auto-increment IDs this way)
    conn = get_db()
    cursor = conn.cursor()
    
    entries_to_create = [
        {'amount': '5.99', 'date': '2026-02-18', 'desc': 'SIGNALRGB TEST', 'merchant': 'SignalRGB'},
        {'amount': '19.99', 'date': '2026-02-18', 'desc': 'WL STEAM PURCHASE TEST', 'merchant': 'Steam'},
    ]
    
    entry_ids = []
    for entry in entries_to_create:
        cursor.execute("""
            INSERT INTO expense_entries (category_id, date, amount, pending, auto_confirmed, processed, is_bucket)
            VALUES (%s, %s, %s, 1, 0, 0, 0)
        """, (UNCATEGORIZED_CAT_ID, entry['date'], entry['amount']))
        entry_ids.append(cursor.lastrowid)
        print(f"Created expense_entry id={cursor.lastrowid}, amount={entry['amount']}, pending=1")
    
    conn.commit()
    
    # Step 2: Create quiltt_transactions linked to those entries
    txn_ids = [f'txn_TEST_{int(time.time())}_{i}' for i in range(len(entries_to_create))]
    
    for i, (entry, txn_id, entry_id) in enumerate(zip(entries_to_create, txn_ids, entry_ids)):
        cursor.execute("""
            INSERT INTO quiltt_transactions 
            (user_id, account_id, transaction_id, date, description, amount, merchant_name, 
             pending, transaction_type, imported_to_entry_id, imported_entry_type, imported_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'expense', %s, 'expense', NOW())
        """, (USER_ID, CHECKING_ACCOUNT_ID, txn_id, entry['date'], entry['desc'], 
              entry['amount'], entry['merchant'], entry_id))
        print(f"Created quiltt_transaction {txn_id} -> entry_id={entry_id}")
    
    conn.commit()
    cursor.close()
    conn.close()
    
    # Step 3: Update Redis with the new data
    # Hydrate expense_entries
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, category_id, date, amount, recurring_id, is_bucket, original_amount, 
               processed, pending, auto_confirmed, bud_item_id
        FROM expense_entries 
        WHERE category_id IN (SELECT id FROM expense_categories WHERE user_id = %s)
    """, (USER_ID,))
    all_entries = cursor.fetchall()
    
    # Convert for JSON
    for e in all_entries:
        e['date'] = str(e['date'])
        e['amount'] = str(e['amount'])
        if e.get('original_amount'):
            e['original_amount'] = str(e['original_amount'])
    
    redis_key = f"expense_entries:v1:{USER_ID}"
    REDIS_CLIENT.setex(redis_key, CACHE_TTL, json.dumps(all_entries))
    print(f"Updated Redis key {redis_key} with {len(all_entries)} entries")
    
    # Count pending
    pending_count = sum(1 for e in all_entries if e.get('pending') == 1)
    print(f"  -> {pending_count} entries have pending=1")
    
    # Hydrate quiltt_transactions
    cursor.execute("""
        SELECT * FROM quiltt_transactions WHERE user_id = %s
    """, (USER_ID,))
    all_txns = cursor.fetchall()
    
    for t in all_txns:
        for k, v in t.items():
            if hasattr(v, 'isoformat'):
                t[k] = str(v)
            elif isinstance(v, (int, float)):
                t[k] = t[k]
            elif v is None:
                t[k] = None
            else:
                t[k] = str(v)
        if t.get('amount'):
            t['amount'] = str(t['amount'])
    
    txn_key = f"quiltt_transactions:v1:{USER_ID}"
    REDIS_CLIENT.setex(txn_key, CACHE_TTL, json.dumps(all_txns))
    print(f"Updated Redis key {txn_key} with {len(all_txns)} transactions")
    
    # Mark dirty
    dirty_key = f"dirty_tables:{USER_ID}"
    REDIS_CLIENT.sadd(dirty_key, 'expense_entries', 'quiltt_transactions')
    REDIS_CLIENT.expire(dirty_key, CACHE_TTL)
    
    # Add user to hydrated set
    REDIS_CLIENT.sadd('hydrated_users', str(USER_ID))
    
    cursor.close()
    conn.close()
    
    print(f"\n=== DONE ===")
    print(f"Created {len(entry_ids)} pending expense entries: {entry_ids}")
    print(f"Created {len(txn_ids)} quiltt_transactions: {txn_ids}")
    print(f"Visit /pending-transactions to see them")

if __name__ == '__main__':
    main()
