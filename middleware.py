"""
Flask Middleware and Decorators for Redis Integration

This module provides Flask-specific utilities for automatic Redis hydration:
1. Before-request handler to track user activity
2. Before-request guard to force incomplete profiles to setup_profile
3. Route decorator for views that require cached data
4. API endpoint to check hydration status
"""

import json
import time
from functools import wraps
from flask import request, session, jsonify, g, current_app, redirect, url_for
from werkzeug.exceptions import HTTPException
from flask_login import current_user
from log_config import generate_request_id, get_logger, log_info, log_error, log_warning, log_exception
from redis_manager import (
    track_user_activity,
    is_user_hydrated,
    get_cached_data,
    invalidate_user_cache
)

logger = get_logger(__name__)


def init_redis_middleware(app):
    """
    Initialize Redis middleware with Flask app.
    Registers before_request handler to track all user activity.
    
    Args:
        app: Flask application instance
    """
    
    @app.before_request
    def set_request_context():
        """Set request_id, user_id, and start time on Flask g for structured logging."""
        g.request_id = generate_request_id()
        g.log_user_id = current_user.id if current_user.is_authenticated else None
        g.request_start_time = time.time()
    
    @app.before_request
    def track_activity():
        """
        Track user activity before each request.
        This automatically triggers hydration if needed and waits for it to complete.
        Also checks if nightly sync ran and triggers recalculation if needed.
        """
        # Only track authenticated users
        if current_user.is_authenticated:
            try:
                user_id = current_user.id
                
                # Skip hydration/activity tracking for lightweight poll endpoints
                # These should NOT keep the user hydrated — they just read a Redis key
                skip_activity_paths = ('/api/data-version',)
                if request.path in skip_activity_paths:
                    g.redis_hydrated = is_user_hydrated(user_id)
                    return
                
                # Track activity (triggers hydration if needed)
                track_user_activity(user_id)
                
                # If not hydrated, wait for hydration to complete before processing request
                # This ensures data is available for immediate operations after dehydration
                if not is_user_hydrated(user_id):
                    log_info(logger, 'MIDDLEWARE', f"User {user_id} not hydrated, waiting for hydration...")
                    
                    # Wait up to 5 seconds for hydration to complete
                    max_wait = 5.0
                    wait_interval = 0.1
                    elapsed = 0.0
                    
                    while elapsed < max_wait:
                        time.sleep(wait_interval)
                        elapsed += wait_interval
                        
                        if is_user_hydrated(user_id):
                            log_info(logger, 'MIDDLEWARE', f"User {user_id} hydration complete after {elapsed:.2f}s")
                            break
                    
                    if not is_user_hydrated(user_id):
                        log_warning(logger, 'MIDDLEWARE', f"User {user_id} hydration timeout after {elapsed:.2f}s, proceeding with MySQL fallback")
                
                # Store hydration status in g for use in templates
                g.redis_hydrated = is_user_hydrated(user_id)
                
            except Exception as e:
                log_error(logger, 'MIDDLEWARE', f"Error tracking user activity: {e}")
                g.redis_hydrated = False
        else:
            g.redis_hydrated = False
    
    @app.before_request
    def force_setup_profile():
        """
        Force users who haven't completed profile setup to /setup_profile.
        A user is considered incomplete if member_since is NULL.
        Whitelists setup-related endpoints so the setup flow works.
        """
        if not current_user.is_authenticated:
            return
        
        # Paths that are always allowed (setup flow, auth, static, API)
        allowed_prefixes = (
            '/setup_profile',
            '/complete_profile_setup',
            '/save_setup_step',
            '/verify_mfa_setup',
            '/enable_mfa',
            '/cancel_mfa',
            '/check_has_categories',
            '/check_handle',
            '/save_setup_name',
            '/bank/',
            '/static/',
            '/api/data-version',
            '/logout',
            '/login',
            '/register',
            '/login_mfa',
            '/verify_email',
            '/forgot_password',
            '/reset_password',
            # Clearing an account sets member_since back to NULL, so a second
            # attempt would otherwise be bounced into setup before it reached
            # the route - and silently do nothing.
            '/clear-account',
        )
        
        if any(request.path.startswith(p) for p in allowed_prefixes):
            return
        
        # Check member_since — if NULL, setup isn't complete
        try:
            from app import init_redis
            r = init_redis()
            user_id = current_user.id
            redis_key = f"users:v1:{user_id}"
            
            member_since = None
            
            # Try Redis first
            if r and current_app.config.get('REDIS_OK'):
                try:
                    cached = r.get(redis_key)
                    if cached:
                        user_data = json.loads(cached)
                        member_since = user_data.get('member_since')
                except Exception:
                    pass
            
            # Fallback to MySQL if Redis didn't have it
            if member_since is None:
                try:
                    from db_connections import get_db_pool
                    with get_db_pool().get_cursor(dictionary=True) as cursor:
                        cursor.execute(
                            "SELECT member_since FROM users WHERE id = %s",
                            (user_id,)
                        )
                        row = cursor.fetchone()
                        if row:
                            member_since = row.get('member_since')
                except Exception as db_err:
                    log_error(logger, 'MIDDLEWARE', f"[force_setup_profile] MySQL fallback error: {db_err}")
            
            if not member_since:
                log_info(logger, 'MIDDLEWARE', f"[force_setup_profile] User {user_id} has no member_since (value={member_since!r}), path={request.path}, redirecting to /setup_profile")
                return redirect('/setup_profile')
        
        except Exception as e:
            log_error(logger, 'MIDDLEWARE', f"[force_setup_profile] Error checking member_since: {e}")
            # Don't block on errors — let the request through
    
    @app.after_request
    def log_request_response(response):
        """Log every request with method, path, status, and duration."""
        # Skip static files to reduce noise
        if request.path.startswith('/static/'):
            return response
        duration_ms = None
        start = getattr(g, 'request_start_time', None)
        if start is not None:
            duration_ms = round((time.time() - start) * 1000, 1)
        log_info(logger, 'REQUEST', f"{request.method} {request.path} {response.status_code}",
                 status=response.status_code, duration_ms=duration_ms)
        return response

    @app.errorhandler(Exception)
    def handle_unhandled_exception(e):
        """
        Log genuinely unhandled exceptions as structured JSON and return a 500.

        HTTPException subclasses are returned untouched. They are not failures -
        they carry their own status code, and Flask raises them as the normal way
        of expressing 404/403/405/401. Before this check existed, this handler
        caught them too and rewrote every one as a 500, so a merely missing file
        was reported as an internal server error (and logged at ERROR level).
        """
        if isinstance(e, HTTPException):
            return e
        log_exception(logger, 'UNHANDLED', f"{request.method} {request.path} raised {type(e).__name__}: {e}")
        return jsonify({'error': 'Internal server error'}), 500

    @app.after_request
    def bump_data_version_on_mutation(response):
        """
        Auto-bump data_version after any successful POST/PUT/DELETE mutation.
        This enables cross-tab/cross-browser stale-data detection.
        Skips read-only endpoints and non-2xx responses.
        """
        if not current_user.is_authenticated:
            return response
        if request.method not in ('POST', 'PUT', 'DELETE'):
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        # Skip polling/read endpoints that happen to use POST
        # Also skip setup-flow bank endpoints that do not represent user data mutations
        skip_paths = (
            '/api/data-version', '/health/',
            '/bank/analyze-transactions-for-categories',
        )
        if any(request.path.startswith(p) for p in skip_paths):
            return response
        try:
            from app import _bump_data_version
            _bump_data_version(current_user.id)
        except Exception:
            pass
        return response

    log_info(logger, 'MIDDLEWARE', "Redis middleware initialized")


def require_hydration(fallback_to_mysql=True):
    """
    Decorator for routes that should prefer Redis cached data.
    
    Args:
        fallback_to_mysql: If True, allow fallback to MySQL when Redis isn't hydrated
                          If False, return 503 until hydration completes
    
    Usage:
        @app.route('/dashboard')
        @login_required
        @require_hydration(fallback_to_mysql=True)
        def dashboard():
            # This route will use cached data if available
            # or fallback to MySQL if not hydrated yet
            pass
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return f(*args, **kwargs)
            
            user_id = current_user.id
            
            if not is_user_hydrated(user_id):
                if fallback_to_mysql:
                    # Log that we're falling back to MySQL
                    log_info(logger, 'MIDDLEWARE', f"User {user_id} not hydrated, falling back to MySQL")
                    g.using_mysql_fallback = True
                else:
                    # Return loading state
                    return jsonify({
                        'status': 'hydrating',
                        'message': 'Data is loading, please wait...'
                    }), 503
            else:
                g.using_mysql_fallback = False
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def init_redis_routes(app):
    """
    Initialize Redis-related API routes.
    
    Args:
        app: Flask application instance
    """
    
    @app.route('/api/redis/status', methods=['GET'])
    def redis_status():
        """
        Check Redis hydration status for current user.
        
        Returns:
            JSON with hydration status
        """
        if not current_user.is_authenticated:
            return jsonify({
                'authenticated': False,
                'hydrated': False
            }), 401
        
        user_id = current_user.id
        hydrated = is_user_hydrated(user_id)
        
        return jsonify({
            'authenticated': True,
            'user_id': user_id,
            'hydrated': hydrated,
            'timestamp': int(time.time())
        }), 200
    
    
    @app.route('/api/redis/invalidate', methods=['POST'])
    def redis_invalidate():
        """
        Manually invalidate Redis cache for current user.
        Useful for forcing a fresh reload from MySQL.
        
        Returns:
            JSON confirmation
        """
        if not current_user.is_authenticated:
            return jsonify({
                'success': False,
                'error': 'Not authenticated'
            }), 401
        
        try:
            user_id = current_user.id
            invalidate_user_cache(user_id)
            
            return jsonify({
                'success': True,
                'message': f'Cache invalidated for user {user_id}'
            }), 200
            
        except Exception as e:
            log_error(logger, 'MIDDLEWARE', f"Error invalidating cache: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    
    @app.route('/api/redis/refresh-check', methods=['GET'])
    def redis_refresh_check():
        """
        Check if frontend should refresh due to hydration completion.
        This endpoint is polled by the frontend.
        
        Returns:
            JSON indicating if refresh is needed
        """
        if not current_user.is_authenticated:
            return jsonify({
                'refresh_needed': False
            }), 200
        
        try:
            user_id = current_user.id
            
            # Check if refresh flag is set
            from redis_manager import _redis_client
            if _redis_client:
                flag_key = f"user:{user_id}:refresh_needed"
                refresh_needed = bool(_redis_client.get(flag_key))
                
                # Clear the flag
                if refresh_needed:
                    _redis_client.delete(flag_key)
                
                return jsonify({
                    'refresh_needed': refresh_needed,
                    'hydrated': is_user_hydrated(user_id)
                }), 200
            else:
                return jsonify({
                    'refresh_needed': False,
                    'hydrated': False
                }), 200
                
        except Exception as e:
            log_error(logger, 'MIDDLEWARE', f"Error checking refresh status: {e}")
            return jsonify({
                'refresh_needed': False
            }), 200


# Helper function for templates
def get_cached_or_query(table: str, user_id: int, fallback_query_func):
    """
    Get data from Redis cache or fallback to MySQL query.
    
    Args:
        table: Table name
        user_id: User ID
        fallback_query_func: Function that queries MySQL if cache miss
                            Should return list of dictionaries
    
    Returns:
        List of dictionaries (rows)
        
    Example:
        def query_income():
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute("SELECT * FROM income_entries WHERE user_id = %s", (user_id,))
                return cursor.fetchall()
        
        income_data = get_cached_or_query('income_entries', user_id, query_income)
    """
    # Try cache first
    cached_data = get_cached_data(table, user_id)
    
    if cached_data is not None:
        log_info(logger, 'MIDDLEWARE', f"Cache HIT for {table}, user {user_id}")
        return cached_data
    
    # Cache miss - query MySQL
    log_info(logger, 'MIDDLEWARE', f"Cache MISS for {table}, user {user_id}")
    data = fallback_query_func()
    
    # Optionally cache the result for next time
    # (This is already handled by hydration, but could help with partial hydration)
    
    return data


