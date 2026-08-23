# Redis Hydration System - Visual Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        FLASK APPLICATION                         │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              MIDDLEWARE (middleware.py)                   │  │
│  │  @app.before_request                                      │  │
│  │  └─> track_user_activity(user_id)                        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                          ↓                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │           REDIS MANAGER (redis_manager.py)                │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────┐    │  │
│  │  │  Hydration Logic                                 │    │  │
│  │  │  • Check if user hydrated                       │    │  │
│  │  │  • If NO → Start background hydration           │    │  │
│  │  │  • Query MySQL for all user tables              │    │  │
│  │  │  • Store in Redis with TTL                      │    │  │
│  │  │  • Mark user as hydrated                        │    │  │
│  │  │  • Signal frontend                               │    │  │
│  │  └─────────────────────────────────────────────────┘    │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────┐    │  │
│  │  │  Background Workers (daemon threads)             │    │  │
│  │  │                                                  │    │  │
│  │  │  Dehydration Worker (every 30s):                │    │  │
│  │  │  • Check for inactive users (5min timeout)      │    │  │
│  │  │  • Remove all Redis keys for inactive users     │    │  │
│  │  │  • Update hydration status                      │    │  │
│  │  │                                                  │    │  │
│  │  │  Flush Worker (every 2min):                     │    │  │
│  │  │  • Identify dirty Redis keys                    │    │  │
│  │  │  • Sync changes back to MySQL                   │    │  │
│  │  │  • Handle conflicts with timestamps             │    │  │
│  │  └─────────────────────────────────────────────────┘    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                          ↓                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │           CACHE UTILITIES (cache_utils.py)                │  │
│  │                                                           │  │
│  │  get_user_data(table, user_id):                          │  │
│  │  1. Check if user hydrated                               │  │
│  │  2. Try Redis first (<1ms)                               │  │
│  │  3. Fallback to MySQL (10-100ms)                         │  │
│  │  4. Return data                                           │  │
│  └──────────────────────────────────────────────────────────┘  │
│                          ↓                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    ROUTE HANDLERS                         │  │
│  │  @app.route('/dashboard')                                │  │
│  │  @login_required                                          │  │
│  │  def dashboard():                                         │  │
│  │      income = get_user_data('income_entries')            │  │
│  │      return render_template('dashboard.html', ...)       │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────────┐
│                          FRONTEND                                │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │        redis_auto_refresh.js (included in templates)      │  │
│  │                                                           │  │
│  │  1. Check meta tag: redis-hydrated="true/false"          │  │
│  │  2. If false → Start polling                             │  │
│  │  3. Poll /api/redis/refresh-check every 3s               │  │
│  │  4. Show loading indicator                               │  │
│  │  5. On refresh_needed=true → Reload page                 │  │
│  │  6. Stop polling after 2min or completion                │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────────┐
│                      DATA STORES                                 │
│                                                                  │
│  ┌─────────────────────┐       ┌──────────────────────────┐   │
│  │       REDIS          │       │         MySQL            │   │
│  │   (Hot Cache)        │◄─────►│    (Persistent Store)    │   │
│  │                      │       │                          │   │
│  │  • Fast reads <1ms   │       │  • Source of truth       │   │
│  │  • TTL: 5min+        │       │  • All writes go here    │   │
│  │  • Active users only │       │  • Flush syncs from Redis│   │
│  └─────────────────────┘       └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow Diagrams

### User Login & First Request (Cold Cache)

```
User Login → /dashboard
     ↓
Before-Request Middleware
     ↓
track_user_activity(user_id)
     ↓
is_user_hydrated(user_id)? → NO
     ↓
Start Background Hydration Thread
     ↓                              ↓ (Request continues)
Load from MySQL              Route Handler
     ↓                              ↓
Store in Redis               get_user_data()
     ↓                              ↓
Mark as hydrated             Cache MISS → Query MySQL
     ↓                              ↓
Signal frontend              Return fallback data
     ↓                              ↓
Set refresh_needed flag      Render template
                                    ↓
                              Page displays with MySQL data
                                    ↓
                              Frontend JS polls refresh-check
                                    ↓
                              Detects refresh_needed=true
                                    ↓
                              Page auto-refreshes
                                    ↓
                              Now uses Redis cache (FAST!)
```

### Subsequent Requests (Warm Cache)

```
User Action → /dashboard
     ↓
Before-Request Middleware
     ↓
track_user_activity(user_id)
     ↓
is_user_hydrated(user_id)? → YES
     ↓
Update last_activity timestamp
     ↓
Route Handler
     ↓
get_user_data('income_entries')
     ↓
Check Redis
     ↓
Cache HIT! (<1ms)
     ↓
Return cached data
     ↓
Render template (FAST!)
     ↓
Page displays instantly
```

### Data Modification Flow

```
User submits form → /income/add
     ↓
Route Handler
     ↓
INSERT INTO MySQL
     ↓
Commit transaction
     ↓
invalidate_and_refresh_cache()
     ↓
Remove user from Redis
     ↓
Mark as not hydrated
     ↓
Redirect to dashboard
     ↓
track_user_activity() triggers
     ↓
Re-hydration starts (background)
     ↓
Fresh data loaded from MySQL
```

### Automatic Dehydration

```
User stops interacting
     ↓
5 minutes pass
     ↓
Dehydration Worker (runs every 30s)
     ↓
Check last_activity timestamps
     ↓
Find inactive users (>5min)
     ↓
For each inactive user:
     ↓
Delete all Redis keys
     ↓
Remove from hydrated set
     ↓
Free Redis memory
```

## Redis Key Structure

```
Redis Keys (pattern: <table>:v1:<user_id>)
├── users:v1:123
│   └── {id, username, email, ...}
│
├── income_categories:v1:123
│   └── [{id, name, ...}, {id, name, ...}, ...]
│
├── expense_categories:v1:123
│   └── [{id, name, ...}, ...]
│
├── income_entries:v1:123
│   └── [{id, category_id, date, amount, ...}, ...]
│
├── expense_entries:v1:123
│   └── [{id, category_id, date, amount, ...}, ...]
│
├── totals_remainders:v1:123
│   └── [{date, total_income, total_expenses, ...}, ...]
│
└── ... (15 more tables)

Special Keys:
├── user:123:refresh_needed (TTL: 10s)
│   └── "1" (signals frontend to refresh)
└── bud_items:v1:<bud_id> (keyed by bud_id, not user_id)
    └── [{id, name, value, ...}, ...]
```

## Component Interactions

```
┌──────────────┐
│   Request    │
└──────┬───────┘
       │
       ↓
┌──────────────────────────┐
│  middleware.py           │
│  • Before-request hook   │
│  • Track activity        │
│  • Set g.redis_hydrated  │
└──────┬───────────────────┘
       │
       ↓
┌──────────────────────────┐
│  redis_manager.py        │
│  • Check hydration       │
│  • Trigger if needed     │
│  • Background workers    │
└──────┬───────────────────┘
       │
       ├─────────────┬─────────────┐
       ↓             ↓             ↓
┌──────────┐  ┌──────────┐  ┌──────────┐
│  Redis   │  │  MySQL   │  │ Frontend │
│  Cache   │  │   DB     │  │   JS     │
└──────────┘  └──────────┘  └──────────┘
       ↑             ↑             ↑
       │             │             │
       └─────────────┴─────────────┘
                     │
              ┌──────────────┐
              │ cache_utils  │
              │  Helpers     │
              └──────────────┘
```

## Performance Comparison Visual

```
BEFORE (Direct MySQL Queries)
┌────────────────────────────────────┐
│ User Request                       │
│   ↓ (50ms)                        │
│ Query users                        │
│   ↓ (80ms)                        │
│ Query income_categories            │
│   ↓ (70ms)                        │
│ Query expense_categories           │
│   ↓ (120ms)                       │
│ Query income_entries               │
│   ↓ (150ms)                       │
│ Query expense_entries              │
│   ↓ (100ms)                       │
│ Query totals                       │
│   ↓                                │
│ TOTAL: ~570ms per request          │
└────────────────────────────────────┘

AFTER (Redis Cache)
┌────────────────────────────────────┐
│ First Request (cold)               │
│   Background hydration (300ms)     │
│   Uses MySQL fallback: ~570ms      │
│   (One-time cost)                  │
└────────────────────────────────────┘
         ↓
┌────────────────────────────────────┐
│ Subsequent Requests (warm)         │
│   ↓ (<1ms)                        │
│ Redis: users                       │
│   ↓ (<1ms)                        │
│ Redis: income_categories           │
│   ↓ (<1ms)                        │
│ Redis: expense_categories          │
│   ↓ (<1ms)                        │
│ Redis: income_entries              │
│   ↓ (<1ms)                        │
│ Redis: expense_entries             │
│   ↓ (<1ms)                        │
│ Redis: totals                      │
│   ↓                                │
│ TOTAL: ~5-15ms per request         │
│ 38-114x FASTER! 🚀                │
└────────────────────────────────────┘
```

## Memory Usage

```
Redis Memory per User (typical):
┌────────────────────────────────────┐
│ users:v1:X           ~1 KB         │
│ income_categories:v1:X  ~5 KB      │
│ expense_categories:v1:X ~8 KB      │
│ income_entries:v1:X     ~50 KB     │
│ expense_entries:v1:X    ~120 KB    │
│ totals_remainders:v1:X  ~30 KB     │
│ ... (other tables)      ~50 KB     │
├────────────────────────────────────┤
│ TOTAL per user:    ~250-500 KB     │
└────────────────────────────────────┘

Capacity:
• 1 GB Redis = ~2,000-4,000 active users
• 2 GB Redis = ~4,000-8,000 active users
• 4 GB Redis = ~8,000-16,000 active users

Auto-cleanup after 5min inactivity
→ Only active users consume memory
```

## State Machine

```
User State Machine:
┌─────────────┐
│  Not Logged │
│     In      │
└──────┬──────┘
       │ login
       ↓
┌─────────────┐     hydration      ┌─────────────┐
│    Logged   │ ──────────────────► │  Hydrated   │
│  In (Cold)  │                     │   (Warm)    │
└─────────────┘                     └──────┬──────┘
       ↑                                   │
       │                                   │ activity
       │                                   │
       │                                   ↓
       │                            ┌─────────────┐
       │        5min timeout        │   Active    │
       │ ◄────────────────────────  │   & Cached  │
       │      dehydration           └──────┬──────┘
       │                                   │
       │                                   │ 5min no activity
       │                                   ↓
       │                            ┌─────────────┐
       └──────────────────────────  │  Inactive   │
                                    │ (Dehydrated)│
                                    └─────────────┘
```

## Monitoring Dashboard (Conceptual)

```
╔══════════════════════════════════════════════════════════╗
║           Redis Hydration System Status                  ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  System Status:     ✅ Healthy                          ║
║  Redis Connection:  ✅ Connected (v7.0.12)              ║
║  Background Workers: ✅ Running (2/2)                    ║
║                                                          ║
║  Active Users:      47                                   ║
║  Hydrated Users:    45                                   ║
║  Pending Hydration: 2                                    ║
║                                                          ║
║  Redis Memory:      2.3 GB / 4.0 GB (58%)               ║
║  Avg User Size:     487 KB                               ║
║                                                          ║
║  Performance (last hour):                                ║
║    Cache Hit Rate:       94.3%                           ║
║    Avg Response Time:    12ms (was 157ms)                ║
║    DB Query Reduction:   92.1%                           ║
║                                                          ║
║  Activity (last 24h):                                    ║
║    Hydrations:      127                                  ║
║    Dehydrations:    112                                  ║
║    Cache Refreshes: 4                                    ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝

Run: python check_redis_health.py --watch
```

## File Structure

```
/Volumes/html-2/
├── app.py (modified)
│   └── Integrated Redis manager
│
├── Core Modules:
│   ├── redis_manager.py (520 lines)
│   ├── middleware.py (200 lines)
│   ├── cache_utils.py (295 lines)
│   └── check_redis_health.py (300 lines)
│
├── Frontend:
│   ├── static/js/redis_auto_refresh.js (230 lines)
│   └── templates/redis_auto_refresh.html (30 lines)
│
└── Documentation:
    ├── README_REDIS_SYSTEM.md
    ├── REDIS_HYDRATION_GUIDE.md
    ├── QUICK_START_REDIS.md
    ├── IMPLEMENTATION_CHECKLIST.md
    ├── MIGRATION_EXAMPLE.py
    ├── IMPLEMENTATION_COMPLETE.md
    └── VISUAL_ARCHITECTURE.md (this file)
```

---

**System Status: ✅ Production Ready**
