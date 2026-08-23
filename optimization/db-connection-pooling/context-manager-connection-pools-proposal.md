# ------------------------------------------------------------------------------------------ #
        PROPOSAL & PLANNING: 
        Migrate Existing DB Connections to Context Manager Connection Pooling
# ------------------------------------------------------------------------------------------ #


# ------------------------------ #
    CONFIGURATION PATTERN:
# ------------------------------ #

# CURRENTLY: manual connection management (example)
conn = get_db_connection()
cursor = conn.cursor()
cursor.execute("SELECT * FROM users")
results = cursor.fetchall()
cursor.close()
conn.close()    # Risk of connection leaks!

# AFTER: context managers with connection pooling (example)
with get_db_pool().get_cursor() as cursor:
    cursor.execute("SELECT * FROM users")
    results = cursor.fetchall()     # automatic cleanup, connection returned to pool

# ------------------------------ #
    BENEFITS
# ------------------------------ #

# Connection Reuse
1. Connections are pooled and reused instead of creating new ones for each request
2. Reduces database connection overhead
3. Better performance under load
# Automatic Resource Management
1. Context managers ensure connections are always returned to the pool
2. No more leaked connections from forgotten conn.close() calls
3. Automatic rollback on exceptions
# Connection Health Checking
1. pool_pre_ping=True detects and replaces stale connections
2. Prevents "MySQL server has gone away" errors
3. More reliable in production
# Better Error Handling
1. Automatic rollback on exceptions when using commit=True
2. Simplified error handling code
3. Less boilerplate
# Resource Limits
1. Pool limits prevent connection exhaustion
2. Configurable maximum connections
3. Better resource control under load

# ------------------------------ #
    KEY IMPLEMENTATION DETAILS
# ------------------------------ #  

# MODIFY
requirements.txt
# Additions
    SQLAlchemy==2.0.23
    pymysql==1.1.0

# CREATE
db_connections.py
# class with configurable pooling
    DatabaseConnectionPool 
# Two context managers:
    get_cursor()              # For simple queries (auto-manages connection)
    get_connection()          # For advanced use (dictionary cursors, multiple cursors)
# Connection Pool Configuration:
    pool_size=5               # Persistent connections
    max_overflow=25           # Additional connections for bursts; temporary overflow (total max: 30)
    pool_timeout=30           # Wait timeout (seconds)
    pool_recycle=1800         # Recycle after 30 minutes (AWS RDS friendly)
    pool_pre_ping=True        # Connection health check before use
    isolation_level="READ COMMITTED"
# Key Functions:
    init_db_pool()            # Initialize the global pool (called at app startup)
    get_db_pool()             # Get the pool instance
    dispose_db_pool()         # Cleanup (for app shutdown)

# MODIFY
app.py
# Add import: 
    from db_connections import init_db_pool, get_db_pool, dispose_db_pool
# Initialize pool at startup: 
    init_db_pool()

# Example Refactoring
# Before:
def some_function():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET name = %s WHERE id = %s", (name, user_id))
    conn.commit()
    conn.close()
# After:
def some_function():
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE users SET name = %s WHERE id = %s", (name, user_id))

# PATTERN TYPES TO IMPLEMENT:
# Auto-Commit Pattern:
with get_db_pool().get_cursor(commit=True) as cursor:
    cursor.execute("DELETE FROM table WHERE id = %s", (id,))
# Dictionary Cursor Pattern:
with get_db_pool().get_connection() as conn:
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    return cursor.fetchone()
# Transaction Pattern:
with get_db_pool().get_connection() as conn:
    cursor = conn.cursor()
    # ... operations
    conn.commit()
    # ... more operations
    conn.commit()
# Buffered Cursor Pattern:
with get_db_pool().get_connection() as conn:
    cursor = conn.cursor(buffered=True)
    cursor.execute("SELECT * FROM large_table")
    for row in cursor:
        process(row)

# ------------------------------ #
    Monitoring (may need tweaking)
# ------------------------------ #

# Check Pool Status
# monitor the connection pool status at any time
status = get_db_pool().get_pool_status()
print(status)
# Output:
# {
#     'pool_size': 5,
#     'checked_in_connections': 3,
#     'checked_out_connections': 2,
#     'overflow_connections': 0,
#     'total_connections': 5
# }
# If 'overflow_connections' consistently > 0, consider increasing pool_size

# Add Health Check Endpoint 
@app.route('/health/db-pool')
def health_db_pool():
    """Monitor connection pool health"""
    try:
        status = get_db_pool().get_pool_status()
        
        # Alert if overflow is frequently used
        overflow_pct = (status['overflow_connections'] / 25) * 100 if status['overflow_connections'] else 0
        
        return jsonify({
            'status': 'ok',
            'pool': status,
            'overflow_usage': f"{overflow_pct:.1f}%",
            'recommendation': 'increase_pool_size' if overflow_pct > 50 else 'optimal'
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 503

# Application Logs - Log These Events:
import logging

logger = logging.getLogger(__name__)

# Log pool status periodically
if overflow_connections > 5:
    logger.warning(f"High overflow usage: {overflow_connections}/25")

# Log timeouts
if connection_timeout:
    logger.error("Connection timeout - consider increasing pool_size")       

# ------------------------------ #
    External Resources
# ------------------------------ #

# SQLAlchemy Core Documentation:
    https://docs.sqlalchemy.org/en/20/core/
# Connection Pooling Guide: 
    https://docs.sqlalchemy.org/en/20/core/pooling.html
# PyMySQL Documentation: 
    https://pymysql.readthedocs.io/
# AWS RDS Best Practices: 
    https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_BestPractices.html
