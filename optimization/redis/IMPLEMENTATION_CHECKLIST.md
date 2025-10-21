# Redis Hydration System - Implementation Checklist

This checklist helps you migrate your existing routes to use the Redis caching system.

## Phase 1: Core Setup ✅ COMPLETE

- [x] Create `redis_manager.py` - Core hydration/dehydration logic
- [x] Create `middleware.py` - Flask integration
- [x] Create `cache_utils.py` - Helper functions
- [x] Create `redis_auto_refresh.js` - Frontend auto-refresh
- [x] Create `redis_auto_refresh.html` - Template snippet
- [x] Integrate into `app.py` - Initialize system
- [x] Add cleanup handlers - Shutdown logic

## Phase 2: Template Updates (TODO)

Update templates to include auto-refresh snippet:

### High Priority (User-facing dashboards)
- [ ] `templates/dashboard.html` - Main dashboard
- [ ] `templates/dashboard_3m.html` - 3-month dashboard
- [ ] `templates/dashboard_d.html` - Daily dashboard
- [ ] `templates/dashboard_m.html` - Monthly dashboard
- [ ] `templates/dashboard_y.html` - Yearly dashboard
- [ ] `templates/profile.html` - User profile
- [ ] `templates/settings.html` - Settings page

### Medium Priority (Data entry/viewing)
- [ ] `templates/recurring_i.html` - Recurring income
- [ ] `templates/recurring_e.html` - Recurring expenses
- [ ] `templates/recurring_ca_e.html` - Recurring credit account expenses
- [ ] `templates/credit_accounts.html` - Credit accounts
- [ ] `templates/buds.html` - Budgets

### Low Priority (Other pages)
- [ ] Any other pages that display user data

**Template Update Pattern:**
```html
<!-- Add before </body> tag -->
{% include 'redis_auto_refresh.html' %}
```

## Phase 3: Route Refactoring (TODO)

Migrate routes to use cache helpers instead of direct DB queries.

### Dashboard Routes

#### dashboard.html route
- [ ] Find the route handler
- [ ] Replace direct queries with `get_user_data()`
- [ ] Test: Login → Check logs for hydration → Verify data loads

**Example:**
```python
# Before
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM income_entries WHERE user_id = %s", (current_user.id,))
    income = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('dashboard.html', income=income)

# After
from cache_utils import get_user_data

@app.route('/dashboard')
@login_required
def dashboard():
    income = get_user_data('income_entries')
    return render_template('dashboard.html', income=income)
```

#### dashboard_3m route
- [ ] Identify queries
- [ ] Replace with `get_aggregated_totals('weekly', start_date, end_date)`
- [ ] Test with date range

#### dashboard_d route
- [ ] Replace with `get_aggregated_totals('daily', start_date, end_date)`
- [ ] Test daily view

#### dashboard_m route
- [ ] Replace with `get_aggregated_totals('monthly', start_date, end_date)`
- [ ] Test monthly view

#### dashboard_y route
- [ ] Replace with `get_aggregated_totals('weekly')` or custom query
- [ ] Test yearly view

### Data Entry Routes

For routes that **modify** data, add cache invalidation:

#### Income Entry Routes
- [ ] `/income/add` - Add `invalidate_and_refresh_cache()` after insert
- [ ] `/income/edit` - Add cache invalidation
- [ ] `/income/delete` - Add cache invalidation

**Pattern:**
```python
from cache_utils import invalidate_and_refresh_cache

@app.route('/income/add', methods=['POST'])
@login_required
def add_income():
    # ... insert into MySQL ...
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("INSERT INTO income_entries ...")
    
    # Invalidate cache to force reload
    invalidate_and_refresh_cache()
    
    return redirect(url_for('dashboard'))
```

#### Expense Entry Routes
- [ ] `/expense/add` - Add cache invalidation
- [ ] `/expense/edit` - Add cache invalidation
- [ ] `/expense/delete` - Add cache invalidation

#### Category Routes
- [ ] `/categories/income/add` - Add cache invalidation
- [ ] `/categories/income/edit` - Add cache invalidation
- [ ] `/categories/expense/add` - Add cache invalidation
- [ ] `/categories/expense/edit` - Add cache invalidation

#### Recurring Entry Routes
- [ ] `/recurring/income/add` - Add cache invalidation
- [ ] `/recurring/income/edit` - Add cache invalidation
- [ ] `/recurring/expense/add` - Add cache invalidation
- [ ] `/recurring/expense/edit` - Add cache invalidation

#### Credit Account Routes
- [ ] `/credit/account/add` - Add cache invalidation
- [ ] `/credit/account/edit` - Add cache invalidation
- [ ] `/credit/expense/add` - Add cache invalidation
- [ ] `/credit/payment/add` - Add cache invalidation

#### Budget (Bud) Routes
- [ ] `/bud/create` - Add cache invalidation
- [ ] `/bud/item/add` - Add cache invalidation
- [ ] `/bud/item/edit` - Add cache invalidation

### Read-Only Routes

For routes that only **read** data, use cache helpers:

#### Profile Route
- [ ] Replace `SELECT * FROM users` with `get_user_profile()`

#### Settings Route
- [ ] Use cache helpers for user preferences
- [ ] Add invalidation after settings updates

#### Report/Analytics Routes
- [ ] Identify all report routes
- [ ] Replace queries with cache helpers
- [ ] Test performance improvement

## Phase 4: Flush Implementation (TODO)

Currently, the flush worker is a framework. Implement actual MySQL sync:

### Implement Dirty Tracking
- [ ] Add `_dirty_keys` set to track modified Redis keys
- [ ] Create `mark_dirty(table, user_id)` function
- [ ] Call `mark_dirty()` whenever cache is updated (not just read)

### Implement Flush Logic per Table
- [ ] `income_entries` - UPSERT logic
- [ ] `expense_entries` - UPSERT logic
- [ ] `savings_entries` - UPSERT logic
- [ ] `income_categories` - UPSERT logic
- [ ] `expense_categories` - UPSERT logic
- [ ] `credit_accounts` - UPSERT logic
- [ ] `c_expense_entries` - UPSERT logic
- [ ] Other tables as needed

**Pattern:**
```python
def _flush_table_to_mysql(table: str, user_id: int):
    redis_key = _get_redis_key(table, user_id)
    redis_data = _redis_client.get(redis_key)
    
    if not redis_data:
        return
    
    rows = json.loads(redis_data)
    
    with get_db_pool().get_cursor(commit=True) as cursor:
        for row in rows:
            # Check if row exists
            cursor.execute(f"SELECT id FROM {table} WHERE id = %s", (row['id'],))
            exists = cursor.fetchone()
            
            if exists:
                # Update
                cursor.execute(f"UPDATE {table} SET ... WHERE id = %s", (...))
            else:
                # Insert
                cursor.execute(f"INSERT INTO {table} (...) VALUES (...)", (...))
```

### Add Conflict Resolution
- [ ] Compare `last_modified` timestamps
- [ ] Implement "last write wins" or other strategy
- [ ] Log conflicts for review

## Phase 5: Testing

### Unit Tests
- [ ] Test hydration for single user
- [ ] Test dehydration after timeout
- [ ] Test concurrent hydration requests
- [ ] Test cache hit/miss logic
- [ ] Test invalidation

### Integration Tests
- [ ] Test full user flow: login → hydrate → use cache → dehydrate
- [ ] Test auto-refresh in browser
- [ ] Test cache invalidation after updates
- [ ] Test flush worker (when implemented)

### Load Tests
- [ ] Test with 10 concurrent users
- [ ] Test with 100 concurrent users
- [ ] Monitor Redis memory usage
- [ ] Monitor MySQL query reduction
- [ ] Measure performance improvement

### Edge Cases
- [ ] Test user with no data
- [ ] Test user with large dataset (10k+ entries)
- [ ] Test rapid login/logout
- [ ] Test Redis failure (should fallback to MySQL)
- [ ] Test MySQL failure during hydration

## Phase 6: Monitoring & Optimization

### Add Metrics
- [ ] Track cache hit rate
- [ ] Track hydration time per user
- [ ] Track dehydration events
- [ ] Track flush success/failure rate
- [ ] Track memory usage per user

### Optimize Hydration
- [ ] Profile slow tables
- [ ] Add indexes if needed
- [ ] Implement selective hydration (only hot tables)
- [ ] Consider pagination for large tables

### Optimize Memory
- [ ] Review TTL values
- [ ] Implement data compression
- [ ] Consider Redis cluster for scale

## Phase 7: Documentation

- [x] Main guide - `REDIS_HYDRATION_GUIDE.md`
- [x] Quick start - `QUICK_START_REDIS.md`
- [x] This checklist - `IMPLEMENTATION_CHECKLIST.md`
- [ ] API documentation for cache_utils functions
- [ ] Team training session/video
- [ ] Deployment guide for production

## Phase 8: Production Deployment

### Pre-deployment
- [ ] Code review completed
- [ ] All tests passing
- [ ] Load testing completed
- [ ] Rollback plan documented
- [ ] Monitoring alerts configured

### Redis Configuration
- [ ] Set Redis password (`REDIS_PASSWORD` env var)
- [ ] Enable Redis persistence (AOF recommended)
- [ ] Configure memory limit (`maxmemory`)
- [ ] Set eviction policy (`maxmemory-policy allkeys-lru`)
- [ ] Enable Redis monitoring

### Application Configuration
- [ ] Set appropriate `INACTIVITY_TIMEOUT` for production
- [ ] Set appropriate `FLUSH_INTERVAL`
- [ ] Configure logging levels
- [ ] Set up log aggregation

### Deployment Steps
- [ ] Deploy to staging
- [ ] Smoke test staging
- [ ] Monitor staging for 24 hours
- [ ] Deploy to production (canary/blue-green)
- [ ] Monitor production closely
- [ ] Document any issues

### Post-deployment
- [ ] Verify cache hit rates improving
- [ ] Verify response times improved
- [ ] Monitor Redis memory usage
- [ ] Monitor MySQL query load reduction
- [ ] Gather user feedback

## Success Metrics

Track these to measure system effectiveness:

### Performance
- [ ] Dashboard load time: Target <50ms (was 150-300ms)
- [ ] Cache hit rate: Target >90%
- [ ] MySQL query reduction: Target >80%

### Reliability
- [ ] No data loss during flush
- [ ] Graceful degradation on Redis failure
- [ ] Zero user-facing errors

### Scalability
- [ ] Support 100+ concurrent users
- [ ] Redis memory usage <1GB for typical load
- [ ] Consistent performance under load

## Notes

### Current Status
- ✅ Core framework complete
- ✅ All modules created
- ✅ Integrated into app.py
- ⏳ Templates need updating
- ⏳ Routes need refactoring
- ⏳ Flush logic needs implementation
- ⏳ Testing needed

### Quick Wins
Start with these for immediate impact:
1. Update `dashboard_3m.html` template (your landing page)
2. Migrate `dashboard_3m` route to use cache
3. Add cache invalidation to top 3 data entry routes
4. Monitor logs to see it working

### Priority Order
1. **High**: Dashboard routes (user sees these most)
2. **Medium**: Data entry routes (add invalidation)
3. **Low**: Admin/settings routes
4. **Future**: Implement flush logic for write-heavy apps

### Time Estimates
- Phase 2 (Templates): 1-2 hours (straightforward find/replace)
- Phase 3 (Routes): 4-8 hours (depends on route complexity)
- Phase 4 (Flush): 8-16 hours (most complex)
- Phase 5 (Testing): 4-8 hours
- Total: 17-34 hours for complete implementation

## Questions?

Refer to:
- `REDIS_HYDRATION_GUIDE.md` - Comprehensive documentation
- `QUICK_START_REDIS.md` - Usage examples
- Code comments in `redis_manager.py` - Implementation details
- `cache_utils.py` - Helper function examples
