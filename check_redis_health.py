#!/usr/bin/env python3
"""
Redis Hydration System Health Check Script

This script checks the health and status of the Redis hydration system.
Run it to verify everything is working correctly.

Usage:
    python check_redis_health.py
    
    # With verbose output
    python check_redis_health.py -v
    
    # Continuous monitoring
    python check_redis_health.py --watch
"""

import sys
import time
import argparse
from datetime import datetime
import os

# Add parent directory to path to import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def check_redis_connection():
    """Check if Redis is accessible"""
    print("🔍 Checking Redis connection...")
    try:
        import redis
        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=1,
        )
        client.ping()
        info = client.info('server')
        print(f"  ✅ Redis connected: {info.get('redis_version')}")
        return True, client
    except Exception as e:
        print(f"  ❌ Redis connection failed: {e}")
        return False, None


def check_redis_memory(client):
    """Check Redis memory usage"""
    print("\n💾 Checking Redis memory...")
    try:
        info = client.info('memory')
        used_memory_human = info.get('used_memory_human')
        used_memory_peak_human = info.get('used_memory_peak_human')
        maxmemory = info.get('maxmemory')
        maxmemory_policy = info.get('maxmemory_policy')
        
        print(f"  Memory used: {used_memory_human}")
        print(f"  Peak memory: {used_memory_peak_human}")
        
        if maxmemory > 0:
            maxmemory_human = f"{maxmemory / 1024 / 1024:.1f}MB"
            print(f"  Max memory: {maxmemory_human}")
            print(f"  Eviction policy: {maxmemory_policy}")
        else:
            print(f"  ⚠️  No max memory limit set")
        
        return True
    except Exception as e:
        print(f"  ❌ Memory check failed: {e}")
        return False


def check_cached_users(client, verbose=False):
    """Check which users are currently cached"""
    print("\n👥 Checking cached users...")
    try:
        # Get all user keys
        user_keys = client.keys("users:v1:*")
        if not user_keys:
            print("  ℹ️  No users currently cached")
            return True
        
        user_ids = []
        for key in user_keys:
            try:
                user_id = key.split(":")[-1]
                user_ids.append(user_id)
                
                ttl = client.ttl(key)
                if verbose:
                    if ttl > 0:
                        print(f"  User {user_id}: TTL {ttl}s ({ttl//60}m {ttl%60}s)")
                    else:
                        print(f"  User {user_id}: No expiration")
            except Exception as e:
                if verbose:
                    print(f"  Error parsing key {key}: {e}")
        
        print(f"  ✅ {len(user_ids)} user(s) cached: {', '.join(user_ids)}")
        return True
    except Exception as e:
        print(f"  ❌ User check failed: {e}")
        return False


def check_table_counts(client, user_id=None, verbose=False):
    """Check number of keys per table"""
    print("\n📊 Checking cached tables...")
    
    tables = [
        'users', 'income_categories', 'expense_categories',
        'income_entries', 'expense_entries', 'recurring_income',
        'recurring_expense', 'starting_balance', 'totals_remainders',
        'totals_remainders_d', 'totals_remainders_m', 'savings_entries',
        'credit_accounts', 'c_expense_categories', 'c_expense_entries',
        'c_a_balances', 'c_a_balances_d', 'c_a_balances_m', 'buds'
    ]
    
    try:
        if user_id:
            print(f"  Checking tables for user {user_id}:")
            for table in tables:
                key = f"{table}:v1:{user_id}"
                if client.exists(key):
                    ttl = client.ttl(key)
                    data = client.get(key)
                    if data:
                        import json
                        rows = json.loads(data)
                        row_count = len(rows) if isinstance(rows, list) else 1
                        print(f"    ✅ {table}: {row_count} rows (TTL: {ttl}s)")
                    else:
                        print(f"    ✅ {table}: exists (TTL: {ttl}s)")
                elif verbose:
                    print(f"    ⚪ {table}: not cached")
        else:
            # Count total keys per table type
            for table in tables:
                pattern = f"{table}:v1:*"
                keys = client.keys(pattern)
                if keys:
                    print(f"  ✅ {table}: {len(keys)} key(s)")
                elif verbose:
                    print(f"  ⚪ {table}: no keys")
        
        return True
    except Exception as e:
        print(f"  ❌ Table check failed: {e}")
        return False


def check_app_health():
    """Check Flask app health endpoints"""
    print("\n🌐 Checking Flask app health...")
    try:
        import requests
        
        base_url = os.getenv("APP_URL", "http://localhost:5000")
        
        # Check Redis health endpoint
        response = requests.get(f"{base_url}/health/redis", timeout=2)
        if response.status_code == 200:
            print(f"  ✅ Redis health endpoint: OK")
        else:
            print(f"  ⚠️  Redis health endpoint: {response.status_code}")
        
        # Check DB pool health endpoint
        response = requests.get(f"{base_url}/health/db-pool", timeout=2)
        if response.status_code == 200:
            data = response.json()
            print(f"  ✅ DB pool health endpoint: OK")
            print(f"     Pool size: {data.get('pool', {}).get('pool_size')}")
            print(f"     Checked out: {data.get('pool', {}).get('checked_out_connections')}")
        else:
            print(f"  ⚠️  DB pool health endpoint: {response.status_code}")
        
        return True
    except requests.exceptions.ConnectionError:
        print(f"  ⚠️  Flask app not running or not accessible")
        return False
    except Exception as e:
        print(f"  ❌ App health check failed: {e}")
        return False


def check_background_workers():
    """Check if background workers are running"""
    print("\n⚙️  Checking background workers...")
    try:
        import threading
        threads = threading.enumerate()
        
        flush_worker = any('RedisFlushWorker' in t.name for t in threads)
        dehydration_worker = any('RedisDehydrationWorker' in t.name for t in threads)
        
        if flush_worker:
            print("  ✅ Flush worker: Running")
        else:
            print("  ⚠️  Flush worker: Not found")
        
        if dehydration_worker:
            print("  ✅ Dehydration worker: Running")
        else:
            print("  ⚠️  Dehydration worker: Not found")
        
        return flush_worker and dehydration_worker
    except Exception as e:
        print(f"  ⚠️  Cannot check workers (app not running): {e}")
        return False


def print_summary(checks):
    """Print overall health summary"""
    print("\n" + "=" * 60)
    passed = sum(1 for c in checks.values() if c)
    total = len(checks)
    
    if passed == total:
        print(f"✅ All checks passed ({passed}/{total})")
        print("\n🎉 Redis hydration system is healthy!")
    else:
        print(f"⚠️  {total - passed} check(s) failed ({passed}/{total} passed)")
        print("\n📝 Review the output above for details")
    
    print("=" * 60)


def watch_mode(interval=10):
    """Continuous monitoring mode"""
    print(f"\n🔄 Watching Redis health (interval: {interval}s, Ctrl+C to stop)...\n")
    try:
        while True:
            os.system('clear' if os.name == 'posix' else 'cls')
            print(f"Redis Health Check - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 60)
            run_checks(verbose=False)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\n👋 Monitoring stopped")


def run_checks(verbose=False, user_id=None):
    """Run all health checks"""
    checks = {}
    
    # Redis connection
    redis_ok, client = check_redis_connection()
    checks['redis'] = redis_ok
    
    if redis_ok and client:
        # Memory usage
        checks['memory'] = check_redis_memory(client)
        
        # Cached users
        checks['users'] = check_cached_users(client, verbose)
        
        # Table counts
        checks['tables'] = check_table_counts(client, user_id, verbose)
    
    # App health
    checks['app'] = check_app_health()
    
    # Background workers (only if app is running)
    if checks['app']:
        checks['workers'] = check_background_workers()
    
    # Summary
    print_summary(checks)
    
    return all(checks.values())


def main():
    parser = argparse.ArgumentParser(
        description='Check Redis hydration system health',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python check_redis_health.py              # Basic health check
  python check_redis_health.py -v           # Verbose output
  python check_redis_health.py -u 123       # Check specific user
  python check_redis_health.py --watch      # Continuous monitoring
  python check_redis_health.py --watch -i 5 # Watch every 5 seconds
        """
    )
    
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('-u', '--user', type=str,
                       help='Check specific user ID')
    parser.add_argument('-w', '--watch', action='store_true',
                       help='Continuous monitoring mode')
    parser.add_argument('-i', '--interval', type=int, default=10,
                       help='Watch mode interval in seconds (default: 10)')
    
    args = parser.parse_args()
    
    print("🏥 Redis Hydration System Health Check")
    print("=" * 60)
    
    if args.watch:
        watch_mode(args.interval)
    else:
        success = run_checks(args.verbose, args.user)
        sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
