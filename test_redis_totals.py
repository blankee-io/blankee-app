#!/usr/bin/env python3
"""
Quick diagnostic script to test Redis integration for totals/remainders/balances.
Run this to verify Redis writes are working correctly.
"""

import os
import sys
import json
from datetime import date, timedelta
from decimal import Decimal

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import from app
from app import app, _redis_client, _update_totals_remainders_in_redis, _get_totals_remainders_from_redis

def test_redis_connection():
    """Test basic Redis connectivity"""
    print("=" * 60)
    print("TEST 1: Redis Connection")
    print("=" * 60)
    
    if not _redis_client:
        print("❌ FAIL: Redis client not initialized")
        return False
    
    try:
        result = _redis_client.ping()
        if result:
            print("✅ PASS: Redis is connected and responding")
            return True
        else:
            print("❌ FAIL: Redis ping returned False")
            return False
    except Exception as e:
        print(f"❌ FAIL: Redis connection error: {e}")
        return False

def test_redis_config():
    """Test Redis configuration"""
    print("\n" + "=" * 60)
    print("TEST 2: Redis Configuration")
    print("=" * 60)
    
    redis_ok = app.config.get('REDIS_OK', False)
    print(f"Redis OK: {redis_ok}")
    print(f"Redis Host: {os.getenv('REDIS_HOST', '127.0.0.1')}")
    print(f"Redis Port: {os.getenv('REDIS_PORT', '6379')}")
    print(f"Redis DB: {os.getenv('REDIS_DB', '0')}")
    
    if redis_ok:
        print("✅ PASS: Redis is configured correctly")
        return True
    else:
        print("❌ FAIL: Redis is not configured correctly")
        return False

def test_write_totals():
    """Test writing totals to Redis"""
    print("\n" + "=" * 60)
    print("TEST 3: Write Totals to Redis")
    print("=" * 60)
    
    test_user_id = 99999  # Use a test user ID that won't conflict
    test_data = [
        {
            'date': date.today(),
            'total_income': 1000.0,
            'total_expenses': 600.0,
            'remainder': 400.0,
            'last_day_remainder': 0.0
        },
        {
            'date': date.today() - timedelta(days=1),
            'total_income': 1200.0,
            'total_expenses': 700.0,
            'remainder': 500.0,
            'last_day_remainder': 400.0
        }
    ]
    
    try:
        _update_totals_remainders_in_redis('totals_remainders_d', test_user_id, test_data)
        print(f"✅ Write operation completed (user {test_user_id})")
        return True
    except Exception as e:
        print(f"❌ FAIL: Write error: {e}")
        return False

def test_read_totals():
    """Test reading totals from Redis"""
    print("\n" + "=" * 60)
    print("TEST 4: Read Totals from Redis")
    print("=" * 60)
    
    test_user_id = 99999
    
    try:
        data = _get_totals_remainders_from_redis('totals_remainders_d', test_user_id)
        
        if data is None:
            print("❌ FAIL: No data returned from Redis")
            return False
        
        if len(data) == 0:
            print("❌ FAIL: Empty array returned from Redis")
            return False
        
        print(f"✅ PASS: Read {len(data)} rows from Redis")
        print(f"Sample data: {data[0]}")
        return True
    except Exception as e:
        print(f"❌ FAIL: Read error: {e}")
        return False

def test_data_consistency():
    """Test data consistency between write and read"""
    print("\n" + "=" * 60)
    print("TEST 5: Data Consistency")
    print("=" * 60)
    
    test_user_id = 99999
    expected_remainder = 400.0
    
    try:
        data = _get_totals_remainders_from_redis('totals_remainders_d', test_user_id)
        
        if not data:
            print("❌ FAIL: No data to verify")
            return False
        
        # Find today's data
        today_str = date.today().isoformat()
        today_data = next((row for row in data if row['date'] == today_str), None)
        
        if not today_data:
            print(f"❌ FAIL: Today's data not found (looking for {today_str})")
            return False
        
        if today_data['remainder'] == expected_remainder:
            print(f"✅ PASS: Data is consistent (remainder={expected_remainder})")
            return True
        else:
            print(f"❌ FAIL: Data mismatch. Expected {expected_remainder}, got {today_data['remainder']}")
            return False
    except Exception as e:
        print(f"❌ FAIL: Consistency check error: {e}")
        return False

def test_redis_key_exists():
    """Test if Redis key was actually created"""
    print("\n" + "=" * 60)
    print("TEST 6: Redis Key Existence")
    print("=" * 60)
    
    test_user_id = 99999
    redis_key = f"totals_remainders_d:v1:{test_user_id}"
    
    try:
        exists = _redis_client.exists(redis_key)
        
        if exists:
            print(f"✅ PASS: Redis key exists: {redis_key}")
            
            # Get TTL
            ttl = _redis_client.ttl(redis_key)
            print(f"   TTL: {ttl} seconds")
            
            # Get key size
            size = _redis_client.memory_usage(redis_key)
            if size:
                print(f"   Size: {size} bytes")
            
            return True
        else:
            print(f"❌ FAIL: Redis key does not exist: {redis_key}")
            return False
    except Exception as e:
        print(f"❌ FAIL: Key check error: {e}")
        return False

def cleanup():
    """Clean up test data"""
    print("\n" + "=" * 60)
    print("CLEANUP: Removing Test Data")
    print("=" * 60)
    
    test_user_id = 99999
    redis_key = f"totals_remainders_d:v1:{test_user_id}"
    
    try:
        _redis_client.delete(redis_key)
        print(f"✅ Test data cleaned up")
    except Exception as e:
        print(f"⚠️  Cleanup warning: {e}")

def main():
    """Run all tests"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "Redis Totals Integration Test" + " " * 19 + "║")
    print("╚" + "=" * 58 + "╝")
    
    results = []
    
    # Run tests
    with app.app_context():
        results.append(("Redis Connection", test_redis_connection()))
        results.append(("Redis Configuration", test_redis_config()))
        results.append(("Write Totals", test_write_totals()))
        results.append(("Read Totals", test_read_totals()))
        results.append(("Data Consistency", test_data_consistency()))
        results.append(("Redis Key Exists", test_redis_key_exists()))
        
        # Cleanup
        cleanup()
    
    # Print summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print("\n" + "=" * 60)
    print(f"Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("⚠️  SOME TESTS FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main())
