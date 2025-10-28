#!/usr/bin/env python3
"""Quick check if Redis writes are working for totals"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, _redis_client

print("Checking Redis status...")
print(f"Redis client exists: {_redis_client is not None}")
print(f"Redis OK config: {app.config.get('REDIS_OK', False)}")

if _redis_client:
    try:
        # Test ping
        _redis_client.ping()
        print("✅ Redis is responding to ping")
        
        # Test write
        test_key = "test:totals:check"
        _redis_client.setex(test_key, 10, "test_value")
        print("✅ Redis write successful")
        
        # Test read
        value = _redis_client.get(test_key)
        if value == "test_value":
            print("✅ Redis read successful")
        else:
            print(f"❌ Redis read returned unexpected value: {value}")
        
        # Cleanup
        _redis_client.delete(test_key)
        print("✅ Redis cleanup successful")
        
        # Check for existing totals keys
        keys = _redis_client.keys("totals_remainders*:v1:*")
        print(f"\n📊 Found {len(keys)} existing totals/remainders keys in Redis")
        if keys:
            print("Sample keys:")
            for key in keys[:5]:
                ttl = _redis_client.ttl(key)
                print(f"  - {key} (TTL: {ttl}s)")
        
    except Exception as e:
        print(f"❌ Redis error: {e}")
else:
    print("❌ Redis client is not initialized")
