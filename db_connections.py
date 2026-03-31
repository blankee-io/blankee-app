"""
Database Connection Pooling Module

This module provides context-managed database connections using SQLAlchemy's
connection pooling. It replaces manual connection management with automatic
resource cleanup and connection reuse.

Usage:
    # Simple query (auto-cleanup):
    with get_db_pool().get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        result = cursor.fetchone()
    
    # Write operation with auto-commit:
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM table WHERE id = %s", (id,))
    
    # Advanced usage (dictionary cursor, multiple operations):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users")
        users = cursor.fetchall()
        conn.commit()
"""

import os
from contextlib import contextmanager
from urllib.parse import quote_plus
from sqlalchemy import create_engine, pool, event
from sqlalchemy.pool import QueuePool
import pymysql.cursors
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

# Global pool instance
_db_pool = None


class DatabaseConnectionPool:
    """
    Manages a connection pool for MySQL database using SQLAlchemy Core.
    
    Features:
    - Connection pooling with configurable size
    - Automatic connection health checking (pool_pre_ping)
    - Connection recycling to prevent stale connections
    - Context managers for automatic resource cleanup
    """
    
    def __init__(
        self,
        host,
        user,
        password,
        database,
        pool_size=5,
        max_overflow=25,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True
    ):
        """
        Initialize the connection pool.
        
        Args:
            host: Database host
            user: Database user
            password: Database password
            database: Database name
            pool_size: Number of persistent connections (default: 5)
            max_overflow: Additional burst connections (default: 25, max total: 30)
            pool_timeout: Timeout in seconds for getting connection (default: 30)
            pool_recycle: Recycle connections after N seconds (default: 1800 = 30 min)
            pool_pre_ping: Health check connections before use (default: True)
        """
        # URL-encode username and password to handle special characters
        encoded_user = quote_plus(user)
        encoded_password = quote_plus(password)
        
        connection_string = (
            f"mysql+pymysql://{encoded_user}:{encoded_password}@{host}/{database}"
            f"?charset=utf8mb4"
        )
        
        self.engine = create_engine(
            connection_string,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
            isolation_level="READ COMMITTED",
            echo=False  # Set to True for SQL debugging
        )
        
        # Log pool creation
        log_info(logger, 'DB',  f"Database pool initialized: pool_size={pool_size}, " f"max_overflow={max_overflow}, pool_recycle={pool_recycle}s" )
        
        # Set up connection event listeners for monitoring
        @event.listens_for(self.engine, "connect")
        def receive_connect(dbapi_conn, connection_record):
            log_info(logger, 'DB', "New database connection created")
        
        @event.listens_for(self.engine, "checkout")
        def receive_checkout(dbapi_conn, connection_record, connection_proxy):
            log_info(logger, 'DB', "Connection checked out from pool")
    
    @contextmanager
    def get_cursor(self, commit=False, dictionary=False, buffered=False):
        """
        Context manager for automatic cursor and connection management.
        
        This is the preferred method for most queries. It automatically:
        - Gets a connection from the pool
        - Creates a cursor
        - Commits if commit=True
        - Rolls back on exception
        - Closes cursor and returns connection to pool
        
        Args:
            commit: If True, automatically commit on success (default: False)
            dictionary: If True, return rows as dictionaries (default: False)
            buffered: If True, use buffered cursor (default: False - Note: PyMySQL doesn't use buffered cursors)
        
        Yields:
            cursor: Database cursor object
        
        Example:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute("UPDATE users SET name = %s WHERE id = %s", (name, id))
        """
        raw_conn = self.engine.raw_connection()
        try:
            # Create cursor with specified options
            # PyMySQL uses different cursor classes for different result formats
            if dictionary:
                cursor = raw_conn.cursor(pymysql.cursors.DictCursor)
            else:
                cursor = raw_conn.cursor()
            
            try:
                yield cursor
                if commit:
                    raw_conn.commit()
            except Exception as e:
                raw_conn.rollback()
                log_error(logger, 'DB', f"Database error, rolling back: {e}")
                raise
            finally:
                cursor.close()
        finally:
            raw_conn.close()  # Returns connection to pool
    
    @contextmanager
    def get_connection(self):
        """
        Context manager for advanced connection management.
        
        Use this when you need:
        - Multiple cursors
        - Manual transaction control (multiple commits)
        - Custom cursor configurations
        
        The connection is automatically returned to the pool on exit.
        Exceptions trigger automatic rollback.
        
        Yields:
            connection: Raw database connection
        
        Example:
            with get_db_pool().get_connection() as conn:
                # For dictionary results, use DictCursor
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("SELECT * FROM users")
                users = cursor.fetchall()
                cursor.close()
                conn.commit()
        """
        raw_conn = self.engine.raw_connection()
        try:
            yield raw_conn
        except Exception as e:
            raw_conn.rollback()
            log_error(logger, 'DB', f"Database error in connection context: {e}")
            raise
        finally:
            raw_conn.close()  # Returns connection to pool
    
    def get_pool_status(self):
        """
        Get current connection pool statistics.
        
        Returns:
            dict: Pool status information including size, checked out connections, etc.
        """
        pool = self.engine.pool
        return {
            'pool_size': pool.size(),
            'checked_in_connections': pool.checkedin(),
            'checked_out_connections': pool.checkedout(),
            'overflow_connections': pool.overflow(),
            'total_connections': pool.size() + pool.overflow()
        }
    
    def dispose(self):
        """
        Dispose of the connection pool.
        
        Call this on application shutdown to cleanly close all connections.
        """
        log_info(logger, 'DB', "Disposing database connection pool")
        self.engine.dispose()


def init_db_pool():
    """
    Initialize the global database connection pool.
    
    Should be called once at application startup.
    Reads configuration from environment variables.
    """
    global _db_pool
    
    if _db_pool is not None:
        log_warning(logger, 'DB', "Database pool already initialized, skipping")
        return
    
    try:
        _db_pool = DatabaseConnectionPool(
            host=os.environ["DB_HOST"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_NAME"],
            pool_size=5,
            max_overflow=25,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True
        )
        log_info(logger, 'DB', "Database connection pool initialized successfully")
    except KeyError as e:
        log_error(logger, 'DB', f"Missing required environment variable: {e}")
        raise
    except Exception as e:
        log_error(logger, 'DB', f"Failed to initialize database pool: {e}")
        raise


def get_db_pool():
    """
    Get the global database connection pool instance.
    
    Returns:
        DatabaseConnectionPool: The global pool instance
    
    Raises:
        RuntimeError: If pool has not been initialized
    """
    if _db_pool is None:
        raise RuntimeError(
            "Database pool not initialized. Call init_db_pool() first."
        )
    return _db_pool


def dispose_db_pool():
    """
    Dispose of the global database connection pool.
    
    Should be called on application shutdown.
    """
    global _db_pool
    if _db_pool is not None:
        _db_pool.dispose()
        _db_pool = None
        log_info(logger, 'DB', "Database connection pool disposed")
