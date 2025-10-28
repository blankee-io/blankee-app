# Quick Reference: Totals, Remainders & Balances Redis Integration

## Quick Start

### Reading Totals/Remainders from Redis

```python
# Get daily totals for a user
cached_daily = _get_totals_remainders_from_redis('totals_remainders_d', user_id)

if cached_daily:
    # Cache hit - use the data
    for row in cached_daily:
        date = row['date']
        total_income = row['total_income']
        total_expenses = row['total_expenses']
        remainder = row['remainder']
else:
    # Cache miss - query MySQL and cache the result
    # ... MySQL query ...
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, mysql_data)
```

### Reading Credit Account Balances

```python
# Get daily CA balances for a user
cached_ca = _get_ca_balances_from_redis('c_a_balances_d', user_id)

# Get balances for a specific account
cached_ca = _get_ca_balances_from_redis('c_a_balances_d', user_id, account_id=123)

# Get balances since a date
cached_ca = _get_ca_balances_from_redis('c_a_balances_d', user_id, start_date=date(2025, 10, 1))
```

### Reading Savings Entries

```python
# Get all savings entries
cached_savings = _get_savings_entries_from_redis(user_id)

# Get savings since a date
cached_savings = _get_savings_entries_from_redis(user_id, start_date=date(2025, 10, 1))
```

## Helper Functions Reference

### Read Functions

| Function | Purpose | Returns |
|----------|---------|---------|
| `_get_totals_remainders_from_redis(table, user_id, start_date)` | Get totals/remainders | List of dicts or None |
| `_get_ca_balances_from_redis(table, user_id, account_id, start_date)` | Get CA balances | List of dicts or None |
| `_get_savings_entries_from_redis(user_id, start_date)` | Get savings | List of dicts or None |

### Write Functions

| Function | Purpose | Use Case |
|----------|---------|----------|
| `_set_totals_remainders_to_redis(table, user_id, data)` | Replace entire cache | After full recalculation |
| `_update_totals_remainders_in_redis(table, user_id, updates)` | Update specific rows | After incremental update |
| `_set_ca_balances_to_redis(table, user_id, data)` | Replace CA cache | After CA recalculation |
| `_set_savings_entries_to_redis(user_id, data)` | Replace savings cache | After savings recalculation |

## Redis Key Patterns

```
# Daily, weekly, monthly totals
totals_remainders:v1:{user_id}      # Weekly
totals_remainders_d:v1:{user_id}    # Daily
totals_remainders_m:v1:{user_id}    # Monthly

# Credit account balances
c_a_balances:v1:{user_id}           # Weekly
c_a_balances_d:v1:{user_id}         # Daily
c_a_balances_m:v1:{user_id}         # Monthly

# Savings
savings_entries:v1:{user_id}        # All savings entries
```

## Common Patterns

### Pattern 1: Try Redis, Fallback to MySQL

```python
def get_user_daily_totals(user_id, date):
    # Try Redis first
    cached = _get_totals_remainders_from_redis('totals_remainders_d', user_id, date)
    
    if cached:
        app.logger.info(f"[REDIS HIT] Daily totals for user {user_id}")
        return cached
    
    # Redis miss - query MySQL
    app.logger.info(f"[REDIS MISS] Daily totals for user {user_id}")
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM totals_remainders_d WHERE user_id = %s AND date >= %s", 
                      (user_id, date))
        data = cursor.fetchall()
    
    # Cache for next time
    if data:
        _set_totals_remainders_to_redis('totals_remainders_d', user_id, data)
    
    return data
```

### Pattern 2: Write-Through Update

```python
def update_user_daily_totals(user_id, date, total_income, total_expenses):
    remainder = total_income - total_expenses
    
    # Write to MySQL first (source of truth)
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("""
            INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_income = VALUES(total_income),
                total_expenses = VALUES(total_expenses),
                remainder = VALUES(remainder)
        """, (user_id, date, total_income, total_expenses, remainder))
    
    # Update Redis cache
    _update_totals_remainders_in_redis('totals_remainders_d', user_id, [{
        'date': date,
        'total_income': float(total_income),
        'total_expenses': float(total_expenses),
        'remainder': float(remainder)
    }])
    
    app.logger.debug(f"[REDIS UPDATE] Daily totals for user {user_id}, date {date}")
```

### Pattern 3: Batch Update with Redis Refresh

```python
def recalculate_all_totals(user_id, start_date):
    # Perform batch MySQL updates
    batch_data = []
    # ... calculate totals ...
    
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.executemany("""
            INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE ...
        """, batch_data)
    
    # Refresh entire Redis cache
    redis_data = []
    for row in batch_data:
        redis_data.append({
            'date': row[1],
            'total_income': float(row[2]),
            'total_expenses': float(row[3]),
            'remainder': float(row[4])
        })
    
    _set_totals_remainders_to_redis('totals_remainders_d', user_id, redis_data)
    app.logger.debug(f"[UPDATE] Daily totals for user {user_id}: {len(batch_data)} rows")
```

## Data Format Examples

### Daily Totals (totals_remainders_d)
```python
{
    'date': '2025-10-22',  # ISO format string
    'total_income': 1500.00,
    'total_expenses': 800.00,
    'remainder': 700.00,
    'last_day_remainder': 500.00
}
```

### Credit Account Balance (c_a_balances_d)
```python
{
    'id': 123,
    'account_id': 45,
    'date': '2025-10-22',
    'total_expenses': 250.00,
    'total_payments': 100.00,
    'balance': 150.00
}
```

### Savings Entry
```python
{
    'date': '2025-10-22',
    'amount': 5000.00
}
```

## Performance Tips

### ✅ DO
- Check Redis before querying MySQL
- Use batch operations when possible
- Update Redis immediately after MySQL writes
- Log cache hits/misses for monitoring
- Handle Redis unavailability gracefully

### ❌ DON'T
- Query MySQL if data is in Redis
- Make multiple Redis calls when one will do
- Assume Redis is always available
- Store sensitive data in Redis without encryption
- Use Redis as primary data store (MySQL is source of truth)

## Debugging

### Check if User is Hydrated
```python
from redis_manager import is_user_hydrated

if is_user_hydrated(user_id):
    print("User data is in Redis")
else:
    print("User needs hydration")
```

### Check Redis Key Contents
```python
# Get raw Redis data
redis_key = f"totals_remainders_d:v1:{user_id}"
raw_data = _redis_client.get(redis_key)

if raw_data:
    data = json.loads(raw_data)
    print(f"Found {len(data)} rows in Redis")
else:
    print("Key not found in Redis")
```

### Force Cache Refresh
```python
# Manually invalidate cache
from redis_manager import invalidate_user_cache

invalidate_user_cache(user_id)

# Next read will trigger MySQL query and cache refresh
```

## Error Handling

```python
try:
    cached = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    
    if cached:
        return cached
    
    # Fallback to MySQL
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM totals_remainders_d WHERE user_id = %s", (user_id,))
        return cursor.fetchall()
        
except Exception as e:
    app.logger.error(f"Error fetching totals for user {user_id}: {e}", exc_info=True)
    # Return empty or default data, or re-raise
    return []
```

## Testing

### Unit Test Example
```python
def test_get_totals_with_cache_hit(mocker):
    # Mock Redis to return cached data
    mocker.patch('app._redis_client.get', return_value=json.dumps([
        {'date': '2025-10-22', 'total_income': 100.0, 'total_expenses': 50.0, 'remainder': 50.0}
    ]))
    
    result = _get_totals_remainders_from_redis('totals_remainders_d', 123)
    
    assert result is not None
    assert len(result) == 1
    assert result[0]['remainder'] == 50.0

def test_get_totals_with_cache_miss(mocker):
    # Mock Redis to return None
    mocker.patch('app._redis_client.get', return_value=None)
    
    result = _get_totals_remainders_from_redis('totals_remainders_d', 123)
    
    assert result is None
```

## Monitoring Queries

### Check Cache Hit Rate
```bash
# In Redis CLI
redis-cli INFO stats | grep keyspace_hits
redis-cli INFO stats | grep keyspace_misses
```

### Check Memory Usage
```bash
redis-cli INFO memory | grep used_memory_human
```

### List User Keys
```bash
redis-cli KEYS "totals_remainders_d:v1:*"
```

### Check TTL
```bash
redis-cli TTL "totals_remainders_d:v1:123"
```

## Configuration

### Set Cache TTL
```python
# In app.py
DASHBOARD_CACHE_TTL = int(os.getenv("DASHBOARD_CACHE_TTL", "60"))  # 60 seconds default
```

### Check Redis Status
```python
if app.config.get('REDIS_OK'):
    print("Redis is available")
else:
    print("Redis is unavailable, using MySQL only")
```

## Quick Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| Always cache miss | Redis not initialized | Check `app.config['REDIS_OK']` |
| Stale data | TTL too long | Reduce `DASHBOARD_CACHE_TTL` |
| High memory | Too much cached data | Enable eviction policy |
| Slow writes | Large batch updates | Use pipelining |
| Connection errors | Redis server down | Check Redis server status |

## Related Documentation

- [Full Migration Guide](TOTALS_REMAINDERS_REDIS_MIGRATION.md)
- [Redis System Architecture](README_REDIS_SYSTEM.md)
- [Redis Hydration Guide](REDIS_HYDRATION_GUIDE.md)
- [Implementation Checklist](IMPLEMENTATION_CHECKLIST.md)
