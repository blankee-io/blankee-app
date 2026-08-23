# Testing Guide: Totals, Remainders & Balances Redis Integration

## Test Scenarios

This document provides comprehensive test scenarios for validating the Redis integration for totals, remainders, and balances calculations.

## Pre-Test Setup

### 1. Verify Redis is Running
```bash
redis-cli ping
# Expected: PONG
```

### 2. Clear Test Data (Optional)
```bash
redis-cli FLUSHDB
# WARNING: This clears all data in current Redis DB
```

### 3. Check Application Configuration
```python
# In Python shell or test file
from app import app
print(f"Redis OK: {app.config.get('REDIS_OK')}")
print(f"Cache TTL: {app.DASHBOARD_CACHE_TTL}")
```

## Unit Tests

### Test 1: Redis Helper - Get Totals (Cache Hit)
```python
def test_get_totals_remainders_cache_hit():
    """Test fetching totals from Redis when data exists"""
    user_id = 123
    table_name = 'totals_remainders_d'
    
    # Setup: Put data in Redis
    test_data = [
        {'date': '2025-10-22', 'total_income': 1000.0, 'total_expenses': 600.0, 'remainder': 400.0},
        {'date': '2025-10-23', 'total_income': 1200.0, 'total_expenses': 700.0, 'remainder': 500.0}
    ]
    _set_totals_remainders_to_redis(table_name, user_id, test_data)
    
    # Test
    result = _get_totals_remainders_from_redis(table_name, user_id)
    
    # Assertions
    assert result is not None
    assert len(result) == 2
    assert result[0]['remainder'] == 400.0
    assert result[1]['remainder'] == 500.0
```

### Test 2: Redis Helper - Get Totals (Cache Miss)
```python
def test_get_totals_remainders_cache_miss():
    """Test fetching totals when Redis has no data"""
    user_id = 999  # Non-existent user
    table_name = 'totals_remainders_d'
    
    # Test
    result = _get_totals_remainders_from_redis(table_name, user_id)
    
    # Assertions
    assert result is None
```

### Test 3: Redis Helper - Filter by Date
```python
def test_get_totals_remainders_with_date_filter():
    """Test date filtering in Redis fetch"""
    user_id = 123
    table_name = 'totals_remainders_d'
    
    # Setup
    test_data = [
        {'date': '2025-10-20', 'total_income': 1000.0, 'remainder': 400.0},
        {'date': '2025-10-21', 'total_income': 1100.0, 'remainder': 450.0},
        {'date': '2025-10-22', 'total_income': 1200.0, 'remainder': 500.0}
    ]
    _set_totals_remainders_to_redis(table_name, user_id, test_data)
    
    # Test with date filter
    from datetime import date
    start_date = date(2025, 10, 21)
    result = _get_totals_remainders_from_redis(table_name, user_id, start_date)
    
    # Assertions
    assert len(result) == 2  # Only 10-21 and 10-22
    assert result[0]['date'] in ['2025-10-21', '2025-10-22']
```

### Test 4: Update Totals in Redis
```python
def test_update_totals_in_redis():
    """Test updating specific rows in Redis cache"""
    user_id = 123
    table_name = 'totals_remainders_d'
    
    # Setup: Initial data
    initial_data = [
        {'date': '2025-10-22', 'total_income': 1000.0, 'remainder': 400.0},
        {'date': '2025-10-23', 'total_income': 1200.0, 'remainder': 500.0}
    ]
    _set_totals_remainders_to_redis(table_name, user_id, initial_data)
    
    # Test: Update one row
    from datetime import date
    updates = [
        {'date': date(2025, 10, 22), 'total_income': 1500.0, 'remainder': 700.0}
    ]
    _update_totals_remainders_in_redis(table_name, user_id, updates)
    
    # Verify
    result = _get_totals_remainders_from_redis(table_name, user_id)
    updated_row = [r for r in result if r['date'] == '2025-10-22'][0]
    assert updated_row['total_income'] == 1500.0
    assert updated_row['remainder'] == 700.0
```

### Test 5: CA Balances with Account Filter
```python
def test_get_ca_balances_with_account_filter():
    """Test fetching CA balances filtered by account"""
    user_id = 123
    table_name = 'c_a_balances_d'
    
    # Setup
    test_data = [
        {'id': 1, 'account_id': 10, 'date': '2025-10-22', 'balance': 100.0},
        {'id': 2, 'account_id': 20, 'date': '2025-10-22', 'balance': 200.0},
        {'id': 3, 'account_id': 10, 'date': '2025-10-23', 'balance': 150.0}
    ]
    _set_ca_balances_to_redis(table_name, user_id, test_data)
    
    # Test with account filter
    result = _get_ca_balances_from_redis(table_name, user_id, account_id=10)
    
    # Assertions
    assert len(result) == 2  # Only account_id=10
    assert all(r['account_id'] == 10 for r in result)
```

## Integration Tests

### Test 6: Full Update Cycle - Daily Totals
```python
def test_update_daily_totals_full_cycle():
    """Test complete update cycle: MySQL write + Redis cache"""
    user_id = 123
    start_date = date(2025, 10, 20)
    goofy_week_mode = False
    date_to_remainder = {}
    
    # Execute update
    update_daily_totals(user_id, start_date, goofy_week_mode, date_to_remainder)
    
    # Verify MySQL
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("""
            SELECT * FROM totals_remainders_d 
            WHERE user_id = %s AND date >= %s
        """, (user_id, start_date))
        mysql_data = cursor.fetchall()
    
    # Verify Redis
    redis_data = _get_totals_remainders_from_redis('totals_remainders_d', user_id, start_date)
    
    # Assertions
    assert mysql_data is not None
    assert redis_data is not None
    assert len(mysql_data) == len(redis_data)
    
    # Verify data consistency
    for mysql_row in mysql_data:
        redis_row = next((r for r in redis_data if r['date'] == mysql_row['date'].isoformat()), None)
        assert redis_row is not None
        assert float(mysql_row['total_income']) == redis_row['total_income']
        assert float(mysql_row['remainder']) == redis_row['remainder']
```

### Test 7: Route Handler - save_totals_remainders_d (Cache Hit)
```python
def test_save_totals_remainders_d_cache_hit(client, auth):
    """Test save route with Redis cache hit"""
    # Login
    auth.login()
    
    # Pre-populate Redis cache
    user_id = 1  # Assuming test user ID
    test_data = [
        {'date': '2025-10-22', 'total_income': 1000.0, 'total_expenses': 600.0, 'remainder': 400.0}
    ]
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, test_data)
    _set_totals_remainders_to_redis('totals_remainders', user_id, test_data)
    _set_totals_remainders_to_redis('totals_remainders_m', user_id, test_data)
    _set_savings_entries_to_redis(user_id, test_data)
    
    # Make request
    response = client.post('/save_totals_remainders_d', json={
        'start_date': '2025-10-22'
    })
    
    # Assertions
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'updated_totals_remainders' in data
    
    # Verify Redis was used (check logs)
    # This would require log capture in test framework
```

### Test 8: Route Handler - get_totals_for_day (Cache Hit)
```python
def test_get_totals_for_day_cache_hit(client, auth):
    """Test get totals route with Redis cache hit"""
    auth.login()
    user_id = 1
    
    # Pre-populate cache
    test_data = [
        {'date': '2025-10-22', 'total_income': 1000.0, 'total_expenses': 600.0, 'remainder': 400.0}
    ]
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, test_data)
    
    # Make request
    response = client.get('/dashboard-d/get_totals_for_day?date=2025-10-22')
    
    # Assertions
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert data['total_income'] == 1000.0
    assert data['total_expenses'] == 600.0
    assert data['remainder'] == 400.0
```

### Test 9: Route Handler - update_totals_for_day (Redis Update)
```python
def test_update_totals_for_day_redis_update(client, auth):
    """Test update route updates both MySQL and Redis"""
    auth.login()
    user_id = 1
    
    # Make update request
    response = client.post('/dashboard-d/update_totals_for_day', json={
        'date': '2025-10-22'
    })
    
    # Assertions
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    
    # Verify Redis was updated
    cached = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    assert cached is not None
    
    # Find the updated date
    updated_row = next((r for r in cached if r['date'] == '2025-10-22'), None)
    assert updated_row is not None
    assert 'remainder' in updated_row
```

### Test 10: Dashboard Data (Full Stack)
```python
def test_get_dashboard_d_data_cache_hit(client, auth):
    """Test dashboard data endpoint with full Redis cache"""
    auth.login()
    user_id = 1
    
    # Pre-populate all required caches
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, [
        {'date': '2025-10-22', 'remainder': 400.0}
    ])
    _set_ca_balances_to_redis('c_a_balances_d', user_id, [
        {'account_id': 1, 'date': '2025-10-22', 'balance': 100.0}
    ])
    _set_savings_entries_to_redis(user_id, [
        {'date': '2025-10-22', 'amount': 5000.0}
    ])
    
    # Make request
    response = client.get('/get_dashboard_d_data')
    
    # Assertions
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'totals_remainders_d' in data
    assert 'c_a_balances_d' in data
    assert 'savings_entries' in data
```

## Performance Tests

### Test 11: Response Time Comparison
```python
import time

def test_response_time_redis_vs_mysql():
    """Compare response times with and without Redis"""
    user_id = 1
    
    # Test 1: Cold cache (MySQL)
    _redis_client.delete(f"totals_remainders_d:v1:{user_id}")
    start = time.time()
    result1 = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    # result1 will be None, triggering MySQL fallback
    mysql_time = time.time() - start
    
    # Test 2: Warm cache (Redis)
    # Populate cache first
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, [
        {'date': '2025-10-22', 'remainder': 400.0}
    ])
    start = time.time()
    result2 = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    redis_time = time.time() - start
    
    # Assertions
    print(f"MySQL time: {mysql_time*1000:.2f}ms")
    print(f"Redis time: {redis_time*1000:.2f}ms")
    assert redis_time < mysql_time, "Redis should be faster than MySQL"
    assert redis_time < 0.05, "Redis lookup should be under 50ms"
```

### Test 12: Concurrent Access
```python
import concurrent.futures

def test_concurrent_redis_access():
    """Test Redis caching under concurrent load"""
    user_id = 123
    num_threads = 10
    
    # Pre-populate cache
    test_data = [{'date': '2025-10-22', 'remainder': 400.0}]
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, test_data)
    
    def fetch_data():
        return _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    
    # Execute concurrent fetches
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(fetch_data) for _ in range(num_threads)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    
    # Assertions
    assert all(r is not None for r in results), "All threads should get cached data"
    assert len(results) == num_threads
```

## Error Handling Tests

### Test 13: Redis Unavailable (Graceful Degradation)
```python
def test_redis_unavailable_fallback(mocker):
    """Test fallback to MySQL when Redis is unavailable"""
    user_id = 123
    
    # Mock Redis to raise exception
    mocker.patch('app._redis_client.get', side_effect=Exception("Redis connection failed"))
    
    # Test - should not raise exception
    result = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    
    # Assertions
    assert result is None  # Should return None, not raise exception
```

### Test 14: Partial Cache Miss
```python
def test_partial_cache_miss():
    """Test behavior when some but not all data is cached"""
    user_id = 123
    
    # Cache only daily totals, not weekly or monthly
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, [
        {'date': '2025-10-22', 'remainder': 400.0}
    ])
    
    # Try to get all data
    daily = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    weekly = _get_totals_remainders_from_redis('totals_remainders', user_id)
    monthly = _get_totals_remainders_from_redis('totals_remainders_m', user_id)
    
    # Assertions
    assert daily is not None
    assert weekly is None
    assert monthly is None
```

## Data Consistency Tests

### Test 15: MySQL-Redis Consistency After Update
```python
def test_mysql_redis_consistency():
    """Verify MySQL and Redis contain same data after update"""
    user_id = 123
    test_date = date(2025, 10, 22)
    
    # Perform update
    from datetime import timedelta
    update_daily_totals(user_id, test_date, False, {})
    
    # Fetch from MySQL
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("""
            SELECT date, total_income, total_expenses, remainder 
            FROM totals_remainders_d 
            WHERE user_id = %s AND date = %s
        """, (user_id, test_date))
        mysql_data = cursor.fetchone()
    
    # Fetch from Redis
    redis_data = _get_totals_remainders_from_redis('totals_remainders_d', user_id, test_date)
    redis_row = redis_data[0] if redis_data else None
    
    # Assertions
    assert mysql_data is not None
    assert redis_row is not None
    assert float(mysql_data['total_income']) == redis_row['total_income']
    assert float(mysql_data['total_expenses']) == redis_row['total_expenses']
    assert float(mysql_data['remainder']) == redis_row['remainder']
```

### Test 16: Cache Expiration (TTL)
```python
import time

def test_cache_ttl_expiration():
    """Test that cache expires after TTL"""
    user_id = 123
    
    # Set data with short TTL (for testing)
    # Note: You may need to temporarily modify DASHBOARD_CACHE_TTL for this test
    test_data = [{'date': '2025-10-22', 'remainder': 400.0}]
    
    # Manually set with 2 second TTL
    redis_key = f"totals_remainders_d:v1:{user_id}"
    _redis_client.setex(redis_key, 2, json.dumps(test_data))
    
    # Verify data exists
    result1 = _redis_client.get(redis_key)
    assert result1 is not None
    
    # Wait for expiration
    time.sleep(3)
    
    # Verify data expired
    result2 = _redis_client.get(redis_key)
    assert result2 is None
```

## Load Tests

### Test 17: Bulk Data Caching
```python
def test_bulk_data_caching():
    """Test caching large amounts of data"""
    user_id = 123
    
    # Create 1 year of daily data (365 rows)
    from datetime import timedelta
    base_date = date(2025, 1, 1)
    test_data = []
    
    for i in range(365):
        current_date = base_date + timedelta(days=i)
        test_data.append({
            'date': current_date.isoformat(),
            'total_income': 1000.0 + i,
            'total_expenses': 600.0 + i,
            'remainder': 400.0
        })
    
    # Cache all data
    start = time.time()
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, test_data)
    cache_time = time.time() - start
    
    # Retrieve all data
    start = time.time()
    result = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    fetch_time = time.time() - start
    
    # Assertions
    assert len(result) == 365
    print(f"Cache write time: {cache_time*1000:.2f}ms")
    print(f"Cache read time: {fetch_time*1000:.2f}ms")
    assert cache_time < 1.0, "Caching 365 rows should take under 1 second"
    assert fetch_time < 0.1, "Fetching from cache should take under 100ms"
```

## Test Fixtures

### pytest conftest.py
```python
import pytest
from app import app, _redis_client
from db_connections import init_db_pool

@pytest.fixture
def client():
    """Flask test client"""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture
def auth(client):
    """Authentication helper"""
    class AuthActions:
        def login(self, username='testuser', password='testpass'):
            return client.post('/login', data={
                'username': username,
                'password': password
            })
        
        def logout(self):
            return client.get('/logout')
    
    return AuthActions()

@pytest.fixture(autouse=True)
def setup_teardown():
    """Setup and teardown for each test"""
    # Setup
    init_db_pool()
    
    yield
    
    # Teardown - clean up test data
    if _redis_client:
        # Clean up test user data
        test_keys = _redis_client.keys("*:v1:123")  # Test user ID
        if test_keys:
            _redis_client.delete(*test_keys)
```

## Running Tests

### Run All Tests
```bash
pytest tests/test_totals_redis.py -v
```

### Run Specific Test
```bash
pytest tests/test_totals_redis.py::test_get_totals_remainders_cache_hit -v
```

### Run with Coverage
```bash
pytest tests/test_totals_redis.py --cov=app --cov-report=html
```

### Run Performance Tests Only
```bash
pytest tests/test_totals_redis.py -m performance -v
```

## Expected Results

### Performance Benchmarks
- Redis read: < 10ms
- Redis write: < 20ms
- MySQL fallback: 100-500ms
- Full dashboard load (cache hit): < 50ms
- Full dashboard load (cache miss): 800-1200ms

### Cache Hit Rates
- Active users: > 85%
- All users: > 70%
- Peak hours: > 90%

## Troubleshooting Test Failures

### Redis Connection Failures
```bash
# Check Redis is running
redis-cli ping

# Check Redis port
netstat -an | grep 6379
```

### Data Consistency Failures
```bash
# Compare MySQL and Redis data
redis-cli GET "totals_remainders_d:v1:123"
mysql -e "SELECT * FROM totals_remainders_d WHERE user_id = 123 LIMIT 5"
```

### Performance Test Failures
```bash
# Monitor Redis performance
redis-cli --latency
redis-cli --latency-history

# Check Redis slow log
redis-cli SLOWLOG GET 10
```

## Continuous Integration

### GitHub Actions Example
```yaml
name: Test Redis Integration

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379
      mysql:
        image: mysql:8
        env:
          MYSQL_ROOT_PASSWORD: test
          MYSQL_DATABASE: budget_test
        ports:
          - 3306:3306
    
    steps:
      - uses: actions/checkout@v2
      
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov pytest-mock
      
      - name: Run tests
        run: pytest tests/test_totals_redis.py -v --cov
        env:
          REDIS_HOST: localhost
          REDIS_PORT: 6379
          DB_HOST: localhost
          DB_USER: root
          DB_PASSWORD: test
          DB_NAME: budget_test
```

## Additional Resources

- [Full Migration Guide](TOTALS_REMAINDERS_REDIS_MIGRATION.md)
- [Quick Reference](QUICK_REFERENCE_TOTALS.md)
- [Redis Best Practices](https://redis.io/topics/best-practices-testing)
- [pytest Documentation](https://docs.pytest.org/)
