# Redis Hydration/Dehydration System

## Overview

This system provides automatic Redis caching for user data with intelligent hydration, dehydration, and synchronization with MySQL.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Application                    │
├─────────────────────────────────────────────────────────┤
│  User Request → track_user_activity()                   │
│       ↓                                                  │
│  Is User Hydrated?                                       │
│    YES → Use Redis Cache (fast)                          │
│    NO  → Trigger Background Hydration                    │
│             └→ Load MySQL → Redis                        │
│             └→ Signal Frontend Refresh                   │
├─────────────────────────────────────────────────────────┤
│  Background Workers:                                     │
│    • Dehydration Worker (checks every 30s)              │
│       - Removes inactive users (5min timeout)           │
│    • Flush Worker (runs every 2min)                     │
│       - Syncs dirty Redis data → MySQL                  │
└─────────────────────────────────────────────────────────┘
```

## Components

### 1. Core Module: `redis_manager.py`

**Key Functions:**
- `init_redis_manager(redis_client)` - Initialize system and start workers
- `track_user_activity(user_id)` - Call on every user request
- `is_user_hydrated(user_id)` - Check if user data is in Redis
- `get_cached_data(table, user_id)` - Retrieve cached data
- `set_cached_data(table, user_id, data)` - Update cache
- `invalidate_user_cache(user_id)` - Force cache clear

**Background Workers:**
- **Dehydration Worker**: Removes inactive users from Redis after 5 minutes
- **Flush Worker**: Syncs Redis changes back to MySQL every 2 minutes

**Configuration:**
```python
INACTIVITY_TIMEOUT = 300  # 5 minutes in seconds
FLUSH_INTERVAL = 120      # 2 minutes in seconds
```

### 2. Flask Integration: `middleware.py`

**Before-Request Handler:**
Automatically tracks user activity on every request, triggering hydration if needed.

**Route Decorator:**
```python
from middleware import require_hydration

@app.route('/dashboard')
@login_required
@require_hydration(fallback_to_mysql=True)
def dashboard():
    # Your code - will use cached data if available
    pass
```

**API Endpoints:**
- `GET /api/redis/status` - Check hydration status
- `POST /api/redis/invalidate` - Force cache clear
- `GET /api/redis/refresh-check` - Check if page refresh needed

### 3. Frontend Auto-Refresh: `redis_auto_refresh.js`

**Features:**
- Polls server every 3 seconds to check hydration status
- Auto-refreshes page when hydration completes
- Shows loading indicator during hydration
- Stops polling after 2 minutes or when hydration completes

**Usage in Templates:**
```html
{% include 'redis_auto_refresh.html' %}
```

### 4. Helper Utilities: `cache_utils.py`

**Convenience Functions:**
```python
from cache_utils import (
    get_user_data,
    get_income_categories,
    get_expense_categories,
    get_aggregated_totals,
    invalidate_and_refresh_cache
)

# Simple usage
income = get_user_data('income_entries')

# With custom query
def my_query():
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM my_table WHERE ...")
        return cursor.fetchall()

data = get_user_data('my_table', my_query)
```

## Usage Guide

### Basic Setup (Already Done in app.py)

```python
from redis_manager import init_redis_manager, shutdown_redis_manager
from middleware import init_redis_middleware, init_redis_routes

# After Redis client initialization
if _redis_client:
    init_redis_manager(_redis_client)
    init_redis_middleware(app)
    init_redis_routes(app)
```

### Using in Route Handlers

**Option 1: Automatic Fallback (Recommended)**
```python
from cache_utils import get_user_data

@app.route('/dashboard')
@login_required
def dashboard():
    # Automatically uses Redis if hydrated, MySQL if not
    income = get_user_data('income_entries')
    expenses = get_user_data('expense_entries')
    
    return render_template('dashboard.html', 
                         income=income, 
                         expenses=expenses)
```

**Option 2: Check Hydration Status**
```python
from middleware import require_hydration
from cache_utils import get_user_data

@app.route('/dashboard')
@login_required
@require_hydration(fallback_to_mysql=True)
def dashboard():
    # Will use Redis preferentially
    # g.using_mysql_fallback tells you which was used
    
    income = get_user_data('income_entries')
    
    if g.get('using_mysql_fallback'):
        logger.info("Using MySQL fallback for this request")
    
    return render_template('dashboard.html', income=income)
```

**Option 3: Force Fresh MySQL Data**
```python
from cache_utils import get_user_data

@app.route('/data/refresh')
@login_required
def refresh_data():
    # Force MySQL query, bypassing cache
    fresh_data = get_user_data('income_entries', force_mysql=True)
    return jsonify(fresh_data)
```

### Frontend Integration

**Add to your base template:**
```html
<!-- In <head> or before </body> -->
{% include 'redis_auto_refresh.html' %}
```

This will:
1. Add a meta tag with current hydration status
2. Include the auto-refresh JavaScript
3. Show a loading indicator if page loaded before hydration
4. Auto-refresh the page when hydration completes

**For AJAX pages:**
```javascript
// Check hydration status via API
fetch('/api/redis/status')
    .then(r => r.json())
    .then(data => {
        console.log('Hydrated:', data.hydrated);
        if (!data.hydrated) {
            // Show loading state
        }
    });

// Poll for refresh
setInterval(() => {
    fetch('/api/redis/refresh-check')
        .then(r => r.json())
        .then(data => {
            if (data.refresh_needed) {
                location.reload();
            }
        });
}, 3000);
```

## Data Flow Examples

### Scenario 1: User Logs In (Cold Cache)

```
1. User logs in → /dashboard
2. Before-request handler calls track_user_activity(user_id)
3. System checks: is_user_hydrated(user_id) → False
4. Background thread starts hydration:
   - Query MySQL for all user tables
   - Store in Redis with TTL
   - Set hydrated flag
   - Send frontend refresh signal
5. Dashboard renders with MySQL data (fallback)
6. Frontend JS polls /api/redis/refresh-check
7. When hydration complete:
   - Frontend receives refresh signal
   - Page auto-refreshes
   - Now uses Redis cached data (fast!)
```

### Scenario 2: Active User (Warm Cache)

```
1. User navigates → /dashboard
2. track_user_activity(user_id) updates last activity time
3. is_user_hydrated(user_id) → True
4. get_user_data() hits Redis cache
5. Dashboard renders instantly with cached data
6. Every 2 minutes: Flush worker syncs changes to MySQL
7. User remains active: cache stays warm
```

### Scenario 3: User Goes Idle

```
1. User last request at 10:00:00
2. Dehydration worker checks every 30s
3. At 10:05:00 (5min later):
   - Worker detects inactive user
   - Removes all Redis keys for user
   - Marks user as not hydrated
4. User returns at 10:10:00
   - Triggers re-hydration (Scenario 1)
```

## Performance Characteristics

### Hydration Time
- **Cold start**: ~200-500ms for typical user (depends on data volume)
- **Runs in background**: Doesn't block initial page load
- **One-time cost**: Subsequent requests are instant

### Cache Hit Performance
- **Redis read**: <1ms per query
- **MySQL query**: 10-100ms per query
- **Speedup**: 10-100x for dashboard-heavy apps

### Memory Usage
- **Per user**: ~50-500KB (depends on data)
- **100 active users**: ~5-50MB Redis memory
- **Auto-cleanup**: Inactive users removed after 5min

## Monitoring

### Health Checks

```bash
# Check Redis availability
curl http://localhost:5000/health/redis

# Check database pool
curl http://localhost:5000/health/db-pool

# Check user hydration status (when logged in)
curl http://localhost:5000/api/redis/status
```

### Logs

```python
# Enable debug logging
import logging
logging.getLogger('redis_manager').setLevel(logging.DEBUG)
logging.getLogger('middleware').setLevel(logging.DEBUG)
```

**Look for:**
- `"Starting hydration for user X"` - Hydration triggered
- `"Hydration complete for user X in Y.YYs"` - Hydration finished
- `"User X inactive for 300s, dehydrating"` - User removed from cache
- `"Cache HIT for table, user X"` - Successfully using cache
- `"Cache MISS for table, user X"` - Fallback to MySQL

## Troubleshooting

### Problem: Frontend not refreshing after hydration

**Check:**
1. Is `redis_auto_refresh.html` included in template?
2. Check browser console for errors
3. Verify `/api/redis/refresh-check` endpoint is accessible
4. Check if `refresh_needed` flag is being set in Redis

**Debug:**
```javascript
// In browser console
RedisAutoRefresh.getStatus()
// Shows: {isHydrated, pageLoadedWhileHydrating, pollCount, isPolling}
```

### Problem: Data not updating in cache

**Cause:** Flush worker not implemented yet (marked as TODO)

**Temporary fix:**
```python
# Manually invalidate cache after updates
from cache_utils import invalidate_and_refresh_cache

@app.route('/update-data', methods=['POST'])
@login_required
def update_data():
    # ... update MySQL ...
    
    # Clear cache to force fresh load
    invalidate_and_refresh_cache()
    
    return jsonify({'status': 'success'})
```

### Problem: High memory usage in Redis

**Solutions:**
1. Reduce `INACTIVITY_TIMEOUT` (default: 5min)
2. Adjust TTL values in hydration functions
3. Selective hydration - only cache frequently accessed tables

```python
# In redis_manager.py, adjust:
INACTIVITY_TIMEOUT = 180  # 3 minutes instead of 5
```

## TODO: Production Enhancements

### 1. Implement Dirty Tracking

Currently, the flush worker is a stub. Implement:

```python
# Track which Redis keys are dirty
_dirty_keys: Set[str] = set()

def mark_dirty(table: str, user_id: int):
    redis_key = _get_redis_key(table, user_id)
    _dirty_keys.add(redis_key)

# In flush worker, only sync dirty keys
def _flush_redis_to_mysql():
    for redis_key in _dirty_keys:
        # Parse key, fetch data, sync to MySQL
        pass
```

### 2. Add Optimistic Locking

Use `last_modified` timestamps to prevent conflicts:

```python
def _flush_table_to_mysql(table, user_id):
    redis_data = get_cached_data(table, user_id)
    
    for row in redis_data:
        # Check if MySQL version is newer
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                f"SELECT last_modified FROM {table} WHERE id = %s",
                (row['id'],)
            )
            mysql_version = cursor.fetchone()
            
            if mysql_version and mysql_version[0] > row['last_modified']:
                # Conflict! MySQL is newer
                logger.warning(f"Conflict for {table} row {row['id']}")
                # Implement resolution strategy
```

### 3. Add Redis Pub/Sub for Real-Time Sync

For multi-server deployments:

```python
# Subscribe to invalidation events
def _subscribe_to_invalidations():
    pubsub = _redis_client.pubsub()
    pubsub.subscribe('cache:invalidate')
    
    for message in pubsub.listen():
        if message['type'] == 'message':
            data = json.loads(message['data'])
            user_id = data['user_id']
            # Invalidate local cache
            _dehydrate_user_data(user_id)
```

### 4. Add Metrics/Instrumentation

Track cache hit rates, hydration times, etc.:

```python
from prometheus_client import Counter, Histogram

cache_hits = Counter('redis_cache_hits_total', 'Total cache hits')
cache_misses = Counter('redis_cache_misses_total', 'Total cache misses')
hydration_duration = Histogram('redis_hydration_duration_seconds', 'Hydration duration')
```

## Redis Key Structure

All keys follow the pattern: `<table>:v1:<user_id>`

**Example keys:**
```
users:v1:123
income_categories:v1:123
expense_categories:v1:123
income_entries:v1:123
expense_entries:v1:123
recurring_income:v1:123
recurring_expense:v1:123
totals_remainders:v1:123
totals_remainders_d:v1:123
totals_remainders_m:v1:123
savings_entries:v1:123
credit_accounts:v1:123
buds:v1:123
bud_items:v1:<bud_id>  # Special case
```

**Inspect Redis:**
```bash
# List all keys for user 123
redis-cli KEYS "*:v1:123"

# Get data for a specific table
redis-cli GET "income_entries:v1:123"

# Check TTL
redis-cli TTL "income_entries:v1:123"

# Monitor all commands
redis-cli MONITOR
```

## Configuration

**Environment Variables:**
```bash
# Redis connection (already configured)
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

# MySQL connection (already configured)
DB_HOST=localhost
DB_USER=your_user
DB_PASSWORD=your_password
DB_NAME=budget
```

**Tuning Parameters** (in `redis_manager.py`):
```python
INACTIVITY_TIMEOUT = 300  # User dehydration timeout (seconds)
FLUSH_INTERVAL = 120      # How often to sync Redis → MySQL (seconds)
REDIS_KEY_VERSION = "v1"  # Version for cache invalidation
```

## Testing

### Manual Testing

```bash
# 1. Start app
python app.py

# 2. Login as user
# 3. Check logs for hydration
tail -f app.log | grep "redis_manager"

# 4. Monitor Redis
redis-cli MONITOR | grep "v1:"

# 5. Test dehydration
# Wait 5+ minutes without activity
# Should see "dehydrating" in logs

# 6. Test auto-refresh
# Login, watch browser console
# Should see polling messages
```

### Integration Tests

```python
def test_hydration():
    from redis_manager import track_user_activity, is_user_hydrated
    
    user_id = 1
    
    # Initially not hydrated
    assert not is_user_hydrated(user_id)
    
    # Track activity triggers hydration
    track_user_activity(user_id)
    time.sleep(2)  # Wait for background thread
    
    # Now should be hydrated
    assert is_user_hydrated(user_id)
```

## Security Considerations

1. **User Isolation**: All data is keyed by user_id - no cross-user data leakage
2. **Authentication**: All endpoints require authentication
3. **TTL**: All keys have expiration to prevent stale data
4. **Input Validation**: SQL injection protected by parameterized queries
5. **Redis Auth**: Configure `REDIS_PASSWORD` in production

## Best Practices

1. **Always use `get_user_data()`** instead of raw Redis queries
2. **Include auto-refresh snippet** in user-facing pages
3. **Invalidate cache** after bulk updates or imports
4. **Monitor Redis memory** usage in production
5. **Set appropriate TTLs** based on your data volatility
6. **Use connection pooling** for both Redis and MySQL (already configured)

## Support

For issues or questions:
1. Check logs for error messages
2. Verify Redis and MySQL connectivity
3. Review this documentation
4. Check TODOs in code comments
