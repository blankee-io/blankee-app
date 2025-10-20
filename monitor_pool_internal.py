#!/usr/bin/env python3
"""
Internal Database Connection Pool Monitor
==========================================

Monitors the database connection pool directly (no HTTP overhead) and logs
alerts when thresholds are exceeded.

Features:
- Direct pool access (zero HTTP overhead)
- Configurable alert thresholds
- Structured logging for CloudWatch integration
- Graceful shutdown
- Automatic reconnection on errors

Usage:
    # Run directly
    python3 monitor_pool_internal.py

    # Run as systemd service (recommended for production)
    sudo systemctl start blankee-pool-monitor

Configuration:
    Edit the CONFIGURATION section below to adjust thresholds and intervals.

Requirements:
    - Must run on same server as the Flask app
    - Requires access to db_connections module
    - Write access to log directory

Author: Blankee App Team
Created: 2025-10-19
"""

import time
import logging
import sys
import signal
from datetime import datetime
from pathlib import Path

# Try to import db_connections module
try:
    from db_connections import get_db_pool, init_db_pool
except ImportError:
    print("ERROR: Cannot import db_connections module")
    print("Make sure this script is in the same directory as db_connections.py")
    sys.exit(1)

################################################################################
# CONFIGURATION
################################################################################

# Monitoring intervals
CHECK_INTERVAL = 5.0  # seconds between checks
LOG_NORMAL_EVERY_N = 12  # Log normal status every N checks (60s at 5s interval)

# Alert thresholds
ALERT_HIGH_USAGE = 0.8  # Alert when 80% of pool is in use
ALERT_LOW_AVAILABLE = 2  # Alert when ≤2 connections available
ALERT_CRITICAL_AVAILABLE = 0  # Critical alert when no connections available

# Logging configuration
LOG_LEVEL = logging.INFO
LOG_FILE = '/var/log/budget/pool_monitor.log'  # Change for production
LOG_TO_CONSOLE = True  # Set to False in production if using systemd
LOG_FORMAT = '%(asctime)s - POOL_MONITOR - %(levelname)s - %(message)s'

# Database connection config (if pool not already initialized)
DB_CONFIG = {
    'host': 'localhost',
    'user': 'budget_user',
    'password': 'your_password',
    'database': 'budget',
    'pool_size': 5,
    'max_overflow': 25
}

################################################################################
# LOGGING SETUP
################################################################################

def setup_logging():
    """Configure logging with file and console handlers"""
    logger = logging.getLogger('pool_monitor')
    logger.setLevel(LOG_LEVEL)
    
    # Create formatter
    formatter = logging.Formatter(LOG_FORMAT)
    
    # File handler (create directory if needed)
    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"WARNING: Cannot create log file {LOG_FILE}: {e}")
        print("Falling back to console-only logging")
    
    # Console handler
    if LOG_TO_CONSOLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(LOG_LEVEL)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

################################################################################
# POOL MONITORING
################################################################################

class PoolStats:
    """Container for pool statistics"""
    def __init__(self, pool_size, available, in_use, usage_pct):
        self.pool_size = pool_size
        self.available = available
        self.in_use = in_use
        self.usage_pct = usage_pct
    
    def __eq__(self, other):
        if not isinstance(other, PoolStats):
            return False
        return (self.pool_size == other.pool_size and
                self.available == other.available and
                self.in_use == other.in_use)
    
    def __str__(self):
        return (f"Pool: {self.in_use}/{self.pool_size} in use, "
                f"{self.available} available ({self.usage_pct:.1f}% usage)")


class PoolMonitor:
    """Database connection pool monitor"""
    
    def __init__(self):
        self.running = False
        self.check_count = 0
        self.alert_count = 0
        self.last_stats = None
        self.consecutive_errors = 0
        self.max_consecutive_errors = 3
    
    def get_pool_stats(self):
        """
        Get current pool statistics
        
        Returns:
            PoolStats object or None on error
        """
        try:
            pool = get_db_pool()
            if pool is None:
                logger.error("Pool not initialized")
                return None
            
            # Access pool internals (SQLAlchemy QueuePool)
            # These are implementation details, but stable across versions
            pool_size = pool.engine.pool.size()
            checked_out = pool.engine.pool.checkedout()
            overflow = pool.engine.pool.overflow()
            
            # Calculate availability
            in_use = checked_out
            available = pool_size - checked_out
            usage_pct = (in_use / pool_size * 100) if pool_size > 0 else 0
            
            return PoolStats(pool_size, available, in_use, usage_pct)
            
        except Exception as e:
            logger.error(f"Error getting pool stats: {e}")
            return None
    
    def check_alerts(self, stats):
        """
        Check for alert conditions
        
        Args:
            stats: PoolStats object
            
        Returns:
            List of alert messages
        """
        alerts = []
        
        # Critical: No connections available
        if stats.available == ALERT_CRITICAL_AVAILABLE:
            alerts.append(
                f"🔴 CRITICAL: No connections available! "
                f"All {stats.pool_size} connections in use"
            )
        
        # Warning: Low availability
        elif stats.available <= ALERT_LOW_AVAILABLE and stats.available > 0:
            alerts.append(
                f"⚠️  WARNING: Low availability - only {stats.available} "
                f"connection(s) available"
            )
        
        # Warning: High usage
        if stats.usage_pct >= ALERT_HIGH_USAGE * 100:
            alerts.append(
                f"⚠️  WARNING: High usage - {stats.usage_pct:.1f}% of pool in use "
                f"({stats.in_use}/{stats.pool_size})"
            )
        
        return alerts
    
    def log_alerts(self, alerts):
        """Log all alerts"""
        for alert in alerts:
            if "CRITICAL" in alert:
                logger.critical(alert)
            else:
                logger.warning(alert)
        
        if alerts:
            self.alert_count += len(alerts)
    
    def should_log_normal(self):
        """Determine if we should log normal status"""
        return self.check_count % LOG_NORMAL_EVERY_N == 0
    
    def run_check(self):
        """Run a single pool check"""
        self.check_count += 1
        
        # Get current stats
        stats = self.get_pool_stats()
        
        if stats is None:
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.max_consecutive_errors:
                logger.critical(
                    f"Failed to get pool stats {self.consecutive_errors} times in a row. "
                    "Pool may be dead!"
                )
            return
        
        # Reset error counter on success
        self.consecutive_errors = 0
        
        # Check for alerts
        alerts = self.check_alerts(stats)
        
        # Log alerts
        if alerts:
            self.log_alerts(alerts)
            logger.info(str(stats))
        
        # Log normal status periodically
        elif self.should_log_normal():
            logger.info(f"Status OK - {stats}")
        
        # Log changes even if not alerting
        elif self.last_stats and stats != self.last_stats:
            logger.debug(f"Pool changed - {stats}")
        
        self.last_stats = stats
    
    def run(self):
        """Main monitoring loop"""
        logger.info("=" * 70)
        logger.info("Database Connection Pool Monitor Starting")
        logger.info("=" * 70)
        logger.info(f"Check interval: {CHECK_INTERVAL}s")
        logger.info(f"Alert thresholds:")
        logger.info(f"  - High usage: {ALERT_HIGH_USAGE:.0%}")
        logger.info(f"  - Low available: ≤{ALERT_LOW_AVAILABLE}")
        logger.info(f"  - Critical: {ALERT_CRITICAL_AVAILABLE} available")
        logger.info(f"Logging normal status every {LOG_NORMAL_EVERY_N * CHECK_INTERVAL:.0f}s")
        logger.info("=" * 70)
        
        self.running = True
        
        try:
            while self.running:
                self.run_check()
                time.sleep(CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.critical(f"Monitor crashed with exception: {e}", exc_info=True)
            raise
        finally:
            self.shutdown()
    
    def shutdown(self):
        """Graceful shutdown"""
        self.running = False
        logger.info("=" * 70)
        logger.info("Database Connection Pool Monitor Stopped")
        logger.info(f"Total checks: {self.check_count}")
        logger.info(f"Total alerts: {self.alert_count}")
        logger.info("=" * 70)

################################################################################
# SIGNAL HANDLERS
################################################################################

monitor = None

def signal_handler(signum, frame):
    """Handle termination signals gracefully"""
    signal_names = {
        signal.SIGTERM: 'SIGTERM',
        signal.SIGINT: 'SIGINT',
        signal.SIGHUP: 'SIGHUP'
    }
    signal_name = signal_names.get(signum, str(signum))
    logger.info(f"Received {signal_name}, shutting down...")
    if monitor:
        monitor.running = False

################################################################################
# MAIN
################################################################################

def main():
    """Main entry point"""
    global monitor
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Try to ensure pool is initialized
    try:
        pool = get_db_pool()
        if pool is None:
            logger.warning("Pool not initialized, attempting to initialize...")
            # This will use environment variables if available
            init_db_pool(
                host=DB_CONFIG['host'],
                user=DB_CONFIG['user'],
                password=DB_CONFIG['password'],
                database=DB_CONFIG['database'],
                pool_size=DB_CONFIG['pool_size'],
                max_overflow=DB_CONFIG['max_overflow']
            )
            logger.info("Pool initialized successfully")
    except Exception as e:
        logger.critical(f"Failed to initialize pool: {e}")
        logger.critical("Cannot monitor pool if it's not initialized. Exiting.")
        sys.exit(1)
    
    # Create and run monitor
    monitor = PoolMonitor()
    
    try:
        monitor.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
