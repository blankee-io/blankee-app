#!/usr/bin/env python3
"""
Quiltt Connection Status Checker
Runs via cron every 6 hours to catch connection errors that webhooks might have missed.

Cron entry (add to server):
    0 */6 * * * cd /var/www/html/budget && /usr/bin/python3 quiltt_connection_checker.py >> /var/log/apache2/quiltt_checker.log 2>&1

What it does:
1. Gets all users with quiltt_enabled = 1
2. For each user, queries Quiltt API for their connections
3. If status is ERROR_REPAIRABLE or DISCONNECTED:
   - Updates our stored connection status (MySQL + Redis if hydrated)
   - Creates notification with auto-reconnect link (if not already notified recently)
4. Logs results
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables from .env file (check multiple locations)
from dotenv import load_dotenv

# Try dev server path first, then prod path
env_paths = ['/var/www/budget_env/.env', '/var/www/blankee/.env']
for env_path in env_paths:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        break

import mysql.connector
from mysql.connector import pooling
import redis

# Configure logging to file
LOG_FILE = '/var/log/apache2/quiltt_checker.log'
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()  # Also print to console when run manually
    ]
)
logger = logging.getLogger(__name__)

# Import after dotenv is loaded
from quiltt_utils import QuilttClient

# Redis configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_KEY_VERSION = 'v1'
INACTIVITY_TIMEOUT = 604800  # 7 days

# Initialize Redis client
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types"""
    def default(self, obj):
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return super().default(obj)


def get_db_connection():
    """Get a direct MySQL connection"""
    return mysql.connector.connect(
        host='localhost',
        user='ms_admin',
        password='dune6MEANTIME.ching_reek',
        database='budget'
    )


def is_user_hydrated(user_id):
    """Check if user has data in Redis"""
    try:
        # Check for any common key that would indicate hydration
        key = f"income_entries:{REDIS_KEY_VERSION}:{user_id}"
        return redis_client.exists(key) > 0
    except Exception as e:
        logger.warning(f"Error checking hydration for user {user_id}: {e}")
        return False


def get_redis_key(table, user_id):
    """Get Redis key for a table"""
    return f"{table}:{REDIS_KEY_VERSION}:{user_id}"


def get_quiltt_enabled_users(cursor):
    """Get all users with Quiltt enabled"""
    cursor.execute("""
        SELECT u.id, u.first_name, u.email, qp.profile_id, qp.session_token
        FROM users u
        JOIN quiltt_profiles qp ON u.id = qp.user_id
        WHERE u.quiltt_enabled = 1
    """)
    return cursor.fetchall()


def get_user_connections(cursor, user_id):
    """Get stored connections for a user"""
    cursor.execute("""
        SELECT connection_id, institution_name, status
        FROM quiltt_connections
        WHERE user_id = %s
    """, (user_id,))
    return cursor.fetchall()


def get_existing_reconnect_notification(cursor, user_id, connection_id):
    """Get existing unread notification for this connection (regardless of age)"""
    cursor.execute("""
        SELECT id FROM notifications
        WHERE user_id = %s 
        AND message LIKE %s
        AND is_read = 0
    """, (user_id, f'%reconnect={connection_id}%'))
    result = cursor.fetchone()
    return result['id'] if result else None


def update_notification_date(cursor, user_id, notification_id):
    """Update an existing notification's date to now"""
    cursor.execute("""
        UPDATE notifications 
        SET date = NOW()
        WHERE id = %s AND user_id = %s
    """, (notification_id, user_id))
    
    # Update Redis if user is hydrated
    if is_user_hydrated(user_id):
        try:
            redis_key = get_redis_key('notifications', user_id)
            cached = redis_client.get(redis_key)
            if cached:
                notifications = json.loads(cached)
                for n in notifications:
                    if n.get('id') == notification_id:
                        n['date'] = datetime.now().isoformat()
                        break
                redis_client.setex(redis_key, INACTIVITY_TIMEOUT, json.dumps(notifications))
        except Exception as e:
            logger.error(f"Error updating notification in Redis: {e}")


def add_notification(cursor, user_id, message):
    """Create a notification for a user (MySQL + Redis if hydrated)"""
    # Insert to MySQL
    cursor.execute("""
        INSERT INTO notifications (user_id, date, message, is_read)
        VALUES (%s, NOW(), %s, 0)
    """, (user_id, message))
    notification_id = cursor.lastrowid
    
    # Update Redis if user is hydrated
    if is_user_hydrated(user_id):
        try:
            redis_key = get_redis_key('notifications', user_id)
            cached = redis_client.get(redis_key)
            notifications = json.loads(cached) if cached else []
            
            # Add new notification
            new_notification = {
                'id': notification_id,
                'user_id': user_id,
                'date': datetime.now().isoformat(),
                'message': message,
                'is_read': 0
            }
            notifications.append(new_notification)
            
            # Save back to Redis
            redis_client.setex(
                redis_key,
                INACTIVITY_TIMEOUT + 60,
                json.dumps(notifications, cls=DecimalEncoder)
            )
            logger.info(f"  Added notification to Redis for user {user_id}")
        except Exception as e:
            logger.warning(f"  Failed to add notification to Redis: {e}")
    
    return notification_id


def update_connection_status(cursor, user_id, connection_id, new_status):
    """Update connection status in database (MySQL + Redis if hydrated)"""
    # Update MySQL
    cursor.execute("""
        UPDATE quiltt_connections 
        SET status = %s, last_synced_at = NOW()
        WHERE user_id = %s AND connection_id = %s
    """, (new_status, user_id, connection_id))
    
    # Update Redis if user is hydrated
    if is_user_hydrated(user_id):
        try:
            redis_key = get_redis_key('quiltt_connections', user_id)
            cached = redis_client.get(redis_key)
            
            if cached:
                connections = json.loads(cached)
                updated = False
                
                for conn in connections:
                    if conn.get('connection_id') == connection_id:
                        conn['status'] = new_status
                        conn['last_synced_at'] = datetime.now().isoformat()
                        updated = True
                        break
                
                if updated:
                    redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(connections, cls=DecimalEncoder)
                    )
                    # Mark as dirty for flush
                    dirty_key = f"dirty_tables:{user_id}"
                    redis_client.sadd(dirty_key, 'quiltt_connections')
                    logger.info(f"  Updated connection status in Redis for user {user_id}")
        except Exception as e:
            logger.warning(f"  Failed to update Redis connection status: {e}")


def refresh_session_if_needed(client, profile_id, session_token):
    """Refresh session token if needed, returns new token or original"""
    # Try to use existing token first - if it fails, we'll refresh
    return session_token


def check_all_connections():
    """Main function to check all Quiltt connections"""
    logger.info("=" * 60)
    logger.info("Starting Quiltt Connection Status Check")
    logger.info("=" * 60)
    
    client = QuilttClient()
    
    if not client.api_key:
        logger.error("QUILTT_API_KEY not configured - exiting")
        return
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        users = get_quiltt_enabled_users(cursor)
        logger.info(f"Found {len(users)} users with Quiltt enabled")
        
        stats = {
            'users_checked': 0,
            'connections_checked': 0,
            'errors_found': 0,
            'notifications_created': 0,
            'already_notified': 0
        }
        
        for user in users:
            user_id = user['id']
            profile_id = user['profile_id']
            session_token = user['session_token']
            first_name = user['first_name'] or 'User'
            
            hydrated = is_user_hydrated(user_id)
            logger.info(f"\nChecking user {user_id} ({first_name})... [Redis hydrated: {hydrated}]")
            stats['users_checked'] += 1
            
            # Get stored connections for this user
            stored_connections = get_user_connections(cursor, user_id)
            
            if not stored_connections:
                logger.info(f"  No connections stored for user {user_id}")
                continue
            
            for stored_conn in stored_connections:
                connection_id = stored_conn['connection_id']
                institution_name = stored_conn['institution_name'] or 'your bank'
                stored_status = stored_conn['status']
                
                stats['connections_checked'] += 1
                
                # Query Quiltt API for current status
                try:
                    live_conn = client.get_connection(session_token, connection_id)
                    
                    if not live_conn:
                        logger.warning(f"  Could not fetch connection {connection_id} from Quiltt API")
                        # Session might be expired - try to refresh
                        token_data = client.refresh_session_token(profile_id)
                        if token_data:
                            session_token = token_data['token']
                            # Convert ISO datetime to MySQL format
                            expires_at = token_data.get('expiresAt')
                            if expires_at:
                                # Parse ISO format and convert to MySQL datetime
                                from datetime import datetime
                                try:
                                    # Handle ISO format like '2026-02-05T08:00:02Z'
                                    expires_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                                    expires_at = expires_dt.strftime('%Y-%m-%d %H:%M:%S')
                                except Exception as e:
                                    logger.error(f"  Error parsing expiresAt '{expires_at}': {e}")
                                    expires_at = None
                            
                            # Update session token in DB
                            cursor.execute("""
                                UPDATE quiltt_profiles 
                                SET session_token = %s, session_expires_at = %s
                                WHERE user_id = %s
                            """, (session_token, expires_at, user_id))
                            conn.commit()
                            logger.info(f"  Refreshed session token for user {user_id}")
                            
                            # Try again with new token
                            live_conn = client.get_connection(session_token, connection_id)
                        
                        if not live_conn:
                            logger.warning(f"  Still could not fetch connection {connection_id} after token refresh")
                            continue
                    
                    live_status = live_conn.get('status', 'UNKNOWN')
                    logger.info(f"  Connection {connection_id} ({institution_name}): stored={stored_status}, live={live_status}")
                    
                    # Check for error states
                    if live_status in ('ERROR_REPAIRABLE', 'DISCONNECTED'):
                        stats['errors_found'] += 1
                        
                        # Update our stored status if different
                        if stored_status != live_status:
                            update_connection_status(cursor, user_id, connection_id, live_status)
                            logger.info(f"  Updated status from {stored_status} to {live_status}")
                        
                        # Check if there's an existing unread notification for this connection
                        existing_notification_id = get_existing_reconnect_notification(cursor, user_id, connection_id)
                        if existing_notification_id:
                            # Update the existing notification's date to now
                            update_notification_date(cursor, user_id, existing_notification_id)
                            logger.info(f"  Updated existing notification #{existing_notification_id} date to now")
                            stats['already_notified'] += 1
                        else:
                            # Create new notification
                            notification_message = f'Your {institution_name} connection needs to be reconnected. <a href="/profile?reconnect={connection_id}" class="notification-link">Click here to reconnect</a>.'
                            add_notification(cursor, user_id, notification_message)
                            stats['notifications_created'] += 1
                            logger.info(f"  Created reconnection notification for user {user_id}")
                    
                    elif live_status == 'SYNCED' and stored_status in ('ERROR_REPAIRABLE', 'DISCONNECTED'):
                        # Connection was fixed externally, update our status
                        update_connection_status(cursor, user_id, connection_id, 'SYNCED')
                        logger.info(f"  Connection recovered - updated status to SYNCED")
                
                except Exception as e:
                    logger.error(f"  Error checking connection {connection_id}: {e}")
                    continue
            
            conn.commit()
        
        # Log summary
        logger.info("\n" + "=" * 60)
        logger.info("Connection Check Complete - Summary:")
        logger.info(f"  Users checked: {stats['users_checked']}")
        logger.info(f"  Connections checked: {stats['connections_checked']}")
        logger.info(f"  Errors found: {stats['errors_found']}")
        logger.info(f"  Notifications created: {stats['notifications_created']}")
        logger.info(f"  Already notified (skipped): {stats['already_notified']}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Error during connection check: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    check_all_connections()
