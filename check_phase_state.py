#!/usr/bin/env python3
"""
Temporary script to check Redis + MySQL state for all credit-account-related tables.
Run on server: python3 check_phase_state.py
"""
import redis
import json
import pymysql
import sys

UID = 273
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_DB = 0
MYSQL_USER = 'ms_admin'
MYSQL_PASS = 'dune6MEANTIME.ching_reek'
MYSQL_DB = 'budget'

TABLES = [
    'expense_categories',
    'expense_entries',
    'c_expense_categories',
    'c_expense_entries',
    'recurring_expense',
    'credit_accounts',
    'c_a_balances',
    'c_a_balances_d',
    'c_a_balances_m',
    'c_payment_entries',
    'recurring_expense_buckets',
    'quiltt_accounts',
]

SEPARATOR = '=' * 80

def get_redis_data(r, table):
    key = f"{table}:v1:{UID}"
    val = r.get(key)
    if val:
        return json.loads(val)
    return None

def print_header(title):
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)

def print_redis(r):
    print_header("REDIS STATE")
    for t in TABLES:
        data = get_redis_data(r, t)
        if data is None:
            print(f"\n  {t}: NOT IN CACHE")
        else:
            print(f"\n  {t}: {len(data)} records")
            for rec in data[:3]:
                # Compact display - key fields only
                if t == 'expense_categories':
                    print(f"    id={rec.get('id')}, name={rec.get('name')}, is_credit_account={rec.get('is_credit_account')}, is_recurring={rec.get('is_recurring')}, no_end_date={rec.get('no_end_date')}")
                elif t == 'expense_entries':
                    print(f"    id={rec.get('id')}, cat_id={rec.get('category_id')}, date={rec.get('date')}, amount={rec.get('amount')}, recurring_id={rec.get('recurring_id')}, is_bucket={rec.get('is_bucket')}")
                elif t == 'c_expense_categories':
                    print(f"    id={rec.get('id')}, account_id={rec.get('account_id')}, name={rec.get('name')}, is_recurring={rec.get('is_recurring')}")
                elif t == 'c_expense_entries':
                    print(f"    id={rec.get('id')}, cat_id={rec.get('category_id')}, date={rec.get('date')}, amount={rec.get('amount')}")
                elif t == 'recurring_expense':
                    print(f"    id={rec.get('id')}, cat_id={rec.get('category_id')}, cat_name={rec.get('category_name')}, amount={rec.get('amount')}, cadence={rec.get('cadence_interval')} {rec.get('cadence_unit')}, start={rec.get('start_date')}, end={rec.get('end_date')}")
                elif t == 'credit_accounts':
                    print(f"    id={rec.get('id')}, name={rec.get('name')}, quiltt_account_id={rec.get('quiltt_account_id')}, starting_balance={rec.get('starting_balance')}")
                elif t in ('c_a_balances', 'c_a_balances_d', 'c_a_balances_m'):
                    print(f"    id={rec.get('id')}, account_id={rec.get('account_id')}, date={rec.get('date')}, balance={rec.get('balance')}, total_expenses={rec.get('total_expenses')}, total_payments={rec.get('total_payments')}")
                elif t == 'c_payment_entries':
                    print(f"    id={rec.get('id')}, account_id={rec.get('account_id')}, date={rec.get('date')}, amount={rec.get('amount')}, recurring_id={rec.get('recurring_id')}")
                elif t == 'recurring_expense_buckets':
                    print(f"    id={rec.get('id')}, cat_id={rec.get('category_id')}, bucket_date={rec.get('bucket_date')}, amount={rec.get('amount')}, original={rec.get('original_amount')}")
                elif t == 'quiltt_accounts':
                    print(f"    id={rec.get('id')}, account_id={rec.get('account_id')}, name={rec.get('account_name')}, type={rec.get('account_type')}, balance={rec.get('current_balance')}")
                else:
                    print(f"    {rec}")
            if len(data) > 3:
                print(f"    ... and {len(data) - 3} more")
    
    # Dirty tables
    dirty = r.smembers(f"dirty_tables:{UID}")
    print(f"\n  DIRTY TABLES: {dirty if dirty else 'none'}")
    
    # Pending deletes
    pending = []
    for key in r.scan_iter(f"pending_deletes:*:{UID}"):
        members = r.smembers(key)
        pending.append(f"{key}: {members}")
    print(f"  PENDING DELETES: {pending if pending else 'none'}")

def print_mysql(conn):
    print_header("MYSQL STATE")
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    
    # expense_categories
    cursor.execute("SELECT id, name, is_credit_account, is_recurring, no_end_date FROM expense_categories WHERE user_id=%s ORDER BY id", (UID,))
    rows = cursor.fetchall()
    print(f"\n  expense_categories: {len(rows)} records")
    for r in rows:
        print(f"    id={r['id']}, name={r['name']}, is_credit_account={r['is_credit_account']}, is_recurring={r['is_recurring']}, no_end_date={r['no_end_date']}")
    
    # expense_entries (just counts by category)
    cursor.execute("""
        SELECT ec.name, ec.id as cat_id, ec.is_credit_account, COUNT(ee.id) as cnt, 
               MIN(ee.date) as min_date, MAX(ee.date) as max_date,
               SUM(CASE WHEN ee.is_bucket=1 THEN 1 ELSE 0 END) as bucket_cnt
        FROM expense_categories ec
        LEFT JOIN expense_entries ee ON ee.category_id = ec.id
        WHERE ec.user_id=%s
        GROUP BY ec.id, ec.name, ec.is_credit_account
        HAVING cnt > 0
        ORDER BY ec.id
    """, (UID,))
    rows = cursor.fetchall()
    print(f"\n  expense_entries: (by category)")
    for r in rows:
        print(f"    cat_id={r['cat_id']} ({r['name']}, credit={r['is_credit_account']}): {r['cnt']} entries ({r['bucket_cnt']} buckets), dates {r['min_date']} to {r['max_date']}")
    
    # credit_accounts
    cursor.execute("SELECT id, name, quiltt_account_id, starting_balance, is_quiltt FROM credit_accounts WHERE user_id=%s", (UID,))
    rows = cursor.fetchall()
    print(f"\n  credit_accounts: {len(rows)} records")
    for r in rows:
        print(f"    id={r['id']}, name={r['name']}, quiltt_id={r['quiltt_account_id']}, starting_bal={r['starting_balance']}, is_quiltt={r['is_quiltt']}")
    
    # c_expense_categories
    cursor.execute("""
        SELECT cec.id, cec.account_id, cec.name, cec.is_recurring, cec.is_interest, cec.is_auto_adjustment
        FROM c_expense_categories cec 
        WHERE cec.account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
        ORDER BY cec.account_id, cec.id
    """, (UID,))
    rows = cursor.fetchall()
    print(f"\n  c_expense_categories: {len(rows)} records")
    for r in rows:
        print(f"    id={r['id']}, account_id={r['account_id']}, name={r['name']}, is_recurring={r['is_recurring']}, is_interest={r['is_interest']}, is_auto_adj={r['is_auto_adjustment']}")
    
    # c_expense_entries count
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM c_expense_entries 
        WHERE category_id IN (SELECT id FROM c_expense_categories WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s))
    """, (UID,))
    row = cursor.fetchone()
    print(f"\n  c_expense_entries: {row['cnt']} records")
    
    # recurring_expense
    cursor.execute("""
        SELECT re.id, re.category_id, ec.name as cat_name, ec.is_credit_account, re.amount, 
               re.cadence_interval, re.cadence_unit, re.start_date, re.end_date, re.monthly_days
        FROM recurring_expense re
        JOIN expense_categories ec ON re.category_id = ec.id
        WHERE re.user_id=%s ORDER BY re.id
    """, (UID,))
    rows = cursor.fetchall()
    print(f"\n  recurring_expense: {len(rows)} records")
    for r in rows:
        print(f"    id={r['id']}, cat_id={r['category_id']} ({r['cat_name']}, credit={r['is_credit_account']}), amount={r['amount']}, cadence={r['cadence_interval']} {r['cadence_unit']}, monthly_days={r['monthly_days']}, dates {r['start_date']} to {r['end_date']}")
    
    # c_a_balances
    for bal_table in ['c_a_balances', 'c_a_balances_d', 'c_a_balances_m']:
        cursor.execute(f"""
            SELECT COUNT(*) as cnt FROM {bal_table} 
            WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
        """, (UID,))
        cnt = cursor.fetchone()['cnt']
        if cnt > 0:
            cursor.execute(f"""
                SELECT account_id, MIN(date) as min_date, MAX(date) as max_date, 
                       COUNT(*) as cnt
                FROM {bal_table} 
                WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
                GROUP BY account_id
            """, (UID,))
            rows = cursor.fetchall()
            print(f"\n  {bal_table}: {cnt} records")
            for r in rows:
                print(f"    account_id={r['account_id']}: {r['cnt']} rows, dates {r['min_date']} to {r['max_date']}")
            # Show a few sample rows with balance values
            cursor.execute(f"""
                SELECT account_id, date, balance, total_expenses, total_payments 
                FROM {bal_table} 
                WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
                ORDER BY date DESC LIMIT 3
            """, (UID,))
            samples = cursor.fetchall()
            for s in samples:
                print(f"      sample: acct={s['account_id']}, date={s['date']}, balance={s['balance']}, expenses={s['total_expenses']}, payments={s['total_payments']}")
        else:
            print(f"\n  {bal_table}: 0 records")
    
    # c_payment_entries
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM c_payment_entries 
        WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
    """, (UID,))
    cnt = cursor.fetchone()['cnt']
    if cnt > 0:
        cursor.execute("""
            SELECT id, account_id, date, amount, recurring_id
            FROM c_payment_entries 
            WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id=%s)
            ORDER BY date LIMIT 5
        """, (UID,))
        rows = cursor.fetchall()
        print(f"\n  c_payment_entries: {cnt} records")
        for r in rows:
            print(f"    id={r['id']}, account_id={r['account_id']}, date={r['date']}, amount={r['amount']}, recurring_id={r['recurring_id']}")
        if cnt > 5:
            print(f"    ... and {cnt - 5} more")
    else:
        print(f"\n  c_payment_entries: 0 records")
    
    # recurring_expense_buckets (only for payment categories)
    cursor.execute("""
        SELECT reb.id, reb.category_id, ec.name, ec.is_credit_account, reb.bucket_date, reb.amount, reb.original_amount
        FROM recurring_expense_buckets reb
        JOIN expense_categories ec ON reb.category_id = ec.id
        WHERE reb.user_id=%s AND ec.is_credit_account = 1
        ORDER BY reb.bucket_date LIMIT 5
    """, (UID,))
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) as cnt FROM recurring_expense_buckets WHERE user_id=%s", (UID,))
    total = cursor.fetchone()['cnt']
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM recurring_expense_buckets reb
        JOIN expense_categories ec ON reb.category_id = ec.id
        WHERE reb.user_id=%s AND ec.is_credit_account = 1
    """, (UID,))
    payment_bucket_cnt = cursor.fetchone()['cnt']
    print(f"\n  recurring_expense_buckets: {total} total ({payment_bucket_cnt} for payment categories)")
    for r in rows:
        print(f"    id={r['id']}, cat_id={r['category_id']} ({r['name']}), date={r['bucket_date']}, amount={r['amount']}, original={r['original_amount']}")
    if payment_bucket_cnt > 5:
        print(f"    ... and {payment_bucket_cnt - 5} more")
    
    # quiltt_accounts (CREDIT type only)
    cursor.execute("""
        SELECT account_id, account_name, account_type, current_balance 
        FROM quiltt_accounts WHERE user_id=%s AND account_type='CREDIT'
    """, (UID,))
    rows = cursor.fetchall()
    print(f"\n  quiltt_accounts (CREDIT): {len(rows)} records")
    for r in rows:
        print(f"    account_id={r['account_id']}, name={r['account_name']}, balance={r['current_balance']}")
    
    cursor.close()

def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "?"
    print(f"\n{'#' * 80}")
    print(f"  PHASE {phase} STATE CHECK - User {UID}")
    print(f"{'#' * 80}")
    
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
    print_redis(r)
    
    conn = pymysql.connect(host='localhost', user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)
    print_mysql(conn)
    conn.close()
    
    print(f"\n{SEPARATOR}")
    print(f"  END OF PHASE {phase} CHECK")
    print(f"{SEPARATOR}\n")

if __name__ == '__main__':
    main()
