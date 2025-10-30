import mysql.connector
import re  # Import the regular expression module
import os
import calendar
import pyotp
import qrcode
import io
import base64
import redis
import logging
import json
import time
import pymysql.cursors
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_bcrypt import Bcrypt
from flask import jsonify
from datetime import date, datetime, timedelta
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename
from dateutil.relativedelta import relativedelta
from decimal import Decimal
import threading
from collections import defaultdict
from db_connections import init_db_pool, get_db_pool, dispose_db_pool
from redis_manager import init_redis_manager, shutdown_redis_manager, DecimalEncoder
from middleware import init_redis_middleware, init_redis_routes

app = Flask(__name__)
app.secret_key = 'your_secret_key'
bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)

# Define the upload folder and allowed file extensions
UPLOAD_FOLDER = '/var/www/html/budget/static/uploads'  # Make sure this folder exists
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.logger.setLevel(logging.INFO)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@login_manager.unauthorized_handler
def unauthorized():
    # Redirect unauthorized users to the login page
    return redirect(url_for('login'))

#################################################################################
############################### REDIS INTEGRATION ###############################
#################################################################################

_redis_client = None

def init_redis():
    global _redis_client
    if _redis_client:
        return _redis_client
    try:
        _redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=0.5,
        )
        _redis_client.ping()
        app.config['REDIS_OK'] = True
    except Exception as e:
        app.logger.warning(f"Redis unavailable: {e}")
        app.config['REDIS_OK'] = False
    return _redis_client

def _redis_warmup_on_import():
    try:
        init_redis()
    except Exception:
        pass

_redis_warmup_on_import()

# Initialize database connection pool at startup
try:
    init_db_pool()
    app.logger.info("Database connection pool initialized")
except Exception as e:
    app.logger.error(f"Failed to initialize database pool: {e}")
    raise

# Initialize Redis manager with hydration/dehydration workers
if _redis_client:
    init_redis_manager(_redis_client)
    app.logger.info("Redis manager initialized with hydration/dehydration workers")
    
    # Initialize Redis middleware and routes
    init_redis_middleware(app)
    init_redis_routes(app)
    app.logger.info("Redis middleware and routes initialized")
else:
    app.logger.warning("Redis manager not initialized - Redis unavailable")

@app.route('/health/redis', methods=['GET'])
def health_redis():
    status = {'ok': bool(app.config.get('REDIS_OK'))}
    return jsonify(status), 200 if status['ok'] else 503

@app.route('/health/db-pool', methods=['GET'])
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

# --- Dashboard cache helpers ---
DASHBOARD_CACHE_TTL = int(os.getenv("DASHBOARD_CACHE_TTL", "60"))
# TTL for data that needs to be persisted to MySQL
# MUST be longer than INACTIVITY_TIMEOUT (300s) + flush interval (30s) to prevent premature expiration
# Set to 10 minutes to ensure data survives until dehydration explicitly removes it
PERSISTENT_CACHE_TTL = int(os.getenv("PERSISTENT_CACHE_TTL", "600"))  # 10 minutes (was 5)

#################################################################################
############################### DB INTEGRATION ##################################
#################################################################################

def get_db_connection():
    conn = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"]
    )
    return conn

class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = id
        self.username = username
        self.password = password

    def get_id(self):
        return str(self.id)

    @staticmethod
    def get(user_id):
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT id, username, password FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()

        if user:
            return User(id=user[0], username=user[1], password=user[2])
        return None

#################################################################################
################################### HOME ########################################
#################################################################################

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('dashboard_3m'))
    return redirect(url_for('login'))

#################################################################################
################################### REGISTRATION ################################
#################################################################################

def find_nearest_friday(some_date, round_up=False):
    """
    Finds the nearest Friday to the given date.
    If round_up is True, rounds up to the next Friday if today is not Friday.
    If round_up is False, rounds down to the previous Friday if today is not Friday.
    """
    day_of_week = some_date.weekday()
    
    if round_up:
        # Round up to the next Friday
        days_to_friday = (4 - day_of_week) if day_of_week <= 4 else (11 - day_of_week)
    else:
        # Round down to the previous or same Friday
        days_to_friday = (4 - day_of_week) if day_of_week <= 4 else (4 - day_of_week - 7)
    
    return some_date + timedelta(days=days_to_friday)

@app.route('/update_landing_page', methods=['POST'])
@login_required
def update_landing_page():
    landing_page = request.form.get('landing_page', 'dashboard_3m')
    
    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'landing_page', landing_page)
    
    flash('Landing page updated.')
    return redirect(url_for('settings'))

def create_totals_remainders_for_new_user(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        today = date.today()

        # Create entries for weekly totals (totals_remainders) for 1 year back and up to 3 years forward
        one_year_back = date(today.year - 1, 1, 1)  # Start from January 1st of last year
        three_years_forward = date(today.year + 3, 12, 31)  # End at December 31st, 3 years from now

        # Find the nearest Friday for the start date
        start_date = find_nearest_friday(one_year_back, round_up=False)
        
        # For the end date, we need to check if it falls beyond December 31st and restrict it to that date
        end_date = find_nearest_friday(three_years_forward, round_up=True)
        if end_date > three_years_forward:
            end_date = three_years_forward

        # Create weekly totals (totals_remainders)
        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))

            current_date += timedelta(weeks=1)  # Move to the next Friday

        # Create daily totals (totals_remainders_d)
        current_date = one_year_back
        while current_date <= three_years_forward:
            cursor.execute("""
                INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder, last_day_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))
                    # --- Add this for savings_entries ---
            cursor.execute("""
                INSERT INTO savings_entries (user_id, date, amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE amount = amount
            """, (user_id, current_date, 0.00))
            # --- End savings_entries addition ---

            current_date += timedelta(days=1)  # Move to the next day

        # Create monthly totals (totals_remainders_m)
        # For each month from one_year_back to three_years_forward, find the last day of the month
        current_month = one_year_back.replace(day=1)
        last_month = three_years_forward.replace(day=1)
        while current_month <= last_month:
            # Find the last day of the current month
            year = current_month.year
            month = current_month.month
            if month == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month + 1, 1)
            last_day = next_month - timedelta(days=1)
            if last_day > three_years_forward:
                last_day = three_years_forward
            cursor.execute("""
                INSERT INTO totals_remainders_m (user_id, date, total_income, total_expenses, remainder, last_month_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, last_day, 0.00, 0.00, 0.00, 0.00))
            # Move to the first day of the next month
            if month == 12:
                current_month = date(year + 1, 1, 1)
            else:
                current_month = date(year, month + 1, 1)

        cursor.close()
        conn.commit()

# Modify the register route to include the creation of totals_remainders
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        member_since = request.form['member_since']

        # Regular expression for validating an Email
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

        # Validate email format
        if not re.match(email_regex, username):
            flash('Invalid email format. Please use a valid email address as your username.')
            return redirect(url_for('register'))

        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Check if the username already exists
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            existing_user = cursor.fetchone()

            if existing_user:
                flash('Username already exists. Please choose a different one.')
                cursor.close()
                return redirect(url_for('register'))

            # Hash the password and insert the new user
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cursor.execute(
                "INSERT INTO users (username, password, member_since) VALUES (%s, %s, %s)",
                (username, hashed_password, member_since)
            )
            new_user_id = cursor.lastrowid  # Get the ID of the newly created user
            conn.commit()

            # Create default income and expense categories for the new user
            cursor.execute("""
                INSERT INTO income_categories (user_id, name, display_order, is_recurring, is_auto_adjustment)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_user_id, 'Auto Adjustments', 1, 0, 1))
            cursor.execute("""
                INSERT INTO expense_categories (user_id, name, display_order, is_recurring, is_auto_adjustment)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_user_id, 'Auto Adjustments', 1, 0, 1))
            # --- Add Savings categories ---
            cursor.execute("""
                INSERT INTO income_categories (user_id, name, display_order, is_recurring, is_auto_adjustment)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_user_id, 'Savings', -1, 0, 1))
            cursor.execute("""
                INSERT INTO expense_categories (user_id, name, display_order, is_recurring, is_auto_adjustment)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_user_id, 'Savings', -1, 0, 1))
            cursor.close()
            conn.commit()

        # Log the user in automatically after registration
        user_obj = User(id=new_user_id, username=username, password=hashed_password)
        login_user(user_obj)

        # Create totals_remainders for every Friday for the new user
        create_totals_remainders_for_new_user(new_user_id)

        # Redirect to the setup profile page after login
        return redirect(url_for('setup_profile'))

    return render_template('register.html')


@app.route('/setup_profile', methods=['GET'])
@login_required
def setup_profile():
    # Simply render the setup profile page
    return render_template('setup_profile.html')


@app.route('/complete_profile_setup', methods=['POST'])
@login_required
def complete_profile_setup():
    if request.method == 'POST':
        starting_balance = request.json.get('starting_balance')
        starting_savings = request.json.get('starting_savings')  # <-- get the savings value
        balance_threshold = request.json.get('balance_threshold')
        income_entry_date = request.json.get('income_entry_date')
        currency_type = request.json.get('currency_type', 'USD')  # <-- get the currency type

        print(f"[complete_profile_setup] starting_balance: {starting_balance}")
        print(f"[complete_profile_setup] starting_savings: {starting_savings}")
        print(f"[complete_profile_setup] balance_threshold: {balance_threshold}")
        print(f"[complete_profile_setup] income_entry_date: {income_entry_date}")
        print(f"[complete_profile_setup] currency_type: {currency_type}")

        try:
            starting_savings = float(starting_savings)
        except (TypeError, ValueError):
            starting_savings = 0.0

        if not all([starting_balance, balance_threshold, income_entry_date]):
            return jsonify({'status': 'error', 'message': 'All fields are required'}), 400

        # Update user details in the database
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Update user information (balance_threshold, starting_savings, and currency_type)
            cursor.execute("""
                UPDATE users 
                SET balance_threshold = %s, starting_savings = %s, currency_type = %s
                WHERE id = %s
            """, (balance_threshold, starting_savings, currency_type, current_user.id))

            # Check if the 'Starting Balance' category already exists for this user
            cursor.execute("""
                SELECT id FROM income_categories 
                WHERE name = %s AND user_id = %s
            """, ('Starting Balance', current_user.id))
            category = cursor.fetchone()

            # If the category does not exist, create it
            if not category:
                cursor.execute("""
                    INSERT INTO income_categories (user_id, name, display_order) 
                    VALUES (%s, %s, %s)
                """, (current_user.id, 'Starting Balance', 0))
                starting_balance_category_id = cursor.lastrowid
            else:
                starting_balance_category_id = category[0]

            # Insert the starting balance into income_entries linked to the 'Starting Balance' category
            cursor.execute("""
                INSERT INTO income_entries (category_id, date, amount) 
                VALUES (%s, %s, %s)
            """, (starting_balance_category_id, income_entry_date, starting_balance))

            # --- Insert starting savings into savings_entries on the current date ---
            today_str = date.today().strftime('%Y-%m-%d')
            cursor.execute("SELECT amount FROM savings_entries WHERE user_id = %s AND date = %s", (current_user.id, today_str))
            print("Before update:", cursor.fetchone())

            cursor.execute("""
                UPDATE savings_entries SET amount = %s WHERE user_id = %s AND date = %s
            """, (starting_savings, current_user.id, today_str))

            cursor.execute("SELECT amount FROM savings_entries WHERE user_id = %s AND date = %s", (current_user.id, today_str))
            print("After update:", cursor.fetchone())

            cursor.close()
            conn.commit()

        # Return success response
        return jsonify({'status': 'success'})

    return redirect(url_for('dashboard_3m'))

@app.route('/check_username', methods=['POST'])
def check_username():
    username = request.form['username']

    with get_db_pool().get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        existing_user = cursor.fetchone()

    if existing_user:
        return jsonify({'status': 'taken'})
    else:
        return jsonify({'status': 'available'})

#################################################################################
############################### LOGIN ###########################################
#################################################################################

############################## Login Redis ######################################

def _cache_get_user(user_id):
    """Try to get user profile from Redis cache."""
    if not app.config.get('REDIS_OK'):
        return None
    try:
        r = init_redis()
        key = f"user:v1:{user_id}"
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        app.logger.warning(f"[CACHE][USER] GET error user={user_id}: {e}")
    return None

def _cache_set_user(user_id, user_data):
    """Set user profile in Redis cache."""
    if not app.config.get('REDIS_OK'):
        return
    try:
        key = f"user:v1:{user_id}"
        init_redis().setex(key, DASHBOARD_CACHE_TTL, json.dumps(user_data))
    except Exception as e:
        app.logger.warning(f"[CACHE][USER] SET error user={user_id}: {e}")

def get_user_profile(user_id):
    """Cache-aside: Try cache, then DB, then update cache."""
    cached = _cache_get_user(user_id)
    if cached:
        return cached
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user_data = cursor.fetchone()
    if user_data:
        _cache_set_user(user_id, user_data)
    return user_data

def mark_user_dirty(user_id):
    if app.config.get('REDIS_OK'):
        init_redis().sadd("dirty_users", user_id)

############################## Login Route ######################################

@app.route('/login', methods=['GET', 'POST'])
def login():
    # If the user is already authenticated, redirect to their preferred landing page
    if current_user.is_authenticated:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT landing_page FROM users WHERE id = %s", (current_user.id,))
            landing_page = cursor.fetchone()
        if landing_page and landing_page[0]:
            return redirect(url_for(landing_page[0]))
        else:
            return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember = 'remember' in request.form  # Get the value of the Remember Me checkbox

        # Establish database connection
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, password, landing_page FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()

            # Check if the user exists and if the password is correct
            if user and bcrypt.check_password_hash(user[2], password):
                # Check for MFA using the same connection
                cursor.execute("SELECT mfa_secret FROM users WHERE id = %s", (user[0],))
                mfa_row = cursor.fetchone()
                mfa_secret = mfa_row[0] if mfa_row else None

                if mfa_secret:
                    print(f"[LOGIN] MFA required for user {user[1]} (id={user[0]})")
                    session['pre_mfa_user_id'] = user[0]
                    session['pre_mfa_remember'] = remember
                    cursor.close()
                    return render_template('login.html', mfa_step=True, username=user[1])
                else:
                    print(f"[LOGIN] No MFA required for user {user[1]} (id={user[0]})")
                    user_obj = User(id=user[0], username=user[1], password=user[2])
                    login_user(user_obj, remember=remember)

                    # Continue with the rest of your logic using the same connection
                    # Check the most recent year in totals_remainders for this user
                    cursor.execute("SELECT MAX(date) FROM totals_remainders WHERE user_id = %s", (user_obj.id,))
                    last_weekly_date = cursor.fetchone()[0]

                    cursor.execute("SELECT MAX(date) FROM totals_remainders_d WHERE user_id = %s", (user_obj.id,))
                    last_daily_date = cursor.fetchone()[0]

                cursor.execute("SELECT MAX(date) FROM totals_remainders_m WHERE user_id = %s", (user_obj.id,))
                last_monthly_date = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT MAX(date) FROM c_a_balances WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                """, (user_obj.id,))
                last_ca_weekly_date = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT MAX(date) FROM c_a_balances_d WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                """, (user_obj.id,))
                last_ca_daily_date = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT MAX(date) FROM c_a_balances_m WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                """, (user_obj.id,))
                last_ca_monthly_date = cursor.fetchone()[0]

                cutoff_date = date.today().replace(month=12, day=31, year=date.today().year + 3)
                year_3 = date.today().year + 3

                # 1. Check income categories with no_end_date = 1
                cursor.execute("""
                    SELECT ic.id AS category_id, ri.id AS recurring_id
                    FROM income_categories ic
                    JOIN recurring_income ri ON ri.category_id = ic.id
                    WHERE ic.user_id = %s AND ic.no_end_date = 1
                """, (user_obj.id,))
                income_cats = cursor.fetchall()

                for cat in income_cats:
                    category_id = cat[0]
                    recurring_id = cat[1]
                    cursor.execute("""
                        SELECT 1 FROM income_entries 
                        WHERE category_id = %s AND YEAR(date) = %s LIMIT 1
                    """, (category_id, year_3))
                    entry_exists = cursor.fetchone()
                    if not entry_exists:
                        cursor.execute("""
                            SELECT ic.name, ri.amount, ri.cadence_interval, ri.cadence_unit, ri.start_date, ri.weekdays, ri.monthly_days, ri.yearly_day, ri.yearly_month
                            FROM recurring_income ri
                            JOIN income_categories ic ON ri.category_id = ic.id
                            WHERE ri.id = %s
                        """, (recurring_id,))
                        rec = cursor.fetchone()
                        if rec:
                            data = {
                                'recurring_id': recurring_id,
                                'category_name': rec[0],
                                'amount': rec[1],
                                'cadence_interval': rec[2],
                                'cadence_unit': rec[3],
                                'start_date': date.today().strftime('%Y-%m-%d'),
                                'end_date': date(year_3, 12, 31).strftime('%Y-%m-%d'),
                                'weekdays': rec[5].split(',') if rec[5] else [],
                                'monthly_days': [int(x) for x in rec[6].split(',')] if rec[6] else [],
                                'yearly_day': rec[7],
                                'yearly_month': rec[8],
                                'no_end_date': 1
                            }
                            with app.test_request_context():
                                update_recurring_income_inner(data, user_obj.id)

                # 2. Check expense categories with no_end_date = 1
                cursor.execute("""
                    SELECT ec.id AS category_id, re.id AS recurring_id
                    FROM expense_categories ec
                    JOIN recurring_expense re ON re.category_id = ec.id
                    WHERE ec.user_id = %s AND ec.no_end_date = 1
                """, (user_obj.id,))
                expense_cats = cursor.fetchall()

                for cat in expense_cats:
                    category_id = cat[0]
                    recurring_id = cat[1]
                    cursor.execute("""
                        SELECT 1 FROM expense_entries 
                        WHERE category_id = %s AND YEAR(date) = %s LIMIT 1
                    """, (category_id, year_3))
                    entry_exists = cursor.fetchone()
                    if not entry_exists:
                        cursor.execute("""
                            SELECT ec.name, re.amount, re.cadence_interval, re.cadence_unit, re.start_date, re.weekdays, re.monthly_days, re.yearly_day, re.yearly_month
                            FROM recurring_expense re
                            JOIN expense_categories ec ON re.category_id = ec.id
                            WHERE re.id = %s
                        """, (recurring_id,))
                        rec = cursor.fetchone()
                        if rec:
                            data = {
                                'recurring_id': recurring_id,
                                'category_name': rec[0],
                                'amount': rec[1],
                                'cadence_interval': rec[2],
                                'cadence_unit': rec[3],
                                'start_date': date.today().strftime('%Y-%m-%d'),
                                'end_date': date(year_3, 12, 31).strftime('%Y-%m-%d'),
                                'weekdays': rec[5].split(',') if rec[5] else [],
                                'monthly_days': [int(x) for x in rec[6].split(',')] if rec[6] else [],
                                'yearly_day': rec[7],
                                'yearly_month': rec[8],
                                'no_end_date': 1
                            }
                            with app.test_request_context():
                                update_recurring_expense_inner(data, user_obj.id)

                # 3. Check CA categories with no_end_date = 1
                cursor.execute("""
                    SELECT cec.id AS category_id, rce.id AS recurring_id
                    FROM c_expense_categories cec
                    JOIN recurring_c_expense rce ON rce.category_id = cec.id
                    WHERE cec.account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s) AND cec.no_end_date = 1
                """, (user_obj.id,))
                ca_cats = cursor.fetchall()

                for cat in ca_cats:
                    category_id = cat[0]
                    recurring_id = cat[1]
                    cursor.execute("""
                        SELECT 1 FROM c_expense_entries 
                        WHERE category_id = %s AND YEAR(date) = %s LIMIT 1
                    """, (category_id, year_3))
                    entry_exists = cursor.fetchone()
                    if not entry_exists:
                        cursor.execute("""
                            SELECT cec.name, rce.amount, rce.cadence_interval, rce.cadence_unit, rce.start_date, rce.weekdays, rce.monthly_days, rce.yearly_day, rce.yearly_month
                            FROM recurring_c_expense rce
                            JOIN c_expense_categories cec ON rce.category_id = cec.id
                            WHERE rce.id = %s
                        """, (recurring_id,))
                        rec = cursor.fetchone()
                        if rec:
                            data = {
                                'recurring_id': recurring_id,
                                'category_name': rec[0],
                                'amount': rec[1],
                                'cadence_interval': rec[2],
                                'cadence_unit': rec[3],
                                'start_date': date.today().strftime('%Y-%m-%d'),
                                'end_date': date(year_3, 12, 31).strftime('%Y-%m-%d'),
                                'weekdays': rec[5].split(',') if rec[5] else [],
                                'monthly_days': [int(x) for x in rec[6].split(',')] if rec[6] else [],
                                'yearly_day': rec[7],
                                'yearly_month': rec[8],
                                'no_end_date': 1
                            }
                            with app.test_request_context():
                                update_recurring_ca_expense_inner(data, user_obj.id)

                # If the last recorded year is older than the current year + 3, add a new year of Fridays
                if last_weekly_date is None or last_weekly_date < cutoff_date:
                    add_one_year_of_fridays(user_obj.id)

                if last_daily_date is None or last_daily_date < cutoff_date:
                    add_one_year_of_days(user_obj.id)
                    add_one_year_of_savings(user_obj.id)

                if last_monthly_date is None or last_monthly_date < cutoff_date:
                    add_one_year_of_months(user_obj.id)

                if last_ca_weekly_date is None or last_ca_weekly_date < cutoff_date:
                    add_one_year_of_ca_fridays(user_obj.id)

                if last_ca_daily_date is None or last_ca_daily_date < cutoff_date:
                    add_one_year_of_ca_days(user_obj.id)

                if last_ca_monthly_date is None or last_ca_monthly_date < cutoff_date:
                    add_one_year_of_ca_months(user_obj.id)

                # Redirect to user's preferred landing page if set, else dashboard
                landing_page = user[3] if len(user) > 3 else None
                cursor.close()
                if landing_page:
                    return redirect(url_for(landing_page))
                else:
                    return redirect(url_for('dashboard'))

            else:
                cursor.close()
                # Invalid username or password, redirect back to login with an error message
                flash("Invalid username or password")
                return render_template('login.html')

    # If it's a GET request, render the login page
    return render_template('login.html')

############################## Login MFA Route ######################################

@app.route('/login_mfa', methods=['POST'])
def login_mfa():
    code = request.form.get('mfa_code')
    user_id = session.get('pre_mfa_user_id')
    remember = session.get('pre_mfa_remember', False)
    if not user_id:
        flash('Session expired. Please log in again.')
        return redirect(url_for('login'))
    
    with get_db_pool().get_cursor() as cursor:
        cursor.execute("SELECT id, username, password, mfa_secret, landing_page FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
    
    if not user or not user[3]:
        flash('MFA not enabled for this account.')
        return redirect(url_for('login'))
    totp = pyotp.TOTP(user[3])
    if totp.verify(code):
        user_obj = User(id=user[0], username=user[1], password=user[2])
        login_user(user_obj, remember=remember)
        session.pop('pre_mfa_user_id', None)
        session.pop('pre_mfa_remember', None)
        landing_page = user[4] if len(user) > 4 and user[4] else 'dashboard'
        return redirect(url_for(landing_page))
    else:
        flash('Invalid MFA code.')
        return render_template('login.html', mfa_step=True, username=user[1])

############################## Login Add One Year of Data ######################################

def add_one_year_of_fridays(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the last Friday in the totals_remainders table for this user
        cursor.execute("""
            SELECT MAX(date) FROM totals_remainders WHERE user_id = %s
        """, (user_id,))
        last_friday = cursor.fetchone()[0]

        if last_friday is None:
            # No records found, this shouldn't happen because records are created at registration
            return

        # Add one week to get the first Friday of the next year
        start_date = last_friday + timedelta(weeks=1)

        # Get the end date (one year from the start date)
        end_date = date(start_date.year + 1, 12, 31)

        # Find the nearest Friday for the end date
        end_date = find_nearest_friday(end_date, round_up=True)

        # Do not go beyond Dec 31st, 3 years from the current year
        max_end_date = date(date.today().year + 3, 12, 31)
        end_date = min(end_date, max_end_date)

        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))

            current_date += timedelta(weeks=1)  # Move to the next Friday

        save_totals_remainders_d()

        cursor.close()
        conn.commit()

def add_one_year_of_days(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the last date in the totals_remainders_d table for this user
        cursor.execute("""
            SELECT MAX(date) FROM totals_remainders_d WHERE user_id = %s
        """, (user_id,))
        last_day = cursor.fetchone()[0]

        if last_day is None:
            # No records found, you may want to handle this case (e.g., initialize from today)
            last_day = date.today()

        # Add one day to get the first day to insert
        start_date = last_day + timedelta(days=1)

        # Get the end date (one year from the start date)
        end_date = date(start_date.year + 1, 12, 31)

        # Do not go beyond Dec 31st, 3 years from the current year
        max_end_date = date(date.today().year + 3, 12, 31)
        end_date = min(end_date, max_end_date)

        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder, last_day_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))
            current_date += timedelta(days=1)  # Move to the next day

        save_totals_remainders_d()

        cursor.close()
        conn.commit()

def add_one_year_of_months(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the last date in the totals_remainders_m table for this user
        cursor.execute("""
            SELECT MAX(date) FROM totals_remainders_m WHERE user_id = %s
        """, (user_id,))
        last_month = cursor.fetchone()[0]

        if last_month is None:
            # No records found, you may want to handle this case (e.g., initialize from today)
            last_month = date.today().replace(day=1)

        # Add one month to get the first month to insert
        if isinstance(last_month, datetime):
            last_month = last_month.date()
        year = last_month.year
        month = last_month.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        start_month = date(year, month, 1)

        # Get the end date (one year from the start month)
        end_month = date(start_month.year + 1, 12, 31)

        # Do not go beyond Dec 31st, 3 years from the current year
        max_end_date = date(date.today().year + 3, 12, 31)
        end_month = min(end_month, max_end_date)

        current_month = start_month
        while current_month <= end_month:
            # Find the last day of the current month
            if current_month.month == 12:
                next_month = date(current_month.year + 1, 1, 1)
            else:
                next_month = date(current_month.year, current_month.month + 1, 1)
            last_day = next_month - timedelta(days=1)
            if last_day > max_end_date:
                last_day = max_end_date
            cursor.execute("""
                INSERT INTO totals_remainders_m (user_id, date, total_income, total_expenses, remainder, last_month_remainder)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, last_day, 0.00, 0.00, 0.00, 0.00))
            # Move to the first day of the next month
            if current_month.month == 12:
                current_month = date(current_month.year + 1, 1, 1)
            else:
                current_month = date(current_month.year, current_month.month + 1, 1)

        cursor.close()
        conn.commit()

def add_one_year_of_savings(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the last date in the savings_entries table for this user
        cursor.execute("""
            SELECT MAX(date) FROM savings_entries WHERE user_id = %s
        """, (user_id,))
        last_day = cursor.fetchone()[0]

        if last_day is None:
            last_day = date.today()

        start_date = last_day + timedelta(days=1)
        end_date = date(start_date.year + 1, 12, 31)
        max_end_date = date(date.today().year + 3, 12, 31)
        end_date = min(end_date, max_end_date)

        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO savings_entries (user_id, date, amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE amount = amount
            """, (user_id, current_date, 0.00))
            current_date += timedelta(days=1)

        cursor.close()
        conn.commit()

def add_one_year_of_ca_fridays(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Get all credit account IDs for this user
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]

        for account_id in account_ids:
            cursor.execute("SELECT MAX(date) FROM c_a_balances WHERE account_id = %s", (account_id,))
            last_friday = cursor.fetchone()[0]
            if last_friday is None:
                last_friday = date.today()
            start_date = last_friday + timedelta(weeks=1)
            end_date = date(start_date.year + 1, 12, 31)
            # Find the nearest Friday for the end date
            end_date = find_nearest_friday(end_date, round_up=True)
            max_end_date = date(date.today().year + 3, 12, 31)
            end_date = min(end_date, max_end_date)
            current_date = start_date
            while current_date <= end_date:
                cursor.execute("""
                    INSERT INTO c_a_balances (account_id, date, total_expenses, balance)
                    VALUES (%s, %s, 0.00, 0.00)
                """, (account_id, current_date))
                current_date += timedelta(weeks=1)
        
        cursor.close()
        conn.commit()

def add_one_year_of_ca_days(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]
        for account_id in account_ids:
            cursor.execute("SELECT MAX(date) FROM c_a_balances_d WHERE account_id = %s", (account_id,))
            last_day = cursor.fetchone()[0]
            if last_day is None:
                last_day = date.today()
            start_date = last_day + timedelta(days=1)
            end_date = date(start_date.year + 1, 12, 31)
            max_end_date = date(date.today().year + 3, 12, 31)
            end_date = min(end_date, max_end_date)
            current_date = start_date
            while current_date <= end_date:
                cursor.execute("""
                    INSERT INTO c_a_balances_d (account_id, date, total_expenses, balance)
                    VALUES (%s, %s, 0.00, 0.00)
                """, (account_id, current_date))
                current_date += timedelta(days=1)
        
        cursor.close()
        conn.commit()

def add_one_year_of_ca_months(user_id):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]
    for account_id in account_ids:
        cursor.execute("SELECT MAX(date) FROM c_a_balances_m WHERE account_id = %s", (account_id,))
        last_month = cursor.fetchone()[0]
        if last_month is None:
            last_month = date.today().replace(day=1)
        if isinstance(last_month, datetime):
            last_month = last_month.date()
        year = last_month.year
        month = last_month.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        start_month = date(year, month, 1)
        end_month = date(start_month.year + 1, 12, 31)
        max_end_date = date(date.today().year + 3, 12, 31)
        end_month = min(end_month, max_end_date)
        current_month = start_month
        while current_month <= end_month:
            last_day = calendar.monthrange(current_month.year, current_month.month)[1]
            last_date = date(current_month.year, current_month.month, last_day)
            if last_date > max_end_date:
                last_date = max_end_date
            cursor.execute("""
                INSERT INTO c_a_balances_m (account_id, date, total_expenses, balance)
                VALUES (%s, %s, 0.00, 0.00)
            """, (account_id, last_date))
            if current_month.month == 12:
                current_month = date(current_month.year + 1, 1, 1)
            else:
                current_month = date(current_month.year, current_month.month + 1, 1)
    
        cursor.close()
        conn.commit()

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

#################################################################################
############################### DASHBOARD DAY ###################################
#################################################################################

@app.route('/dashboard_d')
@login_required
def dashboard_d():
    selected_date = request.args.get('date')  # Get the selected date from query parameters
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')  # Default to current UTC date as a string

    # Establish the database connection using connection pool
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # Fetch user data including the desired fields (profile_picture, first_name, last_name, balance_threshold, goofy_week_mode)
        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] dashboard_d user settings for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] dashboard_d user settings: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
                FROM users
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] dashboard_d user settings for user {current_user.id}")

        # Extract goofy_week_mode value (defaults to False if not set)
        profile_picture = user_data['profile_picture'] if user_data else None
        first_name = user_data['first_name'] if user_data else ''
        last_name = user_data['last_name'] if user_data else ''
        goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False
        balance_threshold = float(user_data.get('balance_threshold', 0)) if user_data else 0
        member_since = user_data['member_since'] if user_data else None
        currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
        landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard'

        # Fetch income categories with is_auto_adjustment
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_recurring
            FROM income_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        income_categories = cursor.fetchall()

        # Fetch expense categories with is_auto_adjustment, is_bud, and is_recurring
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
            FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Convert the selected date to a proper datetime object
        try:
            selected_date_obj = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            selected_date_obj = datetime.utcnow().date()  # Fallback to current date if selected_date is invalid

        # Calculate the week range (start and end date) based on goofy_week_mode
        if goofy_week_mode:
            # Goofy week mode: week starts on the selected Friday, ends on the next Thursday
            start_of_week = selected_date_obj  # Selected Friday is the start of the week
            end_of_week = start_of_week + timedelta(days=6)  # Ends on the following Thursday
        else:
            # Normal week mode: week starts on the Saturday before the selected Friday, ends on that Friday
            start_of_week = selected_date_obj - timedelta(days=6)  # Week starts on the Saturday before
            end_of_week = selected_date_obj  # Selected Friday is the end of the week

        # Try Redis first for income entries
        income_entries = _get_entries_from_redis('income_entries', current_user.id)
        if income_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d income_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
                ORDER BY ic.display_order DESC, ie.date ASC
            """, (current_user.id,))
            income_entries = list(cursor.fetchall())
            income_entries = _filter_pending_deletions('income_entries', current_user.id, income_entries)
            # Update Redis cache
            _set_entries_to_redis('income_entries', current_user.id, income_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d income_entries for user {current_user.id}")
            # Enrich with category names from income_categories
            income_cat_map = {cat['id']: cat['name'] for cat in income_categories}
            for entry in income_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = income_cat_map.get(entry.get('category_id'), 'Unknown')

        # Try Redis first for expense entries
        expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
        if expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
                ORDER BY ec.display_order DESC, ee.date ASC
            """, (current_user.id,))
            expense_entries = list(cursor.fetchall())
            expense_entries = _filter_pending_deletions('expense_entries', current_user.id, expense_entries)
            # Update Redis cache
            _set_entries_to_redis('expense_entries', current_user.id, expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d expense_entries for user {current_user.id}")
            # Enrich with category names from expense_categories
            expense_cat_map = {cat['id']: cat['name'] for cat in expense_categories}
            for entry in expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # Try Redis first for totals/remainders
        totals_remainders_d = _get_totals_remainders_from_redis('totals_remainders_d', current_user.id)
        if totals_remainders_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d totals_remainders_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM totals_remainders_d
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            totals_remainders_d = cursor.fetchall()
            # Update Redis cache
            _set_totals_remainders_to_redis('totals_remainders_d', current_user.id, totals_remainders_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d totals_remainders_d for user {current_user.id}")

        # Try Redis first for savings entries
        savings_entries = _get_savings_entries_from_redis(current_user.id)
        if savings_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d savings_entries for user {current_user.id}")
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            savings_entries = cursor.fetchall()
            # Update Redis cache
            _set_savings_entries_to_redis(current_user.id, savings_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d savings_entries for user {current_user.id}")

        # Fetch credit accounts for the user
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # Fetch c_expense_categories for the user's credit accounts
        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order ASC, cec.id ASC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # Try Redis first for c_expense_entries
        c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
        if c_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d c_expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT cee.*, cec.name AS category_name
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
                ORDER BY cee.date DESC, cee.id ASC
            """, (current_user.id,))
            c_expense_entries = list(cursor.fetchall())
            c_expense_entries = _filter_pending_deletions('c_expense_entries', current_user.id, c_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('c_expense_entries', current_user.id, c_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d c_expense_entries for user {current_user.id}")
            # Enrich with category names from c_expense_categories
            c_expense_cat_map = {cat['id']: cat['name'] for cat in c_expense_categories}
            for entry in c_expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = c_expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # Try Redis first for c_a_balances_d
        c_a_balances_d = _get_ca_balances_from_redis('c_a_balances_d', current_user.id)
        if c_a_balances_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_d c_a_balances_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                )
                ORDER BY date DESC
            """, (current_user.id,))
            c_a_balances_d = cursor.fetchall()
            # Update Redis cache
            _set_ca_balances_to_redis('c_a_balances_d', current_user.id, c_a_balances_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_d c_a_balances_d for user {current_user.id}")
        cursor.close()

    # Render the template, passing necessary data including selected date, goofy_week_mode, entries, and totals/remainders
    return render_template('dashboard_d.html', 
        selected_date=selected_date, 
        goofy_week_mode=goofy_week_mode, 
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        income_entries=income_entries, 
        expense_entries=expense_entries,
        totals_remainders_d=totals_remainders_d,
        balance_threshold=balance_threshold,  # Pass full user data if needed in the template
        savings_entries=savings_entries,
        member_since=member_since,
        landing_page=landing_page,
        expense_categories=expense_categories,
        income_categories=income_categories,
        currency_type=currency_type,
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances_d=c_a_balances_d
    )

@app.route('/dashboard-d/add_entry', methods=['POST'])
@login_required
def dashboard_d_add_entry():
    data = request.get_json()
    entry_type = data.get('entryType')
    category_id = data.get('category')
    amount = data.get('amount')
    entry_date = data.get('date')
    
    app.logger.info(f"[ADD ENTRY] User {current_user.id}: type={entry_type}, category={category_id}, amount={amount}, date={entry_date}")

    if not all([entry_type, category_id, amount, entry_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        ca_triggered = False
        payment_category_name = None
        account_id = None

        # Determine the appropriate table and category table
        if entry_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
        elif entry_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
        else:
            cursor.close()
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

        # Check that the category exists and belongs to the user
        if entry_type == 'ca':
            cursor.execute("""
                SELECT cec.id
                FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE cec.id = %s AND ca.user_id = %s
            """, (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        else:
            cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        
        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        app.logger.info(f"[ADD ENTRY] Category data for category {category_id}: name='{cat_data.get('name')}', is_credit_account={cat_data.get('is_credit_account', 'MISSING')}")
        cursor.close()

    # Add to Redis only - flush worker will persist to MySQL
    # Get existing entries to check if we need to add or update
    existing_data = _get_entries_from_redis(table_name, current_user.id)
    
    # If not in Redis, load from MySQL first
    if existing_data is None:
        existing_data = []
        with get_db_pool().get_connection() as conn:
            cursor2 = conn.cursor(pymysql.cursors.DictCursor)
            if entry_type == 'income':
                cursor2.execute("""
                    SELECT ie.* FROM income_entries ie
                    JOIN income_categories ic ON ie.category_id = ic.id
                    WHERE ic.user_id = %s
                """, (current_user.id,))
            elif entry_type == 'expense':
                cursor2.execute("""
                    SELECT ee.* FROM expense_entries ee
                    JOIN expense_categories ec ON ee.category_id = ec.id
                    WHERE ec.user_id = %s
                """, (current_user.id,))
            elif entry_type == 'ca':
                cursor2.execute("""
                    SELECT cee.* FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (current_user.id,))
            existing_data = list(cursor2.fetchall())
            cursor2.close()
        # Filter out entries marked for deletion
        existing_data = _filter_pending_deletions(table_name, current_user.id, existing_data)
        app.logger.info(f"[REDIS][{table_name}] Loaded {len(existing_data)} entries from MySQL (after filtering pending deletions)")
    
    existing_entry = None
    
    if existing_data:
        for entry in existing_data:
            if str(entry.get('category_id')) == str(category_id) and str(entry.get('date')) == str(entry_date):
                existing_entry = entry
                break
    
    if existing_entry:
        new_amount = Decimal(existing_entry.get('amount', 0)) + Decimal(amount)
        _update_entry_in_redis(table_name, current_user.id, category_id, entry_date, float(new_amount))
    else:
        _update_entry_in_redis(table_name, current_user.id, category_id, entry_date, float(amount))

    # Check if this is a savings category - update savings if so
    is_savings_category = False
    if entry_type in ['income', 'expense'] and cat_data.get('name') == 'Savings':
        is_savings_category = True

    # If this is an expense category and is_credit_account=1, add payment entry and trigger CA balance update
    app.logger.info(f"[CA PAYMENT DEBUG] entry_type={entry_type}, cat_data={cat_data}")
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        app.logger.info(f"[CA PAYMENT] Detected payment category for user {current_user.id}, category {category_id}")
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        app.logger.info(f"[CA PAYMENT] Category name: '{category_name}'")
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            app.logger.info(f"[CA PAYMENT] Looking for credit account with name: '{account_name}'")
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                app.logger.info(f"[CA PAYMENT] Credit account query result: {account_row}")
                if account_row:
                    account_id = account_row['id']
                    app.logger.info(f"[CA PAYMENT] Found credit account_id={account_id}, adding payment entry for date={entry_date}, amount={amount}")
                    # Add or update payment entry in Redis
                    payment_entries = _get_entries_from_redis('c_payment_entries', current_user.id)
                    app.logger.info(f"[CA PAYMENT] Current payment_entries from Redis: {len(payment_entries) if payment_entries else 'None'}")
                    if payment_entries is None:
                        # Load from MySQL first
                        cursor.execute("""
                            SELECT cpe.* FROM c_payment_entries cpe
                            JOIN credit_accounts ca ON cpe.account_id = ca.id
                            WHERE ca.user_id = %s
                        """, (current_user.id,))
                        payment_entries = list(cursor.fetchall())
                        payment_entries = _filter_pending_deletions('c_payment_entries', current_user.id, payment_entries)
                        app.logger.info(f"[CA PAYMENT] Loaded {len(payment_entries)} payment_entries from MySQL")
                    
                    # Check if payment entry already exists for this date/account
                    existing_payment = None
                    for pe in payment_entries:
                        if str(pe.get('account_id')) == str(account_id) and str(pe.get('date')) == str(entry_date):
                            existing_payment = pe
                            break
                    
                    if existing_payment:
                        new_payment_amount = Decimal(existing_payment.get('amount', 0)) + Decimal(amount)
                        app.logger.info(f"[CA PAYMENT] Updating existing payment: {existing_payment.get('amount')} + {amount} = {new_payment_amount}")
                        _update_payment_entry_in_redis(current_user.id, account_id, entry_date, float(new_payment_amount))
                    else:
                        app.logger.info(f"[CA PAYMENT] Creating new payment entry: account_id={account_id}, date={entry_date}, amount={amount}")
                        _update_payment_entry_in_redis(current_user.id, account_id, entry_date, float(amount))
                    app.logger.info(f"[CA PAYMENT] Payment entry operation completed")
                else:
                    app.logger.warning(f"[CA PAYMENT] No credit account found with name '{account_name}' for user {current_user.id}")
                cursor.close()
        else:
            app.logger.warning(f"[CA PAYMENT] Category name '{category_name}' does not end with ' payment'")
    else:
        app.logger.info(f"[CA PAYMENT] Not a payment category: entry_type={entry_type}, is_credit_account={cat_data.get('is_credit_account', 0)}")

    if entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()
    
    # Update totals and savings if this is a savings category
    if is_savings_category:
        save_totals_remainders_d()

    return jsonify({"status": "success"})


@app.route('/dashboard-d/get_categories', methods=['GET'])
@login_required
def get_categories():
    """
    Get income or expense categories for the current user.
    Used by autobalance to find the Auto Adjustments category.
    """
    entry_type = request.args.get('type')
    
    if not entry_type or entry_type not in ['income', 'expense']:
        return jsonify({'status': 'error', 'message': 'Invalid or missing type parameter'}), 400
    
    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            if entry_type == 'income':
                cursor.execute("""
                    SELECT id, name, is_auto_adjustment, hidden, is_recurring
                    FROM income_categories
                    WHERE user_id = %s
                    ORDER BY display_order DESC
                """, (current_user.id,))
            else:  # expense
                cursor.execute("""
                    SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
                    FROM expense_categories
                    WHERE user_id = %s
                    ORDER BY display_order DESC
                """, (current_user.id,))
            
            categories = cursor.fetchall()
            cursor.close()
        
        return jsonify({'status': 'success', 'categories': categories})
        
    except Exception as e:
        app.logger.error(f"[GET CATEGORIES ERROR] User {current_user.id}: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/dashboard-d/get_totals_for_day', methods=['GET'])
@login_required
def get_totals_for_day():
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify({'status': 'error', 'message': 'No date provided.'}), 400

    try:
        # Try Redis first
        cached_daily = _get_totals_remainders_from_redis('totals_remainders_d', current_user.id)
        
        if cached_daily:
            # Search for the specific date in cached data
            selected_date_obj = datetime.strptime(selected_date, '%Y-%m-%d').date()
            for row in cached_daily:
                row_date = datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']
                if row_date == selected_date_obj:
                    app.logger.debug(f"[REDIS HIT] get_totals_for_day for user {current_user.id}, date {selected_date}")
                    return jsonify({
                        'status': 'success',
                        'total_income': float(row.get('total_income', 0)),
                        'total_expenses': float(row.get('total_expenses', 0)),
                        'remainder': float(row.get('remainder', 0))
                    })
        
        # Redis miss - fallback to MySQL
        app.logger.debug(f"[REDIS MISS] get_totals_for_day for user {current_user.id}, date {selected_date}")
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            # Fetch total income for the selected date - join with income_categories to filter by user_id
            cursor.execute("""
                SELECT COALESCE(SUM(ie.amount), 0) as total_income
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s AND ie.date = %s
            """, (current_user.id, selected_date))
            total_income = cursor.fetchone()['total_income']

            # Fetch total expenses for the selected date - join with expense_categories to filter by user_id
            cursor.execute("""
                SELECT COALESCE(SUM(ee.amount), 0) as total_expenses
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s AND ee.date = %s
            """, (current_user.id, selected_date))
            total_expenses = cursor.fetchone()['total_expenses']

            # Fetch remainder for the selected date from totals_remainders_d
            cursor.execute("""
                SELECT remainder FROM totals_remainders_d
                WHERE user_id = %s AND date = %s
            """, (current_user.id, selected_date))
            row = cursor.fetchone()
            remainder = float(row['remainder']) if row and 'remainder' in row else None
            cursor.close()

        # Return the totals and remainder (if available)
        response = {
            'status': 'success',
            'total_income': float(total_income),
            'total_expenses': float(total_expenses)
        }
        if remainder is not None:
            response['remainder'] = remainder
        return jsonify(response)

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/dashboard-d/update_totals_for_day', methods=['POST'])
@login_required
def update_totals_for_day():
    try:
        data = request.get_json()
        selected_date = data.get('date')

        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Get total income - join with income_categories to filter by user_id
            cursor.execute("""
                SELECT COALESCE(SUM(ie.amount), 0) 
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s AND ie.date = %s
            """, (current_user.id, selected_date))
            total_income = cursor.fetchone()[0]

            # Get total expenses - join with expense_categories to filter by user_id
            cursor.execute("""
                SELECT COALESCE(SUM(ee.amount), 0) 
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s AND ee.date = %s
            """, (current_user.id, selected_date))
            total_expenses = cursor.fetchone()[0]

            # Calculate remainder
            remainder = total_income - total_expenses

            # Update totals_remainders_d table with new values
            cursor.execute("""
                UPDATE totals_remainders_d
                SET total_income = %s, total_expenses = %s, remainder = %s
                WHERE user_id = %s AND date = %s
            """, (total_income, total_expenses, remainder, current_user.id, selected_date))

            conn.commit()
            cursor.close()
        
        # Update Redis cache
        selected_date_obj = datetime.strptime(selected_date, '%Y-%m-%d').date()
        _update_totals_remainders_in_redis('totals_remainders_d', current_user.id, [{
            'date': selected_date_obj,
            'total_income': float(total_income),
            'total_expenses': float(total_expenses),
            'remainder': float(remainder)
        }])
        app.logger.debug(f"[REDIS UPDATE] update_totals_for_day for user {current_user.id}, date {selected_date}")

        return jsonify({'status': 'success', 'remainder': remainder})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/update-processed-status-d-entry', methods=['POST'])
@login_required
def update_processed_status_d_entry():
    from redis_crud import get_entries, bulk_update_entries
    from datetime import datetime
    
    data = request.json
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income', 'expense', or 'ca'
    entry_date = data.get('entry_date')
    processed = data.get('processed')

    if not category_id or not entry_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    try:
        # Determine the table name
        if category_type == 'income':
            table_name = 'income_entries'
        elif category_type == 'expense':
            table_name = 'expense_entries'
        elif category_type == 'ca':
            table_name = 'c_expense_entries'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        # Get all entries for this category (Redis-first)
        all_entries = get_entries(table_name, {'category_id': int(category_id)}, user_id=current_user.id)
        
        app.logger.info(f"[UPDATE PROCESSED D] User {current_user.id}, category {category_id}, date {entry_date}, found {len(all_entries)} total entries")
        
        # Normalize the target date
        target_date = datetime.strptime(entry_date, '%Y-%m-%d').date() if isinstance(entry_date, str) else entry_date
        
        # Filter entries by date (handle both string and date object formats)
        entries = []
        for entry in all_entries:
            entry_date_obj = entry.get('date')
            if isinstance(entry_date_obj, str):
                entry_date_obj = datetime.strptime(entry_date_obj, '%Y-%m-%d').date()
            
            if entry_date_obj == target_date:
                entries.append(entry)
        
        app.logger.info(f"[UPDATE PROCESSED D] Found {len(entries)} entries matching date {target_date}")
        
        if not entries:
            return jsonify({'status': 'success', 'message': 'No entries found for this date'})
        
        # Prepare bulk update
        updates = [{'id': entry['id'], 'processed': processed} for entry in entries]
        
        # Update using Redis-first approach
        success = bulk_update_entries(table_name, updates, user_id=current_user.id)
        
        if success:
            app.logger.info(f"[UPDATE PROCESSED D] Successfully updated {len(updates)} entries")
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': 'Failed to update processed status'}), 500

    except Exception as e:
        app.logger.error(f"Error in update_processed_status_d_entry: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to update processed status'}), 500

@app.route('/get_dashboard_d_data')
@login_required
def get_dashboard_d_data():
    user_id = current_user.id
    date = request.args.get('date')

    # Try Redis first for aggregated data
    cached_daily = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    cached_ca_balances = _get_ca_balances_from_redis('c_a_balances_d', user_id)
    cached_savings = _get_savings_entries_from_redis(user_id)
    
    # For entries, check if user is hydrated
    from redis_manager import is_user_hydrated, get_cached_data
    
    entries_cached = False
    income_entries = []
    expense_entries = []
    c_expense_entries = []
    
    if is_user_hydrated(user_id):
        # Try to get entries from Redis
        income_entries_raw = get_cached_data('income_entries', user_id)
        expense_entries_raw = get_cached_data('expense_entries', user_id)
        c_expense_entries_raw = get_cached_data('c_expense_entries', user_id)
        
        if income_entries_raw is not None and expense_entries_raw is not None and c_expense_entries_raw is not None:
            entries_cached = True
            # Need to enrich with category names - fetch categories from cache too
            income_categories = get_cached_data('income_categories', user_id) or []
            expense_categories = get_cached_data('expense_categories', user_id) or []
            c_expense_categories = get_cached_data('c_expense_categories', user_id) or []
            
            # Create lookup maps
            income_cat_map = {cat['id']: cat for cat in income_categories}
            expense_cat_map = {cat['id']: cat for cat in expense_categories}
            c_expense_cat_map = {cat['id']: cat for cat in c_expense_categories}
            
            # Enrich income entries
            for entry in income_entries_raw:
                cat = income_cat_map.get(entry.get('category_id'), {})
                income_entries.append({
                    'id': entry.get('id'),
                    'date': entry.get('date'),
                    'amount': entry.get('amount'),
                    'processed': entry.get('processed'),
                    'category_id': entry.get('category_id'),
                    'category_name': cat.get('name', ''),
                    'display_order': cat.get('display_order', 0)
                })
            
            # Enrich expense entries
            for entry in expense_entries_raw:
                cat = expense_cat_map.get(entry.get('category_id'), {})
                expense_entries.append({
                    'id': entry.get('id'),
                    'date': entry.get('date'),
                    'amount': entry.get('amount'),
                    'processed': entry.get('processed'),
                    'category_id': entry.get('category_id'),
                    'category_name': cat.get('name', ''),
                    'display_order': cat.get('display_order', 0)
                })
            
            # Enrich c_expense entries
            for entry in c_expense_entries_raw:
                cat = c_expense_cat_map.get(entry.get('category_id'), {})
                c_expense_entries.append({
                    **entry,
                    'category_name': cat.get('name', '')
                })
    
    # Use Redis data when available, fetch missing pieces from MySQL
    totals_remainders_d = cached_daily
    c_a_balances_d = cached_ca_balances
    savings_entries = cached_savings
    
    # Track what we need to fetch from MySQL
    need_mysql = False
    redis_hits = []
    redis_misses = []
    
    if cached_daily:
        redis_hits.append('totals_remainders_d')
    else:
        redis_misses.append('totals_remainders_d')
        need_mysql = True
    
    if cached_ca_balances:
        redis_hits.append('c_a_balances_d')
    else:
        redis_misses.append('c_a_balances_d')
        need_mysql = True
    
    if cached_savings:
        redis_hits.append('savings_entries')
    else:
        redis_misses.append('savings_entries')
        need_mysql = True
    
    if entries_cached:
        redis_hits.append('entries')
    else:
        redis_misses.append('entries')
        need_mysql = True
    
    # Fetch missing data from MySQL
    if need_mysql:
        app.logger.info(f"[REDIS PARTIAL] get_dashboard_d_data for user {user_id}, hits={redis_hits}, misses={redis_misses}")
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch daily totals/remainders if not cached
            if not cached_daily:
                cursor.execute("""
                    SELECT *
                    FROM totals_remainders_d
                    WHERE user_id = %s
                    ORDER BY date ASC
                """, (user_id,))
                totals_remainders_d = cursor.fetchall()
                # Update Redis cache
                _set_totals_remainders_to_redis('totals_remainders_d', user_id, totals_remainders_d)

            # Fetch daily CA balances if not cached
            if not cached_ca_balances:
                cursor.execute("""
                    SELECT *
                    FROM c_a_balances_d
                    WHERE account_id IN (
                        SELECT id FROM credit_accounts WHERE user_id = %s
                    )
                    ORDER BY account_id ASC, date ASC
                """, (user_id,))
                c_a_balances_d = cursor.fetchall()
                # Update Redis cache
                _set_ca_balances_to_redis('c_a_balances_d', user_id, c_a_balances_d)

            # Fetch savings entries if not cached
            if not cached_savings:
                cursor.execute("""
                    SELECT date, amount FROM savings_entries
                    WHERE user_id = %s
                    ORDER BY date ASC
                """, (user_id,))
                savings_entries = cursor.fetchall()
                # Update Redis cache
                _set_savings_entries_to_redis(user_id, savings_entries)

            # Fetch entries if not cached
            if not entries_cached:
                # Fetch all income entries for the user
                cursor.execute("""
                    SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
                    FROM income_entries ie
                    JOIN income_categories ic ON ie.category_id = ic.id
                    WHERE ic.user_id = %s
                    ORDER BY ic.display_order DESC, ie.date ASC
                """, (user_id,))
                income_entries = list(cursor.fetchall())
                # Filter out entries marked for deletion
                income_entries = _filter_pending_deletions('income_entries', user_id, income_entries)

                # Fetch all expense entries for the user
                cursor.execute("""
                    SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
                    FROM expense_entries ee
                    JOIN expense_categories ec ON ee.category_id = ec.id
                    WHERE ec.user_id = %s
                    ORDER BY ec.display_order DESC, ee.date ASC
                """, (user_id,))
                expense_entries = list(cursor.fetchall())
                # Filter out entries marked for deletion
                expense_entries = _filter_pending_deletions('expense_entries', user_id, expense_entries)

                # Fetch all c_expense_entries for the user's credit accounts
                cursor.execute("""
                    SELECT cee.*, cec.name AS category_name
                    FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                    ORDER BY cee.date DESC, cee.id ASC
                """, (user_id,))
                c_expense_entries = list(cursor.fetchall())
                # Filter out entries marked for deletion
                c_expense_entries = _filter_pending_deletions('c_expense_entries', user_id, c_expense_entries)

            cursor.close()
    else:
        app.logger.info(f"[REDIS HIT] get_dashboard_d_data for user {user_id} - all data from Redis")

    return jsonify({
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries,
        "income_entries": income_entries,
        "expense_entries": expense_entries,
        "c_expense_entries": c_expense_entries
    })

############################################################################################
############################### TOTALS, REMAINDERS, BALANCES ###############################
############################################################################################

# Redis helper functions for totals/remainders/balances

def _get_totals_remainders_from_redis(table_name, user_id, start_date=None):
    """
    Get totals/remainders data from Redis.
    
    Args:
        table_name: 'totals_remainders', 'totals_remainders_d', or 'totals_remainders_m'
        user_id: User ID
        start_date: Optional filter for dates >= start_date
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        if cached:
            data = json.loads(cached)
            # Filter by start_date if provided
            if start_date:
                data = [row for row in data if datetime.strptime(row['date'], '%Y-%m-%d').date() >= start_date]
            return data
        return None
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] GET error user={user_id}: {e}")
        return None

def _set_totals_remainders_to_redis(table_name, user_id, data):
    """
    Set totals/remainders data to Redis.
    
    Args:
        table_name: 'totals_remainders', 'totals_remainders_d', or 'totals_remainders_m'
        user_id: User ID
        data: List of dicts with date, total_income, total_expenses, remainder, etc.
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        # Convert dates to strings for JSON serialization
        serializable_data = []
        for row in data:
            row_copy = row.copy()
            if 'date' in row_copy and isinstance(row_copy['date'], date):
                row_copy['date'] = row_copy['date'].isoformat()
            # Convert Decimals to floats
            for k, v in row_copy.items():
                if isinstance(v, Decimal):
                    row_copy[k] = float(v)
            serializable_data.append(row_copy)
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(serializable_data))
        app.logger.debug(f"[REDIS][{table_name}] SET user={user_id}, rows={len(data)}")
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] SET error user={user_id}: {e}")

def _update_totals_remainders_in_redis(table_name, user_id, updates):
    """
    Update specific rows in Redis cache.
    
    Args:
        table_name: Table name
        user_id: User ID
        updates: List of dicts with updated values (must include 'date' key)
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        if cached:
            # Cache exists - update specific rows
            data = json.loads(cached)
            # Create a map of date -> update data
            update_map = {}
            for update in updates:
                date_str = update['date'].isoformat() if isinstance(update['date'], date) else update['date']
                update_map[date_str] = update
            
            # Update matching rows
            for i, row in enumerate(data):
                if row['date'] in update_map:
                    data[i].update(update_map[row['date']])
                    # Convert date back to string if needed
                    if isinstance(data[i]['date'], date):
                        data[i]['date'] = data[i]['date'].isoformat()
            
            # Add new rows that don't exist yet
            existing_dates = {row['date'] for row in data}
            for update in updates:
                date_str = update['date'].isoformat() if isinstance(update['date'], date) else update['date']
                if date_str not in existing_dates:
                    new_row = update.copy()
                    if isinstance(new_row['date'], date):
                        new_row['date'] = new_row['date'].isoformat()
                    data.append(new_row)
            
            _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(data))
            # Mark table as dirty for flush (with TTL matching cache TTL)
            dirty_key = f"dirty_tables:{user_id}"
            _redis_client.sadd(dirty_key, table_name)
            _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
            app.logger.info(f"[REDIS][{table_name}] UPDATE user={user_id}, rows={len(updates)}")
        else:
            # Cache doesn't exist - create it with the updates
            serializable_updates = []
            for update in updates:
                update_copy = update.copy()
                if isinstance(update_copy['date'], date):
                    update_copy['date'] = update_copy['date'].isoformat()
                # Convert Decimals to floats
                for k, v in update_copy.items():
                    if isinstance(v, Decimal):
                        update_copy[k] = float(v)
                serializable_updates.append(update_copy)
            
            _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(serializable_updates))
            # Mark table as dirty for flush (with TTL matching cache TTL)
            dirty_key = f"dirty_tables:{user_id}"
            _redis_client.sadd(dirty_key, table_name)
            _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
            app.logger.info(f"[REDIS][{table_name}] CREATE user={user_id}, rows={len(updates)}")
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] UPDATE error user={user_id}: {e}")

def _get_ca_balances_from_redis(table_name, user_id, account_id=None, start_date=None):
    """
    Get credit account balances from Redis.
    
    Args:
        table_name: 'c_a_balances', 'c_a_balances_d', or 'c_a_balances_m'
        user_id: User ID
        account_id: Optional account ID filter
        start_date: Optional date filter
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        if cached:
            data = json.loads(cached)
            # Apply filters
            if account_id:
                data = [row for row in data if row.get('account_id') == account_id]
            if start_date:
                data = [row for row in data if datetime.strptime(row['date'], '%Y-%m-%d').date() >= start_date]
            return data
        return None
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] GET error user={user_id}: {e}")
        return None

def _set_ca_balances_to_redis(table_name, user_id, data):
    """
    Set credit account balances to Redis, merging with existing data.
    
    Args:
        table_name: 'c_a_balances', 'c_a_balances_d', or 'c_a_balances_m'
        user_id: User ID
        data: List of balance records to add/update
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        
        # Get existing data from Redis
        existing_data = []
        cached = _redis_client.get(redis_key)
        if cached:
            existing_data = json.loads(cached)
        
        # Create a map of existing records by (account_id, date)
        existing_map = {}
        for row in existing_data:
            account_id = row.get('account_id')
            row_date = row.get('date')
            if isinstance(row_date, str):
                row_date = datetime.strptime(row_date, '%Y-%m-%d').date().isoformat()
            key = (account_id, row_date)
            existing_map[key] = row
        
        # Serialize and merge new data
        for row in data:
            row_copy = row.copy()
            if 'date' in row_copy and isinstance(row_copy['date'], date):
                row_copy['date'] = row_copy['date'].isoformat()
            for k, v in row_copy.items():
                if isinstance(v, Decimal):
                    row_copy[k] = float(v)
            
            # Update or add the record
            key = (row_copy.get('account_id'), row_copy.get('date'))
            existing_map[key] = row_copy
        
        # Convert back to list
        merged_data = list(existing_map.values())
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(merged_data))
        # Mark table as dirty for flush (with TTL matching cache TTL)
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, table_name)
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        app.logger.debug(f"[REDIS][{table_name}] MERGED user={user_id}, new_rows={len(data)}, total_rows={len(merged_data)}")
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] SET error user={user_id}: {e}")

def _get_savings_entries_from_redis(user_id, start_date=None):
    """
    Get savings entries from Redis.
    
    Args:
        user_id: User ID
        start_date: Optional filter for dates >= start_date
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"savings_entries:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        if cached:
            data = json.loads(cached)
            if start_date:
                data = [row for row in data if datetime.strptime(row['date'], '%Y-%m-%d').date() >= start_date]
            return data
        return None
    except Exception as e:
        app.logger.warning(f"[REDIS][savings_entries] GET error user={user_id}: {e}")
        return None

def _set_savings_entries_to_redis(user_id, data):
    """
    Set savings entries to Redis.
    
    Args:
        user_id: User ID
        data: List of savings records
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"savings_entries:v1:{user_id}"
        serializable_data = []
        for row in data:
            row_copy = row.copy()
            if 'date' in row_copy and isinstance(row_copy['date'], date):
                row_copy['date'] = row_copy['date'].isoformat()
            for k, v in row_copy.items():
                if isinstance(v, Decimal):
                    row_copy[k] = float(v)
            serializable_data.append(row_copy)
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(serializable_data))
        # Mark table as dirty for flush (with TTL matching cache TTL)
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, "savings_entries")
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        app.logger.debug(f"[REDIS][savings_entries] SET user={user_id}, rows={len(data)}")
    except Exception as e:
        app.logger.warning(f"[REDIS][savings_entries] SET error user={user_id}: {e}")

# Redis helper functions for user settings

def _update_user_setting_in_redis(user_id, field, value):
    """
    Update a single user setting field in Redis and mark dirty for flush.
    
    Args:
        user_id: User ID
        field: Field name (e.g., 'balance_threshold', 'starting_savings')
        value: New value for the field
    """
    app.logger.info(f"[REDIS][user_settings] CALLED: user_id={user_id}, field={field}, value={value}")
    
    if not app.config.get('REDIS_OK'):
        app.logger.warning(f"[REDIS][user_settings] REDIS_OK is False, skipping Redis update")
        return
    
    try:
        # Get current user data from Redis or MySQL
        redis_key = f"users:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        app.logger.info(f"[REDIS][user_settings] Redis key: {redis_key}, cached exists: {cached is not None}")
        
        if cached:
            user_data = json.loads(cached)
            app.logger.info(f"[REDIS][user_settings] Loaded from Redis, keys: {list(user_data.keys())}")
        else:
            # Load from MySQL
            app.logger.info(f"[REDIS][user_settings] Not in Redis, loading from MySQL")
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()
                cursor.close()
            
            if not user_data:
                app.logger.warning(f"[REDIS][user_settings] User {user_id} not found in MySQL")
                return
            
            # Convert to dict if needed
            user_data = dict(user_data)
            app.logger.info(f"[REDIS][user_settings] Loaded from MySQL, keys: {list(user_data.keys())}")
        
        # Update the field
        old_value = user_data.get(field, 'NOT_SET')
        user_data[field] = value
        app.logger.info(f"[REDIS][user_settings] Updated field '{field}': {old_value} -> {value}")
        
        # Serialize data
        serializable_data = user_data.copy()
        for k, v in serializable_data.items():
            if isinstance(v, (date, datetime)):
                serializable_data[k] = v.isoformat()
            elif isinstance(v, Decimal):
                serializable_data[k] = float(v)
        
        # Save to Redis
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(serializable_data))
        app.logger.info(f"[REDIS][user_settings] Saved to Redis key: {redis_key}")
        
        # Mark as dirty for flush
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, "users")
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        app.logger.info(f"[REDIS][user_settings] Marked 'users' as dirty for user {user_id}")
        
        app.logger.info(f"[REDIS][user_settings] SUCCESS: Updated {field}={value} for user={user_id}")
    except Exception as e:
        app.logger.error(f"[REDIS][user_settings] UPDATE ERROR user={user_id}, field={field}: {e}", exc_info=True)

# Redis helper functions for entry management (income/expense/c_expense)

def _get_entries_from_redis(table_name, user_id):
    """
    Get entries from Redis.
    
    Args:
        table_name: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        
        if cached:
            return json.loads(cached)
        return None
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] GET error user={user_id}: {e}")
        return None

def _filter_pending_deletions(table_name, user_id, entries):
    """
    Filter out entries that are pending deletion (deleted from Redis but not yet flushed to MySQL).
    This prevents MySQL fallback from resurrecting deleted entries.
    
    Args:
        table_name: Table name
        user_id: User ID
        entries: List of entries from MySQL
        
    Returns:
        Filtered list of entries
    """
    if not app.config.get('REDIS_OK') or not entries:
        return entries
    
    try:
        pending_key = f"pending_deletes:{table_name}:{user_id}"
        pending_deletes = _redis_client.smembers(pending_key)
        
        if not pending_deletes:
            return entries
        
        # Convert to set of integers for fast lookup
        pending_delete_ids = {int(id_str) for id_str in pending_deletes}
        
        # Filter out entries with IDs in the pending deletion set
        filtered = [entry for entry in entries if entry.get('id') not in pending_delete_ids]
        
        if len(filtered) < len(entries):
            app.logger.debug(f"[REDIS][{table_name}] Filtered {len(entries) - len(filtered)} pending deletions for user {user_id}")
        
        return filtered
        
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] Error filtering pending deletions: {e}")
        return entries

def _set_entries_to_redis(table_name, user_id, data):
    """
    Set entries to Redis and mark dirty for flush.
    
    Args:
        table_name: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID
        data: List of entry records
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        # Serialize data
        serializable_data = []
        for row in data:
            row_copy = row.copy()
            if 'date' in row_copy and isinstance(row_copy['date'], date):
                row_copy['date'] = row_copy['date'].isoformat()
            for k, v in row_copy.items():
                if isinstance(v, Decimal):
                    row_copy[k] = float(v)
            serializable_data.append(row_copy)
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(serializable_data))
        # Mark table as dirty for flush
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, table_name)
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        app.logger.debug(f"[REDIS][{table_name}] SET user={user_id}, rows={len(data)}")
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] SET error user={user_id}: {e}")

def _update_entry_in_redis(table_name, user_id, category_id, entry_date, amount, processed=0, entry_id=None, bud_item_id=None):
    """
    Update or insert a single entry in Redis cache.
    
    Args:
        table_name: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID
        category_id: Category ID
        entry_date: Entry date (date object or string)
        amount: Entry amount
        processed: Processed flag (0 or 1)
        entry_id: Existing entry ID (if updating) or None (will generate)
        bud_item_id: Optional bud_item_id for expense entries
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        # Get current entries from Redis
        entries = _get_entries_from_redis(table_name, user_id)
        
        # If not in Redis, load from MySQL first
        if entries is None:
            entries = []
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                if table_name == 'income_entries':
                    cursor.execute("""
                        SELECT ie.* FROM income_entries ie
                        JOIN income_categories ic ON ie.category_id = ic.id
                        WHERE ic.user_id = %s
                    """, (user_id,))
                elif table_name == 'expense_entries':
                    cursor.execute("""
                        SELECT ee.* FROM expense_entries ee
                        JOIN expense_categories ec ON ee.category_id = ec.id
                        WHERE ec.user_id = %s
                    """, (user_id,))
                elif table_name == 'c_expense_entries':
                    cursor.execute("""
                        SELECT cee.* FROM c_expense_entries cee
                        JOIN c_expense_categories cec ON cee.category_id = cec.id
                        JOIN credit_accounts ca ON cec.account_id = ca.id
                        WHERE ca.user_id = %s
                    """, (user_id,))
                entries = list(cursor.fetchall())
                cursor.close()
            
            # Filter out any entries pending deletion
            entries = _filter_pending_deletions(table_name, user_id, entries)
            app.logger.info(f"[REDIS][{table_name}] Loaded {len(entries)} entries from MySQL for user {user_id}")
        
        # Convert entry_date to string for comparison
        if isinstance(entry_date, date):
            entry_date_str = entry_date.isoformat()
        else:
            entry_date_str = entry_date
        
        # Find existing entry
        found = False
        for entry in entries:
            if (int(entry.get('category_id', 0)) == int(category_id) and 
                entry.get('date') == entry_date_str):
                # Update existing entry
                entry['amount'] = float(amount)
                entry['processed'] = int(processed)
                if bud_item_id is not None:
                    entry['bud_item_id'] = int(bud_item_id)
                found = True
                break
        
        if not found:
            # Create new entry
            new_entry = {
                'id': entry_id or (max([e.get('id', 0) for e in entries], default=0) + 1),
                'category_id': int(category_id),
                'date': entry_date_str,
                'amount': float(amount),
                'recurring_id': None,
                'processed': int(processed)
            }
            if table_name in ['expense_entries', 'c_expense_entries']:
                new_entry['bud_item_id'] = int(bud_item_id) if bud_item_id is not None else None
            entries.append(new_entry)
        
        # Write back to Redis
        _set_entries_to_redis(table_name, user_id, entries)
        
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] UPDATE error user={user_id}: {e}")

def _delete_entry_in_redis(table_name, user_id, category_id, start_date, end_date):
    """
    Delete entries in Redis cache for a date range.
    
    Args:
        table_name: 'income_entries', 'expense_entries', or 'c_expense_entries'
        user_id: User ID
        category_id: Category ID
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        # Get current entries from Redis
        entries = _get_entries_from_redis(table_name, user_id)
        
        # If not in Redis, load from MySQL first
        if entries is None:
            entries = []
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                if table_name == 'income_entries':
                    cursor.execute("""
                        SELECT ie.* FROM income_entries ie
                        JOIN income_categories ic ON ie.category_id = ic.id
                        WHERE ic.user_id = %s
                    """, (user_id,))
                elif table_name == 'expense_entries':
                    cursor.execute("""
                        SELECT ee.* FROM expense_entries ee
                        JOIN expense_categories ec ON ee.category_id = ec.id
                        WHERE ec.user_id = %s
                    """, (user_id,))
                elif table_name == 'c_expense_entries':
                    cursor.execute("""
                        SELECT cee.* FROM c_expense_entries cee
                        JOIN c_expense_categories cec ON cee.category_id = cec.id
                        JOIN credit_accounts ca ON cec.account_id = ca.id
                        WHERE ca.user_id = %s
                    """, (user_id,))
                entries = list(cursor.fetchall())
                cursor.close()
            
            # Filter out any entries pending deletion (in case cache was cleared)
            entries = _filter_pending_deletions(table_name, user_id, entries)
            app.logger.info(f"[REDIS][{table_name}] Loaded {len(entries)} entries from MySQL for user {user_id}")
        
        # Convert dates to strings for comparison
        if isinstance(start_date, date):
            start_date_str = start_date.isoformat()
        else:
            start_date_str = start_date
        if isinstance(end_date, date):
            end_date_str = end_date.isoformat()
        else:
            end_date_str = end_date
        
        # Filter out entries in the date range for this category
        # Also track which entry IDs are being deleted
        deleted_ids = []
        filtered_entries = []
        for entry in entries:
            if int(entry.get('category_id', 0)) == int(category_id) and start_date_str <= entry.get('date', '') <= end_date_str:
                # This entry is being deleted
                if 'id' in entry:
                    deleted_ids.append(entry['id'])
            else:
                # Keep this entry
                filtered_entries.append(entry)
        
        # Write filtered entries back to Redis
        _set_entries_to_redis(table_name, user_id, filtered_entries)
        
        # Track pending deletions to prevent MySQL fallback from resurrecting them
        if deleted_ids:
            pending_key = f"pending_deletes:{table_name}:{user_id}"
            _redis_client.sadd(pending_key, *deleted_ids)
            _redis_client.expire(pending_key, PERSISTENT_CACHE_TTL)  # Expire after flush would complete
            app.logger.debug(f"[REDIS][{table_name}] Marked {len(deleted_ids)} entries as pending deletion for user {user_id}")
        
    except Exception as e:
        app.logger.warning(f"[REDIS][{table_name}] DELETE error user={user_id}: {e}")

def _update_payment_entry_in_redis(user_id, account_id, entry_date, amount):
    """
    Update or insert a payment entry in Redis cache.
    
    Args:
        user_id: User ID
        account_id: Credit account ID
        entry_date: Payment date (date object or string)
        amount: Payment amount
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        table_name = 'c_payment_entries'
        # Get current entries from Redis
        entries = _get_entries_from_redis(table_name, user_id)
        
        # If not in Redis, load from MySQL first
        if entries is None:
            entries = []
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT cpe.* FROM c_payment_entries cpe
                    JOIN credit_accounts ca ON cpe.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                entries = list(cursor.fetchall())
                cursor.close()
            
            # Filter out any entries pending deletion
            entries = _filter_pending_deletions(table_name, user_id, entries)
            app.logger.info(f"[REDIS][{table_name}] Loaded {len(entries)} entries from MySQL for user {user_id}")
        
        # Convert entry_date to string for comparison
        if isinstance(entry_date, date):
            entry_date_str = entry_date.isoformat()
        else:
            entry_date_str = entry_date
        
        # Find existing entry
        found = False
        for entry in entries:
            if (int(entry.get('account_id', 0)) == int(account_id) and 
                entry.get('date') == entry_date_str):
                # Update existing entry
                entry['amount'] = float(amount)
                found = True
                break
        
        if not found:
            # Generate a new ID (use negative to avoid conflicts with MySQL IDs)
            max_id = max([abs(int(e.get('id', 0))) for e in entries], default=0)
            new_id = -(max_id + 1)
            
            # Add new entry
            entries.append({
                'id': new_id,
                'account_id': int(account_id),
                'date': entry_date_str,
                'amount': float(amount),
                'recurring_id': None,
                'processed': 0
            })
        
        # Save back to Redis
        _set_entries_to_redis(table_name, user_id, entries)
        app.logger.debug(f"[REDIS][{table_name}] Updated payment entry for account={account_id}, date={entry_date_str}, amount={amount}")
        
    except Exception as e:
        app.logger.warning(f"[REDIS][c_payment_entries] UPDATE error user={user_id}: {e}")

def _delete_payment_entry_in_redis(user_id, account_id, start_date, end_date):
    """
    Delete payment entries from Redis cache by account and date range.
    
    Args:
        user_id: User ID
        account_id: Credit account ID
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        table_name = 'c_payment_entries'
        # Get current entries from Redis
        entries = _get_entries_from_redis(table_name, user_id)
        
        # If not in Redis, load from MySQL first
        if entries is None:
            entries = []
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT cpe.* FROM c_payment_entries cpe
                    JOIN credit_accounts ca ON cpe.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                entries = list(cursor.fetchall())
                cursor.close()
            app.logger.info(f"[REDIS][{table_name}] Loaded {len(entries)} entries from MySQL for user {user_id}")
        
        # Convert dates to strings for comparison
        if isinstance(start_date, date):
            start_date_str = start_date.isoformat()
        else:
            start_date_str = start_date
            
        if isinstance(end_date, date):
            end_date_str = end_date.isoformat()
        else:
            end_date_str = end_date
        
        # Filter out entries to delete
        deleted_ids = []
        filtered_entries = []
        for entry in entries:
            entry_date_str = entry.get('date')
            if (int(entry.get('account_id', 0)) == int(account_id) and
                start_date_str <= entry_date_str <= end_date_str):
                # Mark for deletion
                entry_id = entry.get('id')
                if entry_id and int(entry_id) > 0:  # Only track positive IDs (from MySQL)
                    deleted_ids.append(str(entry_id))
            else:
                filtered_entries.append(entry)
        
        # Save filtered entries back to Redis
        _set_entries_to_redis(table_name, user_id, filtered_entries)
        app.logger.debug(f"[REDIS][{table_name}] Deleted {len(entries) - len(filtered_entries)} payment entries for account={account_id}, dates={start_date_str} to {end_date_str}")
        
        # Track pending deletions to prevent MySQL fallback from resurrecting them
        if deleted_ids:
            pending_key = f"pending_deletes:{table_name}:{user_id}"
            _redis_client.sadd(pending_key, *deleted_ids)
            _redis_client.expire(pending_key, PERSISTENT_CACHE_TTL)
            app.logger.debug(f"[REDIS][{table_name}] Marked {len(deleted_ids)} payment entries as pending deletion for user {user_id}")
        
    except Exception as e:
        app.logger.warning(f"[REDIS][c_payment_entries] DELETE error user={user_id}: {e}")

def _get_payment_entries_from_redis(user_id):
    """
    Get payment entries from Redis.
    
    Args:
        user_id: User ID
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"c_payment_entries:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        if cached:
            return json.loads(cached)
        return None
    except Exception as e:
        app.logger.error(f"Error getting payment entries from Redis: {e}")
        return None

# Redis helper functions for recurring tables

def _get_recurring_from_redis(table_name, user_id):
    """
    Get recurring records from Redis.
    
    Args:
        table_name: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        user_id: User ID
        
    Returns:
        List of dicts or None if not in cache
    """
    if not app.config.get('REDIS_OK'):
        return None
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        if cached:
            return json.loads(cached)
        return None
    except Exception as e:
        app.logger.error(f"Error getting {table_name} from Redis: {e}")
        return None

def _set_recurring_to_redis(table_name, user_id, data):
    """
    Set recurring records to Redis and mark dirty for flush.
    
    Args:
        table_name: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        user_id: User ID
        data: List of recurring records
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        _redis_client.setex(
            redis_key,
            PERSISTENT_CACHE_TTL,
            json.dumps(data, cls=DecimalEncoder)
        )
        
        # Mark table as dirty for flush worker
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, table_name)
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        
    except Exception as e:
        app.logger.error(f"Error setting {table_name} to Redis: {e}")

def _update_recurring_in_redis(table_name, user_id, recurring_data):
    """
    Update or insert a single recurring record in Redis cache.
    
    Args:
        table_name: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        user_id: User ID
        recurring_data: Dict with recurring record data (must include 'id' for update or None for insert)
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        
        # Get existing data
        cached = _redis_client.get(redis_key)
        if cached:
            rows = json.loads(cached)
        else:
            rows = []
        
        recurring_id = recurring_data.get('id')
        
        if recurring_id:
            # Update existing record
            found = False
            for i, row in enumerate(rows):
                if int(row.get('id')) == int(recurring_id):
                    # Preserve category_name if not provided in update
                    if 'category_name' not in recurring_data and 'category_name' in row:
                        recurring_data['category_name'] = row['category_name']
                    rows[i] = recurring_data
                    found = True
                    break
            
            if not found:
                # Record not in cache, add it
                rows.append(recurring_data)
        else:
            # New record - generate a temporary negative ID (will be replaced by MySQL on flush)
            temp_id = -int(time.time() * 1000000)  # Use negative timestamp to avoid collisions
            recurring_data['id'] = temp_id
            rows.append(recurring_data)
        
        # Save back to Redis
        _redis_client.setex(
            redis_key,
            PERSISTENT_CACHE_TTL,
            json.dumps(rows, cls=DecimalEncoder)
        )
        
        # Mark as dirty
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, table_name)
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        
    except Exception as e:
        app.logger.error(f"Error updating {table_name} in Redis: {e}")

def _delete_recurring_in_redis(table_name, user_id, recurring_id):
    """
    Delete a recurring record from Redis cache.
    
    Args:
        table_name: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        user_id: User ID
        recurring_id: ID of the recurring record to delete
    """
    if not app.config.get('REDIS_OK'):
        return
    
    try:
        redis_key = f"{table_name}:v1:{user_id}"
        
        # Get existing data
        cached = _redis_client.get(redis_key)
        if cached:
            rows = json.loads(cached)
            # Filter out the deleted record (compare as int to handle both int and str IDs)
            recurring_id_int = int(recurring_id)
            rows = [row for row in rows if int(row.get('id')) != recurring_id_int]
            
            # Save back to Redis
            _redis_client.setex(
                redis_key,
                PERSISTENT_CACHE_TTL,
                json.dumps(rows, cls=DecimalEncoder)
            )
        
        # Track pending deletion for flush worker
        pending_key = f"pending_deletes:{table_name}:{user_id}"
        _redis_client.sadd(pending_key, str(recurring_id))
        _redis_client.expire(pending_key, PERSISTENT_CACHE_TTL)
        
        # Mark as dirty
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, table_name)
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        
    except Exception as e:
        app.logger.error(f"Error deleting {table_name} in Redis: {e}")

# Redis helper functions for buds and bud_items

def _get_buds_from_redis(user_id):
    """Get buds from Redis."""
    if not app.config.get('REDIS_OK'):
        return None
    try:
        redis_key = f"buds:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        return json.loads(cached) if cached else None
    except Exception as e:
        app.logger.error(f"Error getting buds from Redis: {e}")
        return None

def _set_buds_to_redis(user_id, data):
    """Set buds to Redis and mark dirty for flush."""
    if not app.config.get('REDIS_OK'):
        return
    try:
        redis_key = f"buds:v1:{user_id}"
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(data, cls=DecimalEncoder))
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'buds')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
    except Exception as e:
        app.logger.error(f"Error setting buds to Redis: {e}")

def _update_bud_in_redis(user_id, bud_data):
    """Update or insert a single bud record in Redis cache."""
    if not app.config.get('REDIS_OK'):
        return None
    try:
        redis_key = f"buds:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        rows = json.loads(cached) if cached else []
        
        bud_id = bud_data.get('id')
        if bud_id:
            found = False
            for i, row in enumerate(rows):
                if int(row.get('id')) == int(bud_id):
                    rows[i] = bud_data
                    found = True
                    break
            if not found:
                rows.append(bud_data)
        else:
            temp_id = -int(time.time() * 1000000)
            bud_data['id'] = temp_id
            rows.append(bud_data)
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(rows, cls=DecimalEncoder))
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'buds')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        return bud_data['id']
    except Exception as e:
        app.logger.error(f"Error updating bud in Redis: {e}")
        return None

def _delete_bud_in_redis(user_id, bud_id):
    """Delete a bud record from Redis cache."""
    if not app.config.get('REDIS_OK'):
        return
    try:
        redis_key = f"buds:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        if cached:
            rows = json.loads(cached)
            rows = [row for row in rows if int(row.get('id')) != int(bud_id)]
            _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(rows, cls=DecimalEncoder))
        
        pending_key = f"pending_deletes:buds:{user_id}"
        _redis_client.sadd(pending_key, str(bud_id))
        _redis_client.expire(pending_key, PERSISTENT_CACHE_TTL)
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'buds')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
    except Exception as e:
        app.logger.error(f"Error deleting bud from Redis: {e}")

def _get_bud_items_from_redis(user_id):
    """Get bud_items from Redis."""
    if not app.config.get('REDIS_OK'):
        return None
    try:
        redis_key = f"bud_items:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        return json.loads(cached) if cached else None
    except Exception as e:
        app.logger.error(f"Error getting bud_items from Redis: {e}")
        return None

def _set_bud_items_to_redis(user_id, data):
    """Set bud_items to Redis and mark dirty for flush."""
    if not app.config.get('REDIS_OK'):
        return
    try:
        redis_key = f"bud_items:v1:{user_id}"
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(data, cls=DecimalEncoder))
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'bud_items')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
    except Exception as e:
        app.logger.error(f"Error setting bud_items to Redis: {e}")

def _update_bud_item_in_redis(user_id, bud_item_data):
    """Update or insert a single bud_item record in Redis cache."""
    if not app.config.get('REDIS_OK'):
        return None
    try:
        redis_key = f"bud_items:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        rows = json.loads(cached) if cached else []
        
        item_id = bud_item_data.get('id')
        if item_id:
            found = False
            for i, row in enumerate(rows):
                if int(row.get('id')) == int(item_id):
                    rows[i] = bud_item_data
                    found = True
                    break
            if not found:
                rows.append(bud_item_data)
        else:
            temp_id = -int(time.time() * 1000000)
            bud_item_data['id'] = temp_id
            rows.append(bud_item_data)
        
        _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(rows, cls=DecimalEncoder))
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'bud_items')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
        return bud_item_data['id']
    except Exception as e:
        app.logger.error(f"Error updating bud_item in Redis: {e}")
        return None

def _delete_bud_item_in_redis(user_id, item_id):
    """Delete a bud_item record from Redis cache."""
    if not app.config.get('REDIS_OK'):
        return
    try:
        redis_key = f"bud_items:v1:{user_id}"
        cached = _redis_client.get(redis_key)
        if cached:
            rows = json.loads(cached)
            rows = [row for row in rows if int(row.get('id')) != int(item_id)]
            _redis_client.setex(redis_key, PERSISTENT_CACHE_TTL, json.dumps(rows, cls=DecimalEncoder))
        
        pending_key = f"pending_deletes:bud_items:{user_id}"
        _redis_client.sadd(pending_key, str(item_id))
        _redis_client.expire(pending_key, PERSISTENT_CACHE_TTL)
        dirty_key = f"dirty_tables:{user_id}"
        _redis_client.sadd(dirty_key, 'bud_items')
        _redis_client.expire(dirty_key, PERSISTENT_CACHE_TTL)
    except Exception as e:
        app.logger.error(f"Error deleting bud_item from Redis: {e}")

def update_daily_totals(user_id, start_date, goofy_week_mode, date_to_remainder):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Use covering index (user_id, date) to optimize this query
        cursor.execute("""
            SELECT date FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_dates = [row['date'] for row in cursor.fetchall()]
        
        if not all_dates:
            cursor.close()
            return

        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT remainder FROM totals_remainders_d
            WHERE user_id = %s AND date = %s
        """, (user_id, prev_date))
        prev_remainder_row = cursor.fetchone()
        last_day_remainder = float(prev_remainder_row['remainder']) if prev_remainder_row else 0.0

        # Try to get entries from Redis first
        income_entries = _get_entries_from_redis('income_entries', user_id)
        expense_entries = _get_entries_from_redis('expense_entries', user_id)
        
        # If not in Redis, fall back to MySQL
        if income_entries is None:
            cursor.execute("""
                SELECT ie.* FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
            """, (user_id,))
            income_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(income_entries)} income entries from MySQL")
        
        if expense_entries is None:
            cursor.execute("""
                SELECT ee.* FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
            """, (user_id,))
            expense_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(expense_entries)} expense entries from MySQL")
        
        # Aggregate income by date (filter >= start_date)
        income_by_date = {}
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if entry_date >= start_date:
                income_by_date[entry_date] = income_by_date.get(entry_date, 0) + float(entry.get('amount', 0))
        
        # Aggregate expenses by date (filter >= start_date)
        expense_by_date = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if entry_date >= start_date:
                expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

        # Prepare data for Redis
        redis_updates = []
        
        for current_date in all_dates:
            total_income = income_by_date.get(current_date, 0) + last_day_remainder
            total_expenses = expense_by_date.get(current_date, 0)
            remainder = total_income - total_expenses
            
            redis_updates.append({
                'date': current_date,
                'total_income': float(total_income),
                'total_expenses': float(total_expenses),
                'remainder': float(remainder),
                'last_day_remainder': float(last_day_remainder)
            })
            
            last_day_remainder = remainder
            date_to_remainder[current_date] = remainder
        
        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if redis_updates:
            _update_totals_remainders_in_redis('totals_remainders_d', user_id, redis_updates)
            app.logger.info(f"[REDIS ONLY] Daily totals for user {user_id}: {len(redis_updates)} rows updated in Redis")

def update_weekly_totals(user_id, start_date, goofy_week_mode, date_to_remainder):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Get all relevant Fridays (or week starts) from totals_remainders
        cursor.execute("""
            SELECT date FROM totals_remainders
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_week_dates = [row['date'] for row in cursor.fetchall()]
        
        if not all_week_dates:
            cursor.close()
            return

        # Helper to get week range for a given week date
        def get_week_range(week_date):
            if goofy_week_mode:
                # Goofy: week starts Friday, ends Thursday
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                # Normal: week ends Friday, starts Saturday before
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            return week_start, week_end
        
        # Get the earliest and latest dates needed for our calculation
        earliest_week_date = min(all_week_dates)
        latest_week_date = max(all_week_dates)
        earliest_week_start, _ = get_week_range(earliest_week_date)
        _, latest_week_end = get_week_range(latest_week_date)
        
        # Try to get entries from Redis first
        income_entries = _get_entries_from_redis('income_entries', user_id)
        expense_entries = _get_entries_from_redis('expense_entries', user_id)
        
        # If not in Redis, fall back to MySQL
        if income_entries is None:
            cursor.execute("""
                SELECT ie.* FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
            """, (user_id,))
            income_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(income_entries)} income entries from MySQL")
        
        if expense_entries is None:
            cursor.execute("""
                SELECT ee.* FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
            """, (user_id,))
            expense_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(expense_entries)} expense entries from MySQL")
        
        # Aggregate income by date (filter by date range)
        income_by_date = {}
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if earliest_week_start <= entry_date <= latest_week_end:
                income_by_date[entry_date] = income_by_date.get(entry_date, 0) + float(entry.get('amount', 0))
        
        # Aggregate expenses by date (filter by date range)
        expense_by_date = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if earliest_week_start <= entry_date <= latest_week_end:
                expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

        # Prepare data for Redis
        redis_updates = []
        
        for week_date in all_week_dates:
            week_start, week_end = get_week_range(week_date)

            # Calculate total income for this week
            total_income = 0
            current_date = week_start
            while current_date <= week_end:
                total_income += income_by_date.get(current_date, 0)
                current_date += timedelta(days=1)
            
            # Calculate total expenses for this week
            total_expenses = 0
            current_date = week_start
            while current_date <= week_end:
                total_expenses += expense_by_date.get(current_date, 0)
                current_date += timedelta(days=1)

            # Get last week's remainder
            prev_week_date = week_date - timedelta(days=7)
            last_week_remainder = date_to_remainder.get(prev_week_date, 0)

            # Add last week's remainder to income
            total_income_with_remainder = total_income + float(last_week_remainder)
            week_remainder = total_income_with_remainder - total_expenses

            redis_updates.append({
                'date': week_date,
                'total_income': float(total_income_with_remainder),
                'total_expenses': float(total_expenses),
                'remainder': float(week_remainder),
                'last_week_remainder': float(last_week_remainder)
            })

            # Update date_to_remainder for next week
            date_to_remainder[week_date] = week_remainder
        
        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if redis_updates:
            _update_totals_remainders_in_redis('totals_remainders', user_id, redis_updates)
            app.logger.info(f"[REDIS ONLY] Weekly totals for user {user_id}: {len(redis_updates)} rows updated in Redis")

def update_monthly_totals(user_id, start_date, date_to_remainder):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        import calendar

        # Calculate start of month for the start_date to ensure we get complete month data
        start_of_month = date(start_date.year, start_date.month, 1)
        
        # Get min and max dates from totals_remainders_d to define our query range
        cursor.execute("""
            SELECT MIN(date), MAX(date) FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
        """, (user_id, start_of_month))
        date_range = cursor.fetchone()
        
        if not date_range or not date_range['MIN(date)']:
            cursor.close()
            return
            
        min_date, max_date = date_range['MIN(date)'], date_range['MAX(date)']
        
        # Determine all relevant months in the date range
        months_data = []
        current_year, current_month = min_date.year, min_date.month
        end_year, end_month = max_date.year, max_date.month
        
        while (current_year < end_year) or (current_year == end_year and current_month <= end_month):
            last_day = date(current_year, current_month, calendar.monthrange(current_year, current_month)[1])
            first_day = date(current_year, current_month, 1)
            
            months_data.append({
                'year': current_year,
                'month': current_month,
                'first_day': first_day,
                'last_day': last_day
            })
            
            # Move to next month
            if current_month == 12:
                current_month = 1
                current_year += 1
            else:
                current_month += 1
        
        # Filter to only include months that start on or after our start date
        months_data = [m for m in months_data if m['first_day'] >= start_of_month]
        
        if not months_data:
            cursor.close()
            return
        
        # Try to get entries from Redis first
        income_entries = _get_entries_from_redis('income_entries', user_id)
        expense_entries = _get_entries_from_redis('expense_entries', user_id)
        
        # If not in Redis, fall back to MySQL
        if income_entries is None:
            cursor.execute("""
                SELECT ie.* FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
            """, (user_id,))
            income_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(income_entries)} income entries from MySQL")
        
        if expense_entries is None:
            cursor.execute("""
                SELECT ee.* FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
            """, (user_id,))
            expense_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(expense_entries)} expense entries from MySQL")
        
        # Aggregate income by month (filter by date range)
        income_by_month = {}
        first_day = months_data[0]['first_day']
        last_day = months_data[-1]['last_day']
        
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if first_day <= entry_date <= last_day:
                key = (entry_date.year, entry_date.month)
                income_by_month[key] = income_by_month.get(key, 0) + float(entry.get('amount', 0))
        
        # Aggregate expenses by month (filter by date range)
        expense_by_month = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if first_day <= entry_date <= last_day:
                key = (entry_date.year, entry_date.month)
                expense_by_month[key] = expense_by_month.get(key, 0) + float(entry.get('amount', 0))

        # Prepare data for Redis
        redis_updates = []
        
        for month_info in months_data:
            year = month_info['year']
            month = month_info['month']
            last_day_of_month = month_info['last_day']
            
            # Get income and expense totals for this month
            total_income = income_by_month.get((year, month), 0.0)
            total_expenses = expense_by_month.get((year, month), 0.0)
            
            # Get last month's remainder
            prev_month = (month - 1) or 12
            prev_year = year if month > 1 else year - 1
            
            if (prev_year, prev_month) == (months_data[0]['year'], months_data[0]['month']):
                # First month in our data, get from database
                cursor.execute("""
                    SELECT remainder FROM totals_remainders_m
                    WHERE user_id = %s AND YEAR(date) = %s AND MONTH(date) = %s
                    ORDER BY date DESC LIMIT 1
                """, (user_id, prev_year, prev_month))
                prev_remainder_row = cursor.fetchone()
                last_month_remainder = float(prev_remainder_row['remainder']) if prev_remainder_row else 0.0
            else:
                # Get from our calculated values
                prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
                last_month_remainder = date_to_remainder.get(prev_last_day, 0.0)

            # Calculate totals with remainder
            total_income_with_remainder = total_income + last_month_remainder
            remainder = total_income_with_remainder - total_expenses
            
            # Store for Redis
            redis_updates.append({
                'date': last_day_of_month,
                'total_income': float(total_income_with_remainder),
                'total_expenses': float(total_expenses),
                'remainder': float(remainder),
                'last_month_remainder': float(last_month_remainder)
            })
            
            # Save for next month's calculation
            date_to_remainder[last_day_of_month] = remainder
        
        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if redis_updates:
            _update_totals_remainders_in_redis('totals_remainders_m', user_id, redis_updates)
            app.logger.info(f"[REDIS ONLY] Monthly totals for user {user_id}: {len(redis_updates)} rows updated in Redis")

def update_daily_savings_for_savings_category(user_id, start_date):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Get member_since and starting_savings for this user
        cursor.execute("SELECT member_since, starting_savings FROM users WHERE id = %s", (user_id,))
        user_row = cursor.fetchone()
        if user_row and user_row['member_since']:
            member_since = user_row['member_since']
            starting_savings = float(user_row['starting_savings']) if user_row['starting_savings'] is not None else 0.0
        else:
            member_since = start_date
            starting_savings = 0.0

        # Get the "Savings" income and expense category IDs for this user in one query
        cursor.execute("""
            SELECT 'income' as type, id FROM income_categories 
            WHERE user_id = %s AND name = 'Savings'
            UNION ALL
            SELECT 'expense' as type, id FROM expense_categories 
            WHERE user_id = %s AND name = 'Savings'
        """, (user_id, user_id))
        
        results = cursor.fetchall()
        income_savings_id = None
        expense_savings_id = None
        
        for row in results:
            if row['type'] == 'income':
                income_savings_id = row['id']
            else:
                expense_savings_id = row['id']

        if not income_savings_id and not expense_savings_id:
            cursor.close()
            return  # No savings categories found

        # Get all dates to update, in order
        cursor.execute("""
            SELECT date FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_dates = [row['date'] for row in cursor.fetchall()]
        
        if not all_dates:
            cursor.close()
            return

        # Get previous day's savings
        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT amount FROM savings_entries
            WHERE user_id = %s AND date = %s
        """, (user_id, prev_date))
        prev_savings_row = cursor.fetchone()
        last_savings = float(prev_savings_row['amount']) if prev_savings_row else 0.0

        # Get all income and expense data for the full date range in one query each
        min_date = min(all_dates)
        max_date = max(all_dates)
        
        # Try to get entries from Redis first
        income_entries = _get_entries_from_redis('income_entries', user_id)
        expense_entries = _get_entries_from_redis('expense_entries', user_id)
        
        # If not in Redis, fall back to MySQL
        if income_entries is None:
            cursor.execute("""
                SELECT * FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
            """, (user_id,))
            income_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(income_entries)} income entries from MySQL for savings")
        
        if expense_entries is None:
            cursor.execute("""
                SELECT * FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
            """, (user_id,))
            expense_entries = list(cursor.fetchall())
            app.logger.debug(f"[REDIS MISS] Loaded {len(expense_entries)} expense entries from MySQL for savings")
        
        # Filter and aggregate income by date
        income_by_date = {}
        if income_savings_id:
            for entry in income_entries:
                if int(entry.get('category_id', 0)) == int(income_savings_id):
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        income_by_date[entry_date] = income_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))
        
        # Filter and aggregate expenses by date
        expense_by_date = {}
        if expense_savings_id:
            for entry in expense_entries:
                if int(entry.get('category_id', 0)) == int(expense_savings_id):
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        expense_by_date[entry_date] = expense_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))
        
        # Prepare data for Redis
        redis_updates = []
        
        for current_date in all_dates:
            # Get income and expense totals for this date
            total_income = income_by_date.get(current_date, 0.0)
            total_expenses = expense_by_date.get(current_date, 0.0)

            # Only add starting_savings on the member_since date
            if current_date == member_since:
                savings = last_savings + total_expenses - total_income + starting_savings
            else:
                savings = last_savings + total_expenses - total_income
                
            redis_updates.append({
                'date': current_date,
                'amount': float(savings)
            })
            last_savings = savings
        
        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if redis_updates:
            # For savings, we need to update the full cache
            _set_savings_entries_to_redis(user_id, redis_updates)
            app.logger.info(f"[REDIS ONLY] Savings entries for user {user_id}: {len(redis_updates)} rows updated in Redis")

def update_daily_ca_totals(user_id, start_date):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Get all credit accounts for this user
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row['id'] for row in cursor.fetchall()]
        if not account_ids:
            cursor.close()
            return

        # Collect all updates for aggregation
        all_redis_updates = []

        # Process each account with optimized queries
        for account_id in account_ids:
            # Get the date range we need to process
            cursor.execute("""
                SELECT MIN(date) as min_date, MAX(date) as max_date FROM c_a_balances_d
                WHERE account_id = %s AND date >= %s
            """, (account_id, start_date))
            date_range = cursor.fetchone()
            
            if not date_range or not date_range['min_date']:
                continue
                
            min_date, max_date = date_range['min_date'], date_range['max_date']
            
            # Get all dates within the range
            cursor.execute("""
                SELECT date FROM c_a_balances_d
                WHERE account_id = %s AND date BETWEEN %s AND %s
                ORDER BY date ASC
            """, (account_id, min_date, max_date))
            all_dates = [row['date'] for row in cursor.fetchall()]
            
            if not all_dates:
                continue

            # Get previous balance - try Redis first, then MySQL
            prev_date = min_date - timedelta(days=1)
            last_day_balance = 0.0
            
            # Try to get from Redis cache first
            cached_balances = _get_ca_balances_from_redis('c_a_balances_d', user_id, account_id=account_id)
            if cached_balances:
                # Find the previous date's balance
                for bal in cached_balances:
                    bal_date = bal.get('date')
                    if isinstance(bal_date, str):
                        bal_date = datetime.strptime(bal_date, '%Y-%m-%d').date()
                    if bal_date == prev_date:
                        last_day_balance = float(bal.get('balance', 0))
                        app.logger.debug(f"[REDIS HIT] Previous balance for {prev_date}: {last_day_balance}")
                        break
                else:
                    # Not found in Redis, try MySQL
                    cursor.execute("""
                        SELECT balance FROM c_a_balances_d
                        WHERE account_id = %s AND date = %s
                    """, (account_id, prev_date))
                    prev_balance_row = cursor.fetchone()
                    last_day_balance = float(prev_balance_row['balance']) if prev_balance_row and prev_balance_row['balance'] is not None else 0.0
                    app.logger.debug(f"[MYSQL] Previous balance for {prev_date}: {last_day_balance}")
            else:
                # Redis miss, query MySQL
                cursor.execute("""
                    SELECT balance FROM c_a_balances_d
                    WHERE account_id = %s AND date = %s
                """, (account_id, prev_date))
                prev_balance_row = cursor.fetchone()
                last_day_balance = float(prev_balance_row['balance']) if prev_balance_row and prev_balance_row['balance'] is not None else 0.0
                app.logger.debug(f"[MYSQL FALLBACK] Previous balance for {prev_date}: {last_day_balance}")

            # Try to get expenses from Redis first
            c_expense_entries = _get_entries_from_redis('c_expense_entries', user_id)
            
            if c_expense_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_daily_ca_totals c_expense_entries for user {user_id}")
                cursor.execute("""
                    SELECT cee.* FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                c_expense_entries = list(cursor.fetchall())
            else:
                app.logger.debug(f"[REDIS HIT] update_daily_ca_totals c_expense_entries for user {user_id}")
            
            # Filter and aggregate expenses by date for this account
            expense_by_date = {}
            for entry in c_expense_entries:
                entry_date = entry.get('date')
                if isinstance(entry_date, str):
                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                
                # Get category to check account_id
                entry_category_id = entry.get('category_id')
                cursor.execute("SELECT account_id FROM c_expense_categories WHERE id = %s", (entry_category_id,))
                cat_row = cursor.fetchone()
                
                if cat_row and cat_row['account_id'] == account_id and min_date <= entry_date <= max_date:
                    expense_by_date[entry_date] = expense_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))

            # Try to get payment entries from Redis first
            c_payment_entries = _get_payment_entries_from_redis(user_id)
            
            if c_payment_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_daily_ca_totals c_payment_entries for user {user_id}")
                cursor.execute("""
                    SELECT date, SUM(amount) as daily_total 
                    FROM c_payment_entries
                    WHERE account_id = %s AND date BETWEEN %s AND %s
                    GROUP BY date
                    ORDER BY date
                """, (account_id, min_date, max_date))
                payments_by_date = {row['date']: float(row['daily_total']) for row in cursor.fetchall()}
            else:
                app.logger.debug(f"[REDIS HIT] update_daily_ca_totals c_payment_entries for user {user_id}")
                # Filter and aggregate payments by date for this account
                payments_by_date = {}
                for entry in c_payment_entries:
                    entry_account_id = entry.get('account_id')
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    
                    if entry_account_id == account_id and min_date <= entry_date <= max_date:
                        payments_by_date[entry_date] = payments_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))

            # Prepare data for Redis
            redis_updates = []
            
            for current_date in all_dates:
                total_expenses = expense_by_date.get(current_date, 0.0)
                total_payments = payments_by_date.get(current_date, 0.0)
                balance = last_day_balance + total_expenses - total_payments
                
                redis_updates.append({
                    'account_id': account_id,
                    'date': current_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                last_day_balance = balance
            
            # Store for later aggregation
            if redis_updates:
                all_redis_updates.extend(redis_updates)

        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if all_redis_updates:
            _set_ca_balances_to_redis('c_a_balances_d', user_id, all_redis_updates)
            app.logger.info(f"[REDIS ONLY] CA daily balances for user {user_id}: {len(all_redis_updates)} rows updated in Redis")

def update_weekly_ca_totals(user_id, start_date, goofy_week_mode=False):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Get all credit accounts for this user
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row['id'] for row in cursor.fetchall()]
        if not account_ids:
            cursor.close()
            return

        # Collect all updates for aggregation
        all_redis_updates = []

        # Helper function to get week range
        def get_week_range(week_date):
            if goofy_week_mode:
                # Goofy: week starts Friday, ends Thursday
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                # Normal: week ends Friday, starts Saturday before
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            return week_start, week_end

        for account_id in account_ids:
            # Get all relevant week dates
            cursor.execute("""
                SELECT date FROM c_a_balances
                WHERE account_id = %s AND date >= %s
                ORDER BY date ASC
            """, (account_id, start_date))
            all_week_dates = [row['date'] for row in cursor.fetchall()]
            
            if not all_week_dates:
                continue
                
            # Calculate the full date range needed
            earliest_week = min(all_week_dates)
            latest_week = max(all_week_dates)
            earliest_start, _ = get_week_range(earliest_week)
            _, latest_end = get_week_range(latest_week)
            
            # Precompute all week ranges
            week_ranges = {}
            for week_date in all_week_dates:
                week_ranges[week_date] = get_week_range(week_date)
            
            # Try to get expenses from Redis first
            c_expense_entries = _get_entries_from_redis('c_expense_entries', user_id)
            
            if c_expense_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_weekly_ca_totals c_expense_entries for user {user_id}")
                cursor.execute("""
                    SELECT cee.* FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                c_expense_entries = list(cursor.fetchall())
            else:
                app.logger.debug(f"[REDIS HIT] update_weekly_ca_totals c_expense_entries for user {user_id}")
            
            # Filter and aggregate expenses by date for this account
            expenses_by_date = {}
            for entry in c_expense_entries:
                entry_date = entry.get('date')
                if isinstance(entry_date, str):
                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                
                # Get category to check account_id
                entry_category_id = entry.get('category_id')
                cursor.execute("SELECT account_id FROM c_expense_categories WHERE id = %s", (entry_category_id,))
                cat_row = cursor.fetchone()
                
                if cat_row and cat_row['account_id'] == account_id and earliest_start <= entry_date <= latest_end:
                    expenses_by_date[entry_date] = expenses_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))
            
            # Try to get payment entries from Redis first
            c_payment_entries = _get_payment_entries_from_redis(user_id)
            
            if c_payment_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_weekly_ca_totals c_payment_entries for user {user_id}")
                cursor.execute("""
                    SELECT date, SUM(amount) as daily_payment
                    FROM c_payment_entries
                    WHERE account_id = %s AND date BETWEEN %s AND %s
                    GROUP BY date
                    ORDER BY date
                """, (account_id, earliest_start, latest_end))
                payments_by_date = {row['date']: float(row['daily_payment']) for row in cursor.fetchall()}
            else:
                app.logger.debug(f"[REDIS HIT] update_weekly_ca_totals c_payment_entries for user {user_id}")
                # Filter and aggregate payments by date for this account
                payments_by_date = {}
                for entry in c_payment_entries:
                    entry_account_id = entry.get('account_id')
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    
                    if entry_account_id == account_id and earliest_start <= entry_date <= latest_end:
                        payments_by_date[entry_date] = payments_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))
            
            # Calculate previous week balances (done first to avoid multiple queries in the loop)
            # Try Redis first for all balances
            cached_balances = _get_ca_balances_from_redis('c_a_balances', user_id, account_id=account_id)
            
            prev_balances = {}
            for week_date in all_week_dates:
                prev_week = week_date - timedelta(days=7)
                if prev_week < start_date:
                    balance_found = False
                    
                    # Check Redis cache first
                    if cached_balances:
                        for bal in cached_balances:
                            bal_date = bal.get('date')
                            if isinstance(bal_date, str):
                                bal_date = datetime.strptime(bal_date, '%Y-%m-%d').date()
                            if bal_date == prev_week:
                                prev_balances[week_date] = float(bal.get('balance', 0))
                                balance_found = True
                                break
                    
                    # Fallback to MySQL if not in Redis
                    if not balance_found:
                        cursor.execute("""
                            SELECT balance FROM c_a_balances
                            WHERE account_id = %s AND date = %s
                        """, (account_id, prev_week))
                        prev_row = cursor.fetchone()
                        prev_balances[week_date] = float(prev_row['balance']) if prev_row and prev_row['balance'] is not None else 0.0
            
            # Prepare data for Redis
            redis_updates = []
            for week_date in sorted(all_week_dates):
                week_start, week_end = week_ranges[week_date]
                
                # Sum expenses for this week
                total_expenses = 0.0
                current_date = week_start
                while current_date <= week_end:
                    total_expenses += expenses_by_date.get(current_date, 0.0)
                    current_date += timedelta(days=1)
                    
                # Sum payments for this week
                total_payments = 0.0
                current_date = week_start
                while current_date <= week_end:
                    total_payments += payments_by_date.get(current_date, 0.0)
                    current_date += timedelta(days=1)
                
                # Get previous week's balance
                prev_week_date = week_date - timedelta(days=7)
                # Use precalculated value if available, otherwise use calculated value
                last_week_balance = prev_balances.get(week_date, prev_balances.get(prev_week_date, 0.0))
                
                # Calculate new balance
                balance = last_week_balance + total_expenses - total_payments
                
                # Store for Redis
                redis_updates.append({
                    'account_id': account_id,
                    'date': week_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                # Save for subsequent weeks
                prev_balances[week_date] = balance
            
            # Add to aggregated updates
            if redis_updates:
                all_redis_updates.extend(redis_updates)

        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if all_redis_updates:
            _set_ca_balances_to_redis('c_a_balances', user_id, all_redis_updates)
            app.logger.info(f"[REDIS ONLY] CA weekly balances for user {user_id}: {len(all_redis_updates)} rows updated in Redis")

def update_monthly_ca_totals(user_id, start_date):
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        import calendar

        # Get all credit accounts for this user
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row['id'] for row in cursor.fetchall()]
        if not account_ids:
            cursor.close()
            return

        # Collect all updates for aggregation
        all_redis_updates = []

        for account_id in account_ids:
            # Get date range to process
            cursor.execute("""
                SELECT MIN(date) as min_date, MAX(date) as max_date FROM c_a_balances_m
                WHERE account_id = %s AND date >= %s
            """, (account_id, start_date))
            date_range = cursor.fetchone()
            
            if not date_range or not date_range['min_date']:
                continue
                
            min_date, max_date = date_range['min_date'], date_range['max_date']
            
            # Get all month-end dates
            cursor.execute("""
                SELECT date FROM c_a_balances_m
                WHERE account_id = %s AND date BETWEEN %s AND %s
                ORDER BY date ASC
            """, (account_id, min_date, max_date))
            all_months = [row['date'] for row in cursor.fetchall()]
            
            if not all_months:
                continue
            
            # Create a list of month info with start/end dates
            month_data = []
            for last_day in all_months:
                month_start = date(last_day.year, last_day.month, 1)
                month_data.append({
                    'last_day': last_day,
                    'start_date': month_start,
                    'end_date': last_day,
                    'year': last_day.year,
                    'month': last_day.month
                })
                
            # Get the entire date range for expense/payment queries
            earliest_start = min(m['start_date'] for m in month_data)
            latest_end = max(m['end_date'] for m in month_data)
            
            # Try to get expenses from Redis first
            c_expense_entries = _get_entries_from_redis('c_expense_entries', user_id)
            
            if c_expense_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_monthly_ca_totals c_expense_entries for user {user_id}")
                cursor.execute("""
                    SELECT cee.* FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                c_expense_entries = list(cursor.fetchall())
            else:
                app.logger.debug(f"[REDIS HIT] update_monthly_ca_totals c_expense_entries for user {user_id}")
            
            # Filter and aggregate expenses by month for this account
            expenses_by_month = {}
            for entry in c_expense_entries:
                entry_date = entry.get('date')
                if isinstance(entry_date, str):
                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                
                # Get category to check account_id
                entry_category_id = entry.get('category_id')
                cursor.execute("SELECT account_id FROM c_expense_categories WHERE id = %s", (entry_category_id,))
                cat_row = cursor.fetchone()
                
                if cat_row and cat_row['account_id'] == account_id and earliest_start <= entry_date <= latest_end:
                    year_month = (entry_date.year, entry_date.month)
                    expenses_by_month[year_month] = expenses_by_month.get(year_month, 0.0) + float(entry.get('amount', 0))
            
            # Try to get payment entries from Redis first
            c_payment_entries = _get_payment_entries_from_redis(user_id)
            
            if c_payment_entries is None:
                # Redis miss - fallback to MySQL
                app.logger.debug(f"[REDIS MISS] update_monthly_ca_totals c_payment_entries for user {user_id}")
                cursor.execute("""
                    SELECT YEAR(date) as year, MONTH(date) as month, SUM(amount) as month_total
                    FROM c_payment_entries
                    WHERE account_id = %s AND date BETWEEN %s AND %s
                    GROUP BY YEAR(date), MONTH(date)
                """, (account_id, earliest_start, latest_end))
                payments_by_month = {(row['year'], row['month']): float(row['month_total']) for row in cursor.fetchall()}
            else:
                app.logger.debug(f"[REDIS HIT] update_monthly_ca_totals c_payment_entries for user {user_id}")
                # Filter and aggregate payments by month for this account
                payments_by_month = {}
                for entry in c_payment_entries:
                    entry_account_id = entry.get('account_id')
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    
                    if entry_account_id == account_id and earliest_start <= entry_date <= latest_end:
                        year_month = (entry_date.year, entry_date.month)
                        payments_by_month[year_month] = payments_by_month.get(year_month, 0.0) + float(entry.get('amount', 0))
            
            # Calculate all previous month balances upfront to avoid multiple queries
            # Try Redis first for all balances
            cached_balances = _get_ca_balances_from_redis('c_a_balances_m', user_id, account_id=account_id)
            
            prev_balances = {}
            for month_info in month_data:
                # For the first month or months starting from our actual start_date
                if month_info['start_date'] == earliest_start:
                    # Calculate previous month date
                    if month_info['month'] == 1:
                        prev_month = 12
                        prev_year = month_info['year'] - 1
                    else:
                        prev_month = month_info['month'] - 1
                        prev_year = month_info['year']
                    
                    prev_last_day = date(prev_year, prev_month, 
                                         calendar.monthrange(prev_year, prev_month)[1])
                    
                    balance_found = False
                    
                    # Check Redis cache first
                    if cached_balances:
                        for bal in cached_balances:
                            bal_date = bal.get('date')
                            if isinstance(bal_date, str):
                                bal_date = datetime.strptime(bal_date, '%Y-%m-%d').date()
                            if bal_date == prev_last_day:
                                prev_balances[(month_info['year'], month_info['month'])] = float(bal.get('balance', 0))
                                balance_found = True
                                break
                    
                    # Fallback to MySQL if not in Redis
                    if not balance_found:
                        cursor.execute("""
                            SELECT balance FROM c_a_balances_m
                            WHERE account_id = %s AND date = %s
                        """, (account_id, prev_last_day))
                        prev_row = cursor.fetchone()
                        prev_balances[(month_info['year'], month_info['month'])] = (
                            float(prev_row['balance']) if prev_row and prev_row['balance'] is not None else 0.0
                    )
            
            # Prepare data for Redis
            redis_updates = []
            
            for month_info in sorted(month_data, key=lambda m: (m['year'], m['month'])):
                # Get expenses and payments for this month
                total_expenses = expenses_by_month.get(
                    (month_info['year'], month_info['month']), 0.0)
                total_payments = payments_by_month.get(
                    (month_info['year'], month_info['month']), 0.0)
                
                # Get previous month's balance
                if month_info['month'] == 1:
                    prev_month = 12
                    prev_year = month_info['year'] - 1
                else:
                    prev_month = month_info['month'] - 1
                    prev_year = month_info['year']
                
                # Get from our calculated values if available
                last_month_balance = prev_balances.get(
                    (month_info['year'], month_info['month']),
                    prev_balances.get((prev_year, prev_month), 0.0)
                )
                
                # Calculate new balance
                balance = last_month_balance + total_expenses - total_payments
                
                # Store for Redis
                redis_updates.append({
                    'account_id': account_id,
                    'date': month_info['last_day'],
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                # Save for next month's calculation
                prev_balances[(month_info['year'], month_info['month'])] = balance
            
            # Add to aggregated updates
            if redis_updates:
                all_redis_updates.extend(redis_updates)

        cursor.close()
        
        # Update Redis cache only - flush workers will persist to MySQL
        if all_redis_updates:
            _set_ca_balances_to_redis('c_a_balances_m', user_id, all_redis_updates)
            app.logger.info(f"[REDIS ONLY] CA monthly balances for user {user_id}: {len(all_redis_updates)} rows updated in Redis")

@app.route('/save_totals_remainders_d', methods=['POST'])
@login_required
def save_totals_remainders_d():
    try:
        data = request.get_json(silent=True) or {}
        start_date_str = data.get('start_date')
        user_id = current_user.id

        # Fetch goofy_week_mode for the current user
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT goofy_week_mode FROM users WHERE id = %s", (user_id,))
            goofy_week_mode = bool(cursor.fetchone()[0])
            cursor.close()

        # Determine the starting date for incremental update
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except Exception:
                return jsonify({"status": "error", "message": "Invalid start_date format"}), 400
        else:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MIN(date) FROM totals_remainders_d WHERE user_id = %s", (user_id,))
                min_date_row = cursor.fetchone()
                start_date = min_date_row[0] if min_date_row and min_date_row[0] else date.today()
                cursor.close()

        date_to_remainder = {}

        # Run daily, weekly, and monthly updates (these now update Redis automatically)
        update_daily_totals(user_id, start_date, goofy_week_mode, date_to_remainder)
        update_daily_savings_for_savings_category(user_id, start_date)
        update_weekly_totals(user_id, start_date, goofy_week_mode, date_to_remainder)
        update_monthly_totals(user_id, start_date, date_to_remainder)

        # Try to fetch from Redis first, fallback to MySQL
        cached_daily = _get_totals_remainders_from_redis('totals_remainders_d', user_id, start_date)
        cached_weekly = _get_totals_remainders_from_redis('totals_remainders', user_id, start_date)
        cached_monthly = _get_totals_remainders_from_redis('totals_remainders_m', user_id, start_date)
        cached_savings = _get_savings_entries_from_redis(user_id, start_date)
        
        if cached_daily and cached_weekly and cached_monthly and cached_savings:
            # Redis hit - use cached data
            app.logger.info(f"[REDIS HIT] save_totals_remainders_d for user {user_id}")
            
            # Enrich daily totals with last_week_remainder
            results = []
            weekly_by_date = {row['date']: row for row in cached_weekly}
            
            for daily_row in cached_daily:
                current_date = datetime.strptime(daily_row['date'], '%Y-%m-%d').date() if isinstance(daily_row['date'], str) else daily_row['date']
                
                # Find the most recent previous Friday
                if goofy_week_mode:
                    prev_friday = current_date - timedelta(days=(current_date.weekday() - 4) % 7 or 7)
                else:
                    prev_friday = current_date - timedelta(days=7)
                
                prev_friday_str = prev_friday.isoformat()
                last_week_remainder = float(weekly_by_date.get(prev_friday_str, {}).get('remainder', 0.0))
                
                result = {
                    'date': current_date if isinstance(current_date, date) else datetime.strptime(current_date, '%Y-%m-%d').date(),
                    'total_income': float(daily_row.get('total_income', 0)),
                    'total_expenses': float(daily_row.get('total_expenses', 0)),
                    'remainder': float(daily_row.get('remainder', 0)),
                    'last_day_remainder': float(daily_row.get('last_day_remainder', 0)),
                    'last_week_remainder': last_week_remainder
                }
                results.append(result)
            
            # Format monthly results
            monthly_results = [
                {
                    'date': datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date'],
                    'total_income': float(row.get('total_income', 0)),
                    'total_expenses': float(row.get('total_expenses', 0)),
                    'remainder': float(row.get('remainder', 0)),
                    'last_month_remainder': float(row.get('last_month_remainder', 0))
                }
                for row in cached_monthly
            ]
            
            # Format savings entries
            savings_entries = [
                {
                    'date': datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date'],
                    'amount': float(row.get('amount', 0))
                }
                for row in cached_savings
            ]
            
            return jsonify({
                "status": "success",
                "updated_totals_remainders": results,
                "updated_monthly_totals_remainders": monthly_results,
                "updated_savings_entries": savings_entries
            })
        
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] save_totals_remainders_d for user {user_id}, falling back to MySQL")
        
        # Prepare the response: for each date, fetch last_week_remainder from totals_remainders table
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT date FROM totals_remainders_d
                WHERE user_id = %s AND date >= %s
                ORDER BY date ASC
            """, (user_id, start_date))
            all_dates = [row[0] for row in cursor.fetchall()]

            results = []
            for current_date in all_dates:
                # Find the most recent previous Friday
                if goofy_week_mode:
                    prev_friday = current_date - timedelta(days=(current_date.weekday() - 4) % 7 or 7)
                else:
                    prev_friday = current_date - timedelta(days=7)

                cursor.execute("""
                    SELECT remainder FROM totals_remainders
                    WHERE user_id = %s AND date = %s
                """, (user_id, prev_friday))
                last_week_remainder_row = cursor.fetchone()
                last_week_remainder = float(last_week_remainder_row[0]) if last_week_remainder_row else 0.0

                cursor.execute("""
                    SELECT total_income, total_expenses, remainder, last_day_remainder
                    FROM totals_remainders_d
                    WHERE user_id = %s AND date = %s
                """, (user_id, current_date))
                row = cursor.fetchone()
                if not row:
                    continue

                result = {
                    'date': current_date,
                    'total_income': float(row[0]),
                    'total_expenses': float(row[1]),
                    'remainder': float(row[2]),
                    'last_day_remainder': float(row[3]),
                    'last_week_remainder': float(last_week_remainder)
                }
                results.append(result)

            # Fetch updated monthly totals
            cursor.execute("""
                SELECT date, total_income, total_expenses, remainder, last_month_remainder
                FROM totals_remainders_m
                WHERE user_id = %s AND date >= %s
                ORDER BY date ASC
            """, (user_id, start_date))
            monthly_results = [
                {
                    'date': row[0],
                    'total_income': float(row[1]),
                    'total_expenses': float(row[2]),
                    'remainder': float(row[3]),
                    'last_month_remainder': float(row[4])
                }
                for row in cursor.fetchall()
            ]

            # --- Fetch updated savings entries ---
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s AND date >= %s
                ORDER BY date ASC
            """, (user_id, start_date))
            savings_entries = [
                {'date': row[0], 'amount': float(row[1])}
                for row in cursor.fetchall()
            ]
            cursor.close()

        return jsonify({
            "status": "success",
            "updated_totals_remainders": results,
            "updated_monthly_totals_remainders": monthly_results,
            "updated_savings_entries": savings_entries
        })

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/save_ca_daily_balance', methods=['POST'])
@login_required
def save_ca_daily_balance():
    try:
        data = request.get_json(silent=True) or {}
        start_date_str = data.get('start_date')
        user_id = current_user.id

        # Fetch goofy_week_mode for the current user
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT goofy_week_mode FROM users WHERE id = %s", (user_id,))
            goofy_week_mode = bool(cursor.fetchone()[0])
            cursor.close()

        # Determine the starting date for incremental update
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except Exception:
                return jsonify({"status": "error", "message": "Invalid start_date format"}), 400
        else:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT MIN(date) FROM c_a_balances_d
                    WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                """, (user_id,))
                min_date_row = cursor.fetchone()
                start_date = min_date_row[0] if min_date_row and min_date_row[0] else date.today()
                cursor.close()

        # Update CA balances (daily, weekly, monthly) - these now update Redis automatically
        update_daily_ca_totals(user_id, start_date)
        update_weekly_ca_totals(user_id, start_date, goofy_week_mode)
        update_monthly_ca_totals(user_id, start_date)

        # Try to fetch from Redis first, fallback to MySQL
        cached_daily = _get_ca_balances_from_redis('c_a_balances_d', user_id, start_date=start_date)
        cached_weekly = _get_ca_balances_from_redis('c_a_balances', user_id, start_date=start_date)
        cached_monthly = _get_ca_balances_from_redis('c_a_balances_m', user_id, start_date=start_date)
        
        if cached_daily and cached_weekly and cached_monthly:
            # Redis hit - use cached data
            app.logger.info(f"[REDIS HIT] save_ca_daily_balance for user {user_id}")
            
            # Format daily balances
            ca_balances_d = [
                {
                    'id': row.get('id'),
                    'account_id': row.get('account_id'),
                    'date': datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date'],
                    'total_expenses': float(row.get('total_expenses', 0)),
                    'balance': float(row.get('balance', 0))
                }
                for row in cached_daily
            ]
            
            # Format weekly balances
            ca_balances = [
                {
                    'id': row.get('id'),
                    'account_id': row.get('account_id'),
                    'date': datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date'],
                    'total_expenses': float(row.get('total_expenses', 0)),
                    'balance': float(row.get('balance', 0))
                }
                for row in cached_weekly
            ]
            
            # Format monthly balances
            ca_balances_m = [
                {
                    'id': row.get('id'),
                    'account_id': row.get('account_id'),
                    'date': datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date'],
                    'total_expenses': float(row.get('total_expenses', 0)),
                    'balance': float(row.get('balance', 0))
                }
                for row in cached_monthly
            ]
            
            return jsonify({
                "status": "success",
                "updated_ca_balances_d": ca_balances_d,
                "updated_ca_balances": ca_balances,
                "updated_ca_balances_m": ca_balances_m
            })
        
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] save_ca_daily_balance for user {user_id}, falling back to MySQL")
        
        # Fetch updated daily CA balances
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                ) AND date >= %s
                ORDER BY account_id ASC, date ASC
            """, (user_id, start_date))
            ca_balances_d = [
                {
                    'id': row[0],
                    'account_id': row[1],
                    'date': row[2],
                    'total_expenses': float(row[3]) if row[3] is not None else 0.0,
                    'balance': float(row[4]) if row[4] is not None else 0.0
                }
                for row in cursor.fetchall()
            ]

            # Fetch updated weekly CA balances
            cursor.execute("""
                SELECT * FROM c_a_balances
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                ) AND date >= %s
                ORDER BY account_id ASC, date ASC
            """, (user_id, start_date))
            ca_balances = [
                {
                    'id': row[0],
                    'account_id': row[1],
                    'date': row[2],
                    'total_expenses': float(row[3]) if row[3] is not None else 0.0,
                    'balance': float(row[4]) if row[4] is not None else 0.0
                }
                for row in cursor.fetchall()
            ]

            # Fetch updated monthly CA balances
            cursor.execute("""
                SELECT * FROM c_a_balances_m
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                ) AND date >= %s
                ORDER BY account_id ASC, date ASC
            """, (user_id, start_date))
            ca_balances_m = [
                {
                    'id': row[0],
                    'account_id': row[1],
                    'date': row[2],
                    'total_expenses': float(row[3]) if row[3] is not None else 0.0,
                    'balance': float(row[4]) if row[4] is not None else 0.0
                }
                for row in cursor.fetchall()
            ]
            cursor.close()

        return jsonify({
            "status": "success",
            "updated_ca_balances_d": ca_balances_d,
            "updated_ca_balances": ca_balances,
            "updated_ca_balances_m": ca_balances_m
        })

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/move_entry_d', methods=['POST'])
@login_required
def move_entry_d():
    from redis_crud import get_entries, bulk_update_entries
    
    data = request.get_json()
    entry_id = data.get('entry_id')
    new_date = data.get('new_date')
    entry_type = data.get('type')  # 'income', 'expense', or 'ca'

    if not entry_id or not new_date or not entry_type:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    try:
        ca_triggered = False
        category_id = None
        is_savings_category = False
        
        # Determine table name
        if entry_type == 'income':
            table_name = 'income_entries'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
        elif entry_type == 'ca':
            table_name = 'c_expense_entries'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400
        
        # Get all entries using Redis-first approach
        entries = get_entries(table_name, user_id=current_user.id)
        
        # Find the entry to move
        entry_to_move = None
        for entry in entries:
            if str(entry.get('id')) == str(entry_id):
                entry_to_move = entry
                break
        
        if not entry_to_move:
            return jsonify({'status': 'error', 'message': 'Entry not found'}), 404
        
        category_id = entry_to_move.get('category_id')
        amount = Decimal(str(entry_to_move.get('amount', 0)))
        old_date = entry_to_move.get('date')
        if isinstance(old_date, str):
            old_date_str = old_date
        else:
            old_date_str = old_date.strftime('%Y-%m-%d') if hasattr(old_date, 'strftime') else str(old_date)
        
        app.logger.info(f"[MOVE ENTRY] Moving {entry_type} entry {entry_id} from {old_date_str} to {new_date}, category {category_id}, amount {amount}")
        
        # Check if entry exists at new date
        existing_at_new_date = None
        for entry in entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date_str = entry_date
            else:
                entry_date_str = entry_date.strftime('%Y-%m-%d') if hasattr(entry_date, 'strftime') else str(entry_date)
            
            if str(entry.get('category_id')) == str(category_id) and entry_date_str == new_date:
                existing_at_new_date = entry
                break
        
        if existing_at_new_date:
            # Add to existing entry at new date
            new_amount = Decimal(str(existing_at_new_date.get('amount', 0))) + amount
            app.logger.info(f"[MOVE ENTRY] Merging with existing entry at {new_date}, new amount: {new_amount}")
            
            # Update existing entry with combined amount
            success = bulk_update_entries(table_name, [
                {'id': existing_at_new_date['id'], 'amount': float(new_amount)}
            ], user_id=current_user.id)
            
            if not success:
                return jsonify({'status': 'error', 'message': 'Failed to update entry at new date'}), 500
            
            # Delete the old entry
            success = bulk_update_entries(table_name, [
                {'id': int(entry_id), 'amount': 0}
            ], user_id=current_user.id)
            
            # Actually delete it by setting a deletion marker
            _delete_entry_in_redis(table_name, current_user.id, category_id, old_date_str, old_date_str)
        else:
            # Update date on existing entry
            app.logger.info(f"[MOVE ENTRY] Moving entry to new date {new_date}")
            success = bulk_update_entries(table_name, [
                {'id': int(entry_id), 'date': new_date}
            ], user_id=current_user.id)
            
            if not success:
                return jsonify({'status': 'error', 'message': 'Failed to move entry'}), 500
        
        # Check for special handling
        if entry_type == 'expense':
            # Check if this is a credit account category
            expense_cats = get_entries('expense_categories', {'id': int(category_id)}, user_id=current_user.id)
            if expense_cats and expense_cats[0].get('is_credit_account') == 1:
                ca_triggered = True
            
            # Check if this is savings
            if expense_cats and expense_cats[0].get('name') == 'Savings':
                is_savings_category = True
        
        elif entry_type == 'income':
            # Check if this is savings
            income_cats = get_entries('income_categories', {'id': int(category_id)}, user_id=current_user.id)
            if income_cats and income_cats[0].get('name') == 'Savings':
                is_savings_category = True
        
        elif entry_type == 'ca':
            ca_triggered = True
        
        # Update aggregated data
        if entry_type == 'ca' or ca_triggered:
            save_ca_daily_balance()
        
        if is_savings_category or entry_type in ['income', 'expense']:
            save_totals_remainders_d()
        
        return jsonify({'status': 'success'})
        
    except Exception as e:
        app.logger.error(f"Error in move_entry_d: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500



##############################################################################
############################### DASHBOARD WEEK ###############################
##############################################################################

@app.route('/delete_income_category', methods=['POST'])
@login_required
def delete_income_category():
    category_id = request.form['id']

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # 1. Find the Auto Adjustments category for this user
            cursor.execute("""
                SELECT id FROM income_categories
                WHERE user_id = %s AND name = 'Auto Adjustments'
                LIMIT 1
            """, (current_user.id,))
            auto_adj_row = cursor.fetchone()
            if not auto_adj_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Auto Adjustments category not found'}), 400
            auto_adj_id = auto_adj_row[0]

            # 2. Find all entries for this category with date <= yesterday
            yesterday = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
            cursor.execute("""
                SELECT id, date, amount FROM income_entries
                WHERE category_id = %s AND date <= %s
            """, (category_id, yesterday))
            old_entries = cursor.fetchall()

            # 3. For each entry, add its amount to the same date in Auto Adjustments (create if needed)
            for entry_id, entry_date, amount in old_entries:
                # Check if an entry already exists for this date in Auto Adjustments
                cursor.execute("""
                    SELECT id, amount FROM income_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    # Update the amount
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE income_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    # Insert a new entry
                    cursor.execute("""
                        INSERT INTO income_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Delete the original entry
                cursor.execute("DELETE FROM income_entries WHERE id = %s", (entry_id,))

            # Delete all recurring income records associated with the income category
            cursor.execute("DELETE FROM recurring_income WHERE user_id = %s AND category_id = %s", (current_user.id, category_id))
            # Delete the income category itself
            cursor.execute("DELETE FROM income_categories WHERE user_id = %s AND id = %s", (current_user.id, category_id))

            conn.commit()
            cursor.close()
            return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/delete_expense_category', methods=['POST'])
@login_required
def delete_expense_category():
    category_id = request.form['id']

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # 1. Find the Auto Adjustments expense category for this user
            cursor.execute("""
                SELECT id FROM expense_categories
                WHERE user_id = %s AND name = 'Auto Adjustments'
                LIMIT 1
            """, (current_user.id,))
            auto_adj_row = cursor.fetchone()
            if not auto_adj_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Auto Adjustments category not found'}), 400
            auto_adj_id = auto_adj_row[0]

            # 2. Find all entries for this category with date <= yesterday
            yesterday = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
            cursor.execute("""
                SELECT id, date, amount FROM expense_entries
                WHERE category_id = %s AND date <= %s
            """, (category_id, yesterday))
            old_entries = cursor.fetchall()

            # 3. For each entry, add its amount to the same date in Auto Adjustments (create if needed)
            for entry_id, entry_date, amount in old_entries:
                # Check if an entry already exists for this date in Auto Adjustments
                cursor.execute("""
                    SELECT id, amount FROM expense_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    # Update the amount
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE expense_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    # Insert a new entry
                    cursor.execute("""
                        INSERT INTO expense_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Delete the original entry
                cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry_id,))

            # Delete all recurring expense records associated with the expense category
            cursor.execute("DELETE FROM recurring_expense WHERE user_id = %s AND category_id = %s", (current_user.id, category_id))
            # Delete the expense category itself
            cursor.execute("DELETE FROM expense_categories WHERE user_id = %s AND id = %s", (current_user.id, category_id))

            conn.commit()
            cursor.close()
            return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
        
@app.route('/delete_ca_category', methods=['POST'])
@login_required
def delete_ca_category():
    category_id = request.form['id']

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # 1. Find the account_id for this CA category
            cursor.execute("""
                SELECT account_id FROM c_expense_categories
                WHERE id = %s
            """, (category_id,))
            row = cursor.fetchone()
            if not row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'CA category not found'}), 400
            account_id = row[0]

            # 2. Find the Auto Adjustments CA category for this account
            cursor.execute("""
                SELECT id FROM c_expense_categories
                WHERE account_id = %s AND name = 'Auto Adjustments'
                LIMIT 1
            """, (account_id,))
            auto_adj_row = cursor.fetchone()
            if not auto_adj_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Auto Adjustments CA category not found'}), 400
            auto_adj_id = auto_adj_row[0]

            # 3. For all entries for this category with date <= yesterday, move to Auto Adjustments
            yesterday = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
            cursor.execute("""
                SELECT id, date, amount FROM c_expense_entries
                WHERE category_id = %s AND date <= %s
            """, (category_id, yesterday))
            old_entries = cursor.fetchall()

            for entry_id, entry_date, amount in old_entries:
                # Check if an entry already exists for this date in Auto Adjustments
                cursor.execute("""
                    SELECT id, amount FROM c_expense_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    # Update the amount
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE c_expense_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    # Insert a new entry
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Delete the original entry
                cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry_id,))

            # 4. Delete all recurring CA expense records for this category
            cursor.execute("""
                DELETE FROM recurring_c_expense WHERE category_id = %s
            """, (category_id,))

            # 5. Delete the CA category itself
            cursor.execute("""
                DELETE FROM c_expense_categories WHERE id = %s
            """, (category_id,))

            conn.commit()
            cursor.close()
            return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/add_income_category', methods=['POST'])
@login_required
def add_income_category():
    category_name = request.form['name']

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the current max display order for income categories
        cursor.execute("SELECT COALESCE(MAX(display_order), 0) FROM income_categories WHERE user_id = %s", (current_user.id,))
        max_order = cursor.fetchone()[0]

        # Insert the new category with the next display order
        cursor.execute("INSERT INTO income_categories (user_id, name, display_order) VALUES (%s, %s, %s)", (current_user.id, category_name, max_order + 1))
        new_category_id = cursor.lastrowid
        conn.commit()
        cursor.close()
    
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': category_name})

@app.route('/add_expense_category', methods=['POST'])
@login_required
def add_expense_category():
    category_name = request.form['name']

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        # Find the current max display order for expense categories
        cursor.execute("SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s", (current_user.id,))
        max_order = cursor.fetchone()[0]

        # Insert the new category with the next display order
        cursor.execute("INSERT INTO expense_categories (user_id, name, display_order) VALUES (%s, %s, %s)", (current_user.id, category_name, max_order + 1))
        new_category_id = cursor.lastrowid
        conn.commit()
        cursor.close()
    
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': category_name})

@app.route('/add_ca_category', methods=['POST'])
@login_required
def add_ca_category():
    name = request.form.get('name')
    account_id = request.form.get('account_id')
    if not name or not account_id:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Find the current max display order for CA categories for this account
        cursor.execute("""
            SELECT COALESCE(MAX(display_order), 0) FROM c_expense_categories WHERE account_id = %s
        """, (account_id,))
        max_order = cursor.fetchone()['COALESCE(MAX(display_order), 0)']

        # Insert the new category with the next display order
        cursor.execute("""
            INSERT INTO c_expense_categories (account_id, name, display_order)
            VALUES (%s, %s, %s)
        """, (account_id, name, max_order + 1))
        conn.commit()
        new_category_id = cursor.lastrowid
        cursor.close()
    
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': name})


@app.route('/update_income_category', methods=['POST'])
@login_required
def update_income_category():
    category_id = request.form['category_id']  # Use the category_id
    new_name = request.form['new_name']

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE income_categories
            SET name = %s
            WHERE id = %s AND user_id = %s
        """, (new_name, category_id, current_user.id))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})


@app.route('/update_expense_category', methods=['POST'])
@login_required
def update_expense_category():
    category_id = request.form['category_id']  # Use the category_id
    new_name = request.form['new_name']

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE expense_categories
            SET name = %s
            WHERE id = %s AND user_id = %s
        """, (new_name, category_id, current_user.id))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})

@app.route('/update_ca_category', methods=['POST'])
@login_required
def update_ca_category():
    category_id = request.form['category_id']
    new_name = request.form['new_name']

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE c_expense_categories
            SET name = %s
            WHERE id = %s
        """, (new_name, category_id))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})

@app.route('/fetch-latest-data')
def fetch_latest_data():
    # Your logic to return the latest data
    return jsonify({"data": "latest data"})

@app.route('/dashboard')
@login_required
def dashboard():
    now = datetime.now()
    fridays_by_month = {}

    # Fetch user data including goofy_week_mode, profile_picture, first_name, last_name, and balance_threshold
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] dashboard user settings for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] dashboard user settings: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
                FROM users
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] dashboard user settings for user {current_user.id}")

        goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False

        for month in range(1, 13):
            fridays = []
            cal = calendar.Calendar()
            for week in cal.monthdatescalendar(now.year, month):
                if goofy_week_mode:
                    friday = week[4]
                    if friday.month == month:
                        fridays.append(friday)
                else:
                    saturday = week[5]
                    if saturday.month == month:
                        fridays.append(saturday)
            fridays_by_month[calendar.month_name[month]] = fridays

        # Fetch income categories
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_recurring
            FROM income_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        income_categories = cursor.fetchall()

        # Fetch expense categories
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
            FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Helper to get week key (start of week for goofy, end of week for normal)
        def get_week_key(date_val, goofy_week_mode):
            if isinstance(date_val, datetime):
                dt = date_val.date()
            elif isinstance(date_val, date):
                dt = date_val
            else:
                dt = datetime.strptime(date_val, '%Y-%m-%d').date()
            if goofy_week_mode:
                # Week starts Friday: find the most recent Friday (could be today)
                days_since_friday = (dt.weekday() - 4) % 7
                week_start = dt - timedelta(days=days_since_friday)
                return week_start.strftime('%Y-%m-%d')
            else:
                # Week ends Friday: find the next Friday (could be today)
                days_until_friday = (4 - dt.weekday()) % 7
                week_end = dt + timedelta(days=days_until_friday)
                return week_end.strftime('%Y-%m-%d')

        # --- AGGREGATE INCOME ENTRIES ---
        # Try Redis first
        raw_income_entries = _get_entries_from_redis('income_entries', current_user.id)
        if raw_income_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard income_entries for user {current_user.id}")
            cursor.execute("""
                SELECT id, category_id, date, amount, processed
                FROM income_entries
                WHERE category_id IN (SELECT id FROM income_categories WHERE user_id = %s)
            """, (current_user.id,))
            raw_income_entries = list(cursor.fetchall())
            raw_income_entries = _filter_pending_deletions('income_entries', current_user.id, raw_income_entries)
            # Update Redis cache
            _set_entries_to_redis('income_entries', current_user.id, raw_income_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard income_entries for user {current_user.id}")

        income_map = {}
        processed_map = {}
        for entry in raw_income_entries:
            week_key = get_week_key(entry['date'], goofy_week_mode)
            key = (entry['category_id'], week_key)
            income_map[key] = income_map.get(key, 0.0) + float(entry['amount'])
            if key not in processed_map:
                processed_map[key] = []
            processed_map[key].append(entry['processed'])

        income_entries = []
        for key, total_amount in income_map.items():
            processed_list = processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, week_key = key
            income_entries.append({
                'category_id': category_id,
                'date': week_key,
                'total_amount': total_amount,
                'processed': processed
            })

        # --- AGGREGATE EXPENSE ENTRIES ---
        # Try Redis first
        raw_expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
        if raw_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT id, category_id, date, amount, processed
                FROM expense_entries
                WHERE category_id IN (SELECT id FROM expense_categories WHERE user_id = %s)
            """, (current_user.id,))
            raw_expense_entries = list(cursor.fetchall())
            raw_expense_entries = _filter_pending_deletions('expense_entries', current_user.id, raw_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('expense_entries', current_user.id, raw_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard expense_entries for user {current_user.id}")

        expense_map = {}
        expense_processed_map = {}
        for entry in raw_expense_entries:
            week_key = get_week_key(entry['date'], goofy_week_mode)
            key = (entry['category_id'], week_key)
            expense_map[key] = expense_map.get(key, 0.0) + float(entry['amount'])
            if key not in expense_processed_map:
                expense_processed_map[key] = []
            expense_processed_map[key].append(entry['processed'])

        expense_entries = []
        for key, total_amount in expense_map.items():
            processed_list = expense_processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, week_key = key
            expense_entries.append({
                'category_id': category_id,
                'date': week_key,
                'total_amount': total_amount,
                'processed': processed
            })

        # --- AGGREGATE CA ENTRIES ---
        # Try Redis first
        raw_c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
        if raw_c_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard c_expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT cee.id, cee.category_id, cee.date, cee.amount, cee.processed
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
            """, (current_user.id,))
            raw_c_expense_entries = list(cursor.fetchall())
            raw_c_expense_entries = _filter_pending_deletions('c_expense_entries', current_user.id, raw_c_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('c_expense_entries', current_user.id, raw_c_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard c_expense_entries for user {current_user.id}")

        c_expense_map = {}
        c_expense_processed_map = {}
        for entry in raw_c_expense_entries:
            week_key = get_week_key(entry['date'], goofy_week_mode)
            key = (entry['category_id'], week_key)
            c_expense_map[key] = c_expense_map.get(key, 0.0) + float(entry['amount'])
            if key not in c_expense_processed_map:
                c_expense_processed_map[key] = []
            c_expense_processed_map[key].append(entry['processed'])

        c_expense_entries = []
        for key, total_amount in c_expense_map.items():
            processed_list = c_expense_processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, week_key = key
            c_expense_entries.append({
                'category_id': category_id,
                'date': week_key,
                'total_amount': total_amount,
                'processed': processed
            })

        # Fetch all totals and remainders
        # Try Redis first
        totals_remainders = _get_totals_remainders_from_redis('totals_remainders', current_user.id)
        if totals_remainders is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard totals_remainders for user {current_user.id}")
            cursor.execute("""
                SELECT date, total_income, total_expenses, remainder, last_week_remainder
                FROM totals_remainders
                WHERE user_id = %s
            """, (current_user.id,))
            totals_remainders = cursor.fetchall()
            # Update Redis cache
            _set_totals_remainders_to_redis('totals_remainders', current_user.id, totals_remainders)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard totals_remainders for user {current_user.id}")

        # Fetch all savings entries for the user
        # Try Redis first
        savings_entries = _get_savings_entries_from_redis(current_user.id)
        if savings_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard savings_entries for user {current_user.id}")
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            savings_entries = cursor.fetchall()
            # Update Redis cache
            _set_savings_entries_to_redis(current_user.id, savings_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard savings_entries for user {current_user.id}")

        # Fetch buds
        cursor.execute("""
            SELECT b.id, b.expense_category_id
            FROM buds b
            WHERE b.user_id = %s
        """, (current_user.id,))
        buds = cursor.fetchall()

        # --- Credit Accounts Section ---
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order DESC, cec.id DESC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # Try Redis first for CA balances
        c_a_balances = _get_ca_balances_from_redis('c_a_balances', current_user.id)
        if c_a_balances is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard c_a_balances for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM c_a_balances
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                )
                ORDER BY date DESC
            """, (current_user.id,))
            c_a_balances = cursor.fetchall()
            # Update Redis cache
            _set_ca_balances_to_redis('c_a_balances', current_user.id, c_a_balances)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard c_a_balances for user {current_user.id}")

        cursor.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = user_data['balance_threshold'] if user_data else 0
    member_since = user_data['member_since'] if user_data else None
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard'

    return render_template(
        'dashboard.html',
        fridays_by_month=fridays_by_month,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        income_categories=income_categories,
        expense_categories=expense_categories,
        income_entries=income_entries,
        expense_entries=expense_entries,
        totals_remainders=totals_remainders,
        calendar=calendar,
        now=now,
        balance_threshold=balance_threshold,
        goofy_week_mode=goofy_week_mode,
        savings_entries=savings_entries,
        member_since=member_since,
        landing_page=landing_page,
        buds=buds,
        currency_type=currency_type,
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances=c_a_balances
    )

@app.route('/get_total_income', methods=['GET'])
@login_required
def get_total_income():
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': 'Missing date parameter'}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    total_income = row.get('total_income', 0)
                    return jsonify({'status': 'success', 'total_income': total_income})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch total_income from totals_remainders table
            cursor.execute("""
                SELECT total_income
                FROM totals_remainders
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            
            result = cursor.fetchone()
            total_income = result['total_income'] if result else 0
            cursor.close()
        
        return jsonify({'status': 'success', 'total_income': total_income})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500


@app.route('/get_total_expenses', methods=['GET'])
@login_required
def get_total_expenses():
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': 'Missing date parameter'}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    total_expenses = row.get('total_expenses', 0)
                    return jsonify({'status': 'success', 'total_expenses': total_expenses})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch total_expenses from totals_remainders table
            cursor.execute("""
                SELECT total_expenses
                FROM totals_remainders
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            
            result = cursor.fetchone()
            total_expenses = result['total_expenses'] if result else 0
            cursor.close()
        
        return jsonify({'status': 'success', 'total_expenses': total_expenses})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500
    
@app.route('/get_ca_balance', methods=['GET'])
@login_required
def get_ca_balance():
    account_id = request.args.get('account_id')
    date = request.args.get('date')
    if not account_id or not date:
        return jsonify({'status': 'error', 'message': 'Missing account_id or date'}), 400

    try:
        # Try Redis first
        cached_data = _get_ca_balances_from_redis('c_a_balances', current_user.id, account_id=int(account_id))
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    balance = row.get('balance', 0)
                    return jsonify({'status': 'success', 'balance': balance})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT balance
                FROM c_a_balances
                WHERE account_id = %s AND date = %s
                LIMIT 1
            """, (account_id, date))
            result = cursor.fetchone()
            cursor.close()
            
        balance = result['balance'] if result and result['balance'] is not None else 0
        return jsonify({'status': 'success', 'balance': balance})
    except Exception as e:
        app.logger.error(f"[get_ca_balance] Error: {e}")
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

@app.route('/get_last_remainder', methods=['GET'])
@login_required
def get_last_remainder():
    date = request.args.get('date')

    if not date:
        return jsonify({"status": "error", "message": "Date parameter is missing"}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    last_remainder = row.get('last_week_remainder', 0)
                    return jsonify({"status": "success", "last_remainder": last_remainder})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Query to get the last week's remainder
            cursor.execute("""
                SELECT last_week_remainder
                FROM totals_remainders
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            last_remainder_data = cursor.fetchone()
            cursor.close()

        if last_remainder_data:
            return jsonify({"status": "success", "last_remainder": last_remainder_data['last_week_remainder']})
        else:
            return jsonify({"status": "error", "message": "No data found for the given date"}), 404

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500

@app.route('/get_remainder', methods=['GET'])
@login_required
def get_remainder():
    date = request.args.get('date')

    if not date:
        return jsonify({"status": "error", "message": "Date parameter is missing"}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    remainder = row.get('remainder', 0)
                    return jsonify({"status": "success", "remainder": remainder})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Query to get the remainder for the specified date
            cursor.execute("""
                SELECT remainder
                FROM totals_remainders
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            remainder_data = cursor.fetchone()
            cursor.close()

        if remainder_data:
            return jsonify({"status": "success", "remainder": remainder_data['remainder']})
        else:
            return jsonify({"status": "error", "message": "No remainder found for the specified date"}), 404

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500


@app.route('/update_income_order', methods=['POST'])
@login_required
def update_income_order():
    order_data = request.json['order']
    user_id = current_user.id

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        for item in order_data:
            category_name = item['name']
            display_order = item['order']
            cursor.execute("""
                UPDATE income_categories 
                SET display_order = %s 
                WHERE user_id = %s AND name = %s
            """, (display_order, user_id, category_name))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})

@app.route('/update_expense_order', methods=['POST'])
@login_required
def update_expense_order():
    order_data = request.json['order']
    user_id = current_user.id

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        for item in order_data:
            category_name = item['name']
            display_order = item['order']
            cursor.execute("""
                UPDATE expense_categories 
                SET display_order = %s 
                WHERE user_id = %s AND name = %s
            """, (display_order, user_id, category_name))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})

@app.route('/update_ca_order', methods=['POST'])
@login_required
def update_ca_order():
    order_data = request.json['order']
    account_id = request.json.get('account_id')

    if not account_id or not order_data:
        return jsonify({'status': 'error', 'message': 'Missing account_id or order data'}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        for item in order_data:
            category_name = item['name']
            display_order = item['order']
            cursor.execute("""
                UPDATE c_expense_categories
                SET display_order = %s
                WHERE account_id = %s AND name = %s
            """, (display_order, account_id, category_name))

        conn.commit()
        cursor.close()

    return jsonify({'status': 'success'})

@app.route('/update-entry', methods=['POST'])
@login_required
def update_entry():
    data = request.json
    category_id = data.get('category_id')
    date = data.get('date')
    amount = data.get('amount')
    entry_type = data.get('type')
    
    app.logger.info(f"[UPDATE ENTRY] User {current_user.id}: type={entry_type}, category={category_id}, amount={amount}, date={date}")

    if not category_id or not date or amount is None:
        return jsonify({"status": "error", "message": "Missing required parameters"}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        ca_triggered = False
        payment_category_name = None
        account_id = None

        if entry_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
        elif entry_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
        else:
            cursor.close()
            return jsonify({"status": "error", "message": "Invalid entry type"}), 400

        # User isolation for CA: join through credit_accounts
        if entry_type == 'ca':
            cursor.execute(f"""
                SELECT cec.id
                FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE cec.id = %s AND ca.user_id = %s
            """, (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400
        else:
            cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400
        
        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        app.logger.info(f"[UPDATE ENTRY] Category data for category {category_id}: name='{cat_data.get('name')}', is_credit_account={cat_data.get('is_credit_account', 'MISSING')}")
        cursor.close()
    
    # Write to Redis only - flush worker will persist to MySQL
    _update_entry_in_redis(table_name, current_user.id, category_id, date, amount)
    
    # If this is an expense category and is_credit_account=1, update payment entry and trigger CA balance update
    app.logger.info(f"[UPDATE CA PAYMENT DEBUG] entry_type={entry_type}, cat_data={cat_data}")
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        app.logger.info(f"[UPDATE CA PAYMENT] Detected payment category for user {current_user.id}, category {category_id}")
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        app.logger.info(f"[UPDATE CA PAYMENT] Category name: '{category_name}'")
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            app.logger.info(f"[UPDATE CA PAYMENT] Looking for credit account with name: '{account_name}'")
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                app.logger.info(f"[UPDATE CA PAYMENT] Credit account query result: {account_row}")
                if account_row:
                    account_id = account_row['id']
                    app.logger.info(f"[UPDATE CA PAYMENT] Found credit account_id={account_id}, updating payment entry for date={date}, amount={amount}")
                    # Update payment entry in Redis
                    _update_payment_entry_in_redis(current_user.id, account_id, date, float(amount))
                    app.logger.info(f"[UPDATE CA PAYMENT] Payment entry update completed")
                else:
                    app.logger.warning(f"[UPDATE CA PAYMENT] No credit account found with name '{account_name}' for user {current_user.id}")
                cursor.close()
        else:
            app.logger.warning(f"[UPDATE CA PAYMENT] Category name '{category_name}' does not end with ' payment'")
    else:
        app.logger.info(f"[UPDATE CA PAYMENT] Not a payment category: entry_type={entry_type}, is_credit_account={cat_data.get('is_credit_account', 0)}")

    if entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()

    return jsonify({"status": "success"})

@app.route('/delete-entry', methods=['POST'])
@login_required
def delete_entry():
    data = request.get_json()
    category_id = data.get('category_id')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    entry_type = data.get('type')

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        ca_triggered = False
        payment_category_name = None
        account_id = None

        if entry_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
            user_field = 'user_id'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
            user_field = 'user_id'
        elif entry_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
            user_field = 'ca.user_id'
        else:
            cursor.close()
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

        # User isolation for CA: join through credit_accounts
        if entry_type == 'ca':
            cursor.execute(f"""
                SELECT cec.id
                FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE cec.id = %s AND ca.user_id = %s
            """, (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        else:
            cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        
        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        cursor.close()
    
    # Delete from Redis only - flush worker will persist to MySQL
    _delete_entry_in_redis(table_name, current_user.id, category_id, start_date, end_date)
    
    # Check if this is a savings category - update savings if so
    is_savings_category = False
    if entry_type in ['income', 'expense'] and cat_data.get('name') == 'Savings':
        is_savings_category = True
    
    # If this is an expense category and is_credit_account=1, delete payment entry and trigger CA balance update
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                if account_row:
                    account_id = account_row['id']
                    # Delete payment entries for this account and date range
                    _delete_payment_entry_in_redis(current_user.id, account_id, start_date, end_date)
                cursor.close()

    if entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()
    
    # Update totals and savings if this is a savings category or regular income/expense
    if is_savings_category or entry_type in ['income', 'expense']:
        save_totals_remainders_d()

    return jsonify({'status': 'success'})

@app.route('/check_and_initialize_totals', methods=['POST'])
@login_required
def check_and_initialize_totals():
    try:
        data = request.get_json()
        fridays = data.get('fridays', [])

        if not fridays:
            return jsonify({"status": "error", "message": "No Fridays provided"}), 400

        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Iterate over each provided Friday
            for friday in fridays:
                friday_date = friday['date']

                # Check if a record for this Friday already exists in totals_remainders
                cursor.execute("""
                    SELECT COUNT(*) as count FROM totals_remainders
                    WHERE user_id = %s AND date = %s
                """, (current_user.id, friday_date))
                record_exists = cursor.fetchone()['count']

                # If no record exists, create it with default values (0 for totals)
                if record_exists == 0:
                    cursor.execute("""
                        INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (current_user.id, friday_date, 0.00, 0.00, 0.00, 0.00))

            # Commit the changes to the database
            cursor.close()
            conn.commit()

        return jsonify({"status": "success", "message": "Missing records initialized."})
    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/update-processed-status-week-range', methods=['POST'])
@login_required
def update_processed_status_week_range():
    from redis_crud import get_entries, bulk_update_entries
    from datetime import datetime
    
    data = request.get_json()
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income', 'expense', or 'ca'
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    processed = data.get('processed')

    if not category_id or not category_type or not start_date or not end_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    try:
        # Determine the table name
        if category_type == 'income':
            table_name = 'income_entries'
        elif category_type == 'expense':
            table_name = 'expense_entries'
        elif category_type == 'ca':
            table_name = 'c_expense_entries'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        # Parse dates
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
        
        # Get all entries for this category (Redis-first)
        all_entries = get_entries(table_name, {'category_id': int(category_id)}, user_id=current_user.id)
        
        app.logger.info(f"[UPDATE PROCESSED WEEK] User {current_user.id}, category {category_id}, date range {start_date} to {end_date}, found {len(all_entries)} total entries")
        
        # Filter entries within date range
        entries_to_update = []
        for entry in all_entries:
            entry_date = entry.get('date')
            # Handle both date objects and string dates
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            
            if start_date_obj <= entry_date <= end_date_obj:
                entries_to_update.append(entry)
        
        app.logger.info(f"[UPDATE PROCESSED WEEK] Found {len(entries_to_update)} entries in date range")
        
        if not entries_to_update:
            return jsonify({'status': 'success', 'message': 'No entries found in date range'})
        
        # Prepare bulk update - convert processed to int
        processed_int = int(processed)
        updates = [{'id': entry['id'], 'processed': processed_int} for entry in entries_to_update]
        
        # Update using Redis-first approach
        success = bulk_update_entries(table_name, updates, user_id=current_user.id)
        
        if success:
            app.logger.info(f"[UPDATE PROCESSED WEEK] Successfully updated {len(updates)} entries")
            
            # Calculate if all entries in this date range for this category are now processed
            all_entries_in_range = get_entries(table_name, {'category_id': int(category_id)}, user_id=current_user.id)
            entries_in_date_range = []
            for entry in all_entries_in_range:
                entry_date = entry.get('date')
                if isinstance(entry_date, str):
                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                if start_date_obj <= entry_date <= end_date_obj:
                    entries_in_date_range.append(entry)
            
            # Check if all entries are processed - convert to int for comparison
            all_processed = all(int(entry.get('processed', 0)) == 1 for entry in entries_in_date_range) if entries_in_date_range else False
            
            return jsonify({
                'status': 'success',
                'all_processed': all_processed,
                'entries_updated': len(updates)
            })
        else:
            return jsonify({'status': 'error', 'message': 'Failed to update entries'}), 500

    except Exception as e:
        app.logger.error(f"Error in update_processed_status_week_range: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/hide_income_category', methods=['POST'])
@login_required
def hide_income_category():
    category_id = request.form.get('category_id')
    hidden = request.form.get('hidden', 1)
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE income_categories SET hidden = %s WHERE id = %s AND user_id = %s", (hidden, category_id, current_user.id))
    return jsonify({'status': 'success'})

@app.route('/hide_expense_category', methods=['POST'])
@login_required
def hide_expense_category():
    category_id = request.form.get('category_id')
    hidden = request.form.get('hidden', 1)
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE expense_categories SET hidden = %s WHERE id = %s AND user_id = %s", (hidden, category_id, current_user.id))
    return jsonify({'status': 'success'})

@app.route('/hide_ca_category', methods=['POST'])
@login_required
def hide_ca_category():
    category_id = request.form['category_id']
    hidden = int(request.form['hidden'])
    with get_db_pool().get_cursor(commit=True) as cursor:
        cursor.execute(
            "UPDATE c_expense_categories SET hidden = %s WHERE id = %s",
            (hidden, category_id)
        )
    return jsonify({'status': 'success'})

@app.route('/delete-week-entry', methods=['POST'])
@login_required
def delete_week_entry():
    from datetime import datetime
    data = request.get_json()
    category_id = data.get('category_id')
    entry_type = data.get('type')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    friday_date = data.get('friday_date')  # You may need to pass this from frontend

    if not all([category_id, entry_type, start_date, end_date]):
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Determine the correct table for deletion
        if entry_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
        elif entry_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
        else:
            cursor.close()
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

        # Optionally, check that the category_id exists in the correct category table
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s", (category_id,))
        cat_row = cursor.fetchone()

        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        cursor.close()
    
    # Delete from Redis only - flush worker will persist to MySQL
    _delete_entry_in_redis(table_name, current_user.id, category_id, start_date, end_date)
    
    ca_triggered = False
    # If this is an expense category and is_credit_account=1, delete payment entry and trigger CA balance update
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                if account_row:
                    account_id = account_row['id']
                    # Delete payment entries for this account and date range
                    _delete_payment_entry_in_redis(current_user.id, account_id, start_date, end_date)
                cursor.close()

    # If a CA payment was updated, trigger CA balance recalculation
    if ca_triggered or entry_type == 'ca':
        save_ca_daily_balance()

    return jsonify({'status': 'success'})

@app.route('/update-week-entry', methods=['POST'])
@login_required
def update_week_entry():
    data = request.get_json()
    entry_type = data.get('type')
    category_id = data.get('category_id')
    amount = data.get('amount')
    friday_date = data.get('friday_date')
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not category_id or not friday_date or amount is None or not entry_type or not start_date or not end_date:
        return jsonify({"status": "error", "message": "Missing required parameters"}), 400

    # Choose the correct table
    if entry_type == 'income':
        table_name = 'income_entries'
        category_table = 'income_categories'
    elif entry_type == 'expense':
        table_name = 'expense_entries'
        category_table = 'expense_categories'
    elif entry_type == 'ca':
        table_name = 'c_expense_entries'
        category_table = 'c_expense_categories'
    else:
        return jsonify({"status": "error", "message": "Invalid entry type"}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Optionally, check that the category_id exists in the correct category table
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s", (category_id,))
        cat_row = cursor.fetchone()
        if not cat_row:
            cursor.close()
            return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400

        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        cursor.close()

    # Delete old entries and add new entry to Redis only - flush worker will persist
    _delete_entry_in_redis(table_name, current_user.id, category_id, start_date, end_date)
    _update_entry_in_redis(table_name, current_user.id, category_id, friday_date, float(amount))

    # If this is an expense category and is_credit_account=1, create/update payment entry and trigger CA balance update
    ca_triggered = False
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Detected payment category for user {current_user.id}, category {category_id}")
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Category name: '{category_name}'")
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Looking for credit account with name: '{account_name}'")
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Credit account query result: {account_row}")
                if account_row:
                    account_id = account_row['id']
                    app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Found credit account_id={account_id}, updating payment entry for date={friday_date}, amount={amount}")
                    # Update payment entry in Redis
                    _update_payment_entry_in_redis(current_user.id, account_id, friday_date, float(amount))
                    app.logger.info(f"[UPDATE WEEK ENTRY - CA PAYMENT] Payment entry update completed")
                else:
                    app.logger.warning(f"[UPDATE WEEK ENTRY - CA PAYMENT] No credit account found with name '{account_name}' for user {current_user.id}")
                cursor.close()
        else:
            app.logger.warning(f"[UPDATE WEEK ENTRY - CA PAYMENT] Category name '{category_name}' does not end with ' payment'")

    # If a CA payment was updated, trigger CA balance recalculation
    if ca_triggered or entry_type == 'ca':
        save_ca_daily_balance()

    return jsonify({"status": "success"})


############################################################################################
############################### DASHBOARD MONTH ############################################
############################################################################################

@app.route('/dashboard_3m')
@login_required
def dashboard_3m():
    now = datetime.now()
    fridays_by_month = {}

    # Fetch user data using connection pool
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] dashboard_3m user settings for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] dashboard_3m user settings: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
                FROM users
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] dashboard_3m user settings for user {current_user.id}")
        
        goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False

        # Build fridays_by_month for navigation (unchanged)
        for month in range(1, 13):
            fridays = []
            cal = calendar.Calendar()
            for week in cal.monthdatescalendar(now.year, month):
                if goofy_week_mode:
                    friday = week[4]
                    if friday.month == month:
                        fridays.append(friday)
                else:
                    saturday = week[5]
                    if saturday.month == month:
                        fridays.append(saturday)
            fridays_by_month[calendar.month_name[month]] = fridays

        # Fetch categories
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_recurring
            FROM income_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        income_categories = cursor.fetchall()

        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
            FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Helper: get last day of month string
        def get_month_end_str(date_val):
            if isinstance(date_val, datetime):
                dt = date_val.date()
            elif isinstance(date_val, date):
                dt = date_val
            else:
                dt = datetime.strptime(date_val, '%Y-%m-%d').date()
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            month_end = dt.replace(day=last_day)
            return month_end.strftime('%Y-%m-%d')

        # --- AGGREGATE INCOME ENTRIES BY MONTH ---
        # Try Redis first
        raw_income_entries = _get_entries_from_redis('income_entries', current_user.id)
        if raw_income_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m income_entries for user {current_user.id}")
            cursor.execute("""
                SELECT id, category_id, date, amount, processed
                FROM income_entries
                WHERE category_id IN (SELECT id FROM income_categories WHERE user_id = %s)
            """, (current_user.id,))
            raw_income_entries = list(cursor.fetchall())
            raw_income_entries = _filter_pending_deletions('income_entries', current_user.id, raw_income_entries)
            # Update Redis cache
            _set_entries_to_redis('income_entries', current_user.id, raw_income_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m income_entries for user {current_user.id}")
        income_map = {}
        processed_map = {}
        for entry in raw_income_entries:
            month_end = get_month_end_str(entry['date'])
            key = (entry['category_id'], month_end)
            income_map[key] = income_map.get(key, 0.0) + float(entry['amount'])
            if key not in processed_map:
                processed_map[key] = []
            processed_map[key].append(entry['processed'])
        income_entries = []
        for key, total_amount in income_map.items():
            processed_list = processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, month_end = key
            income_entries.append({
                'category_id': category_id,
                'date': month_end,
                'total_amount': total_amount,
                'processed': processed
            })

        # --- AGGREGATE EXPENSE ENTRIES BY MONTH ---
        # Try Redis first
        raw_expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
        if raw_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT id, category_id, date, amount, processed
                FROM expense_entries
                WHERE category_id IN (SELECT id FROM expense_categories WHERE user_id = %s)
            """, (current_user.id,))
            raw_expense_entries = list(cursor.fetchall())
            raw_expense_entries = _filter_pending_deletions('expense_entries', current_user.id, raw_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('expense_entries', current_user.id, raw_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m expense_entries for user {current_user.id}")
        expense_map = {}
        expense_processed_map = {}
        for entry in raw_expense_entries:
            month_end = get_month_end_str(entry['date'])
            key = (entry['category_id'], month_end)
            expense_map[key] = expense_map.get(key, 0.0) + float(entry['amount'])
            if key not in expense_processed_map:
                expense_processed_map[key] = []
            expense_processed_map[key].append(entry['processed'])
        expense_entries = []
        for key, total_amount in expense_map.items():
            processed_list = expense_processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, month_end = key
            expense_entries.append({
                'category_id': category_id,
                'date': month_end,
                'total_amount': total_amount,
                'processed': processed
            })

        # --- AGGREGATE CA ENTRIES BY MONTH ---
        # Try Redis first
        raw_c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
        if raw_c_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m c_expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT cee.id, cee.category_id, cee.date, cee.amount, cee.processed
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
            """, (current_user.id,))
            raw_c_expense_entries = list(cursor.fetchall())
            raw_c_expense_entries = _filter_pending_deletions('c_expense_entries', current_user.id, raw_c_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('c_expense_entries', current_user.id, raw_c_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m c_expense_entries for user {current_user.id}")
        c_expense_map = {}
        c_expense_processed_map = {}
        for entry in raw_c_expense_entries:
            month_end = get_month_end_str(entry['date'])
            key = (entry['category_id'], month_end)
            c_expense_map[key] = c_expense_map.get(key, 0.0) + float(entry['amount'])
            if key not in c_expense_processed_map:
                c_expense_processed_map[key] = []
            c_expense_processed_map[key].append(entry['processed'])
        c_expense_entries = []
        for key, total_amount in c_expense_map.items():
            processed_list = c_expense_processed_map[key]
            processed = 1 if all(p == 1 for p in processed_list) else 0
            category_id, month_end = key
            c_expense_entries.append({
                'category_id': category_id,
                'date': month_end,
                'total_amount': total_amount,
                'processed': processed
            })

        # Fetch all monthly totals and remainders
        # Try Redis first
        totals_remainders = _get_totals_remainders_from_redis('totals_remainders_m', current_user.id)
        if totals_remainders is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m totals_remainders_m for user {current_user.id}")
            cursor.execute("""
                SELECT date, total_income, total_expenses, remainder, last_month_remainder
                FROM totals_remainders_m
                WHERE user_id = %s
            """, (current_user.id,))
            totals_remainders = cursor.fetchall()
            # Update Redis cache
            _set_totals_remainders_to_redis('totals_remainders_m', current_user.id, totals_remainders)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m totals_remainders_m for user {current_user.id}")

        # Fetch all savings entries for the user
        # Try Redis first
        savings_entries = _get_savings_entries_from_redis(current_user.id)
        if savings_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m savings_entries for user {current_user.id}")
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            savings_entries = cursor.fetchall()
            # Update Redis cache
            _set_savings_entries_to_redis(current_user.id, savings_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m savings_entries for user {current_user.id}")

        # Fetch Buds
        cursor.execute("""
            SELECT b.id, b.expense_category_id
            FROM buds b
            WHERE b.user_id = %s
        """, (current_user.id,))
        buds = cursor.fetchall()

        # --- Credit Accounts Section ---
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order DESC, cec.id ASC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # Try Redis first for CA balances
        c_a_balances_m = _get_ca_balances_from_redis('c_a_balances_m', current_user.id)
        if c_a_balances_m is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_3m c_a_balances_m for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM c_a_balances_m
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                )
                ORDER BY date DESC
            """, (current_user.id,))
            c_a_balances_m = cursor.fetchall()
            # Update Redis cache
            _set_ca_balances_to_redis('c_a_balances_m', current_user.id, c_a_balances_m)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_3m c_a_balances_m for user {current_user.id}")
        cursor.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = user_data['balance_threshold'] if user_data else 0
    member_since = user_data['member_since'] if user_data else None
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard_3m'

    return render_template(
        'dashboard_3m.html',
        fridays_by_month=fridays_by_month,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        income_categories=income_categories,
        expense_categories=expense_categories,
        income_entries=income_entries,
        expense_entries=expense_entries,
        totals_remainders=totals_remainders,
        calendar=calendar,
        now=now,
        balance_threshold=balance_threshold,
        goofy_week_mode=goofy_week_mode,
        savings_entries=savings_entries,
        member_since=member_since,
        landing_page=landing_page,
        buds=buds,
        currency_type=currency_type,
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances_m=c_a_balances_m
    )

@app.route('/get_ca_balance_3m', methods=['GET'])
@login_required
def get_ca_balance_3m():
    account_id = request.args.get('account_id')
    date = request.args.get('date')
    if not account_id or not date:
        return jsonify({'status': 'error', 'message': 'Missing account_id or date'}), 400

    try:
        # Try Redis first
        cached_data = _get_ca_balances_from_redis('c_a_balances_m', current_user.id, account_id=int(account_id))
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    balance = row.get('balance', 0)
                    return jsonify({'status': 'success', 'balance': balance})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT balance
                FROM c_a_balances_m
                WHERE account_id = %s AND date = %s
                LIMIT 1
            """, (account_id, date))
            result = cursor.fetchone()
            cursor.close()
            
        balance = result['balance'] if result and result['balance'] is not None else 0
        return jsonify({'status': 'success', 'balance': balance})
    except Exception as e:
        app.logger.error(f"[get_ca_balance_3m] Error: {e}")
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

@app.route('/get_total_income_3m', methods=['GET'])
@login_required
def get_total_income_3m():
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': 'Missing date parameter'}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders_m', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    total_income = row.get('total_income', 0)
                    return jsonify({'status': 'success', 'total_income': total_income})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch total_income from totals_remainders_m table
            cursor.execute("""
                SELECT total_income
                FROM totals_remainders_m
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            
            result = cursor.fetchone()
            total_income = result['total_income'] if result else 0
            cursor.close()
        
        return jsonify({'status': 'success', 'total_income': total_income})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

@app.route('/get_total_expenses_3m', methods=['GET'])
@login_required
def get_total_expenses_3m():
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': 'Missing date parameter'}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders_m', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    total_expenses = row.get('total_expenses', 0)
                    return jsonify({'status': 'success', 'total_expenses': total_expenses})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch total_expenses from totals_remainders_m table
            cursor.execute("""
                SELECT total_expenses
                FROM totals_remainders_m
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            
            result = cursor.fetchone()
            total_expenses = result['total_expenses'] if result else 0
            cursor.close()
        
        return jsonify({'status': 'success', 'total_expenses': total_expenses})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

@app.route('/get_last_remainder_3m', methods=['GET'])
@login_required
def get_last_remainder_3m():
    date = request.args.get('date')

    if not date:
        return jsonify({"status": "error", "message": "Date parameter is missing"}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders_m', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    last_remainder = row.get('last_month_remainder', 0)
                    return jsonify({"status": "success", "last_remainder": last_remainder})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Query to get the last month's remainder
            cursor.execute("""
                SELECT last_month_remainder
                FROM totals_remainders_m
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            last_remainder_data = cursor.fetchone()
            cursor.close()

        if last_remainder_data:
            return jsonify({"status": "success", "last_remainder": last_remainder_data['last_month_remainder']})
        else:
            return jsonify({"status": "error", "message": "No data found for the given date"}), 404

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500

@app.route('/get_remainder_3m', methods=['GET'])
@login_required
def get_remainder_3m():
    date = request.args.get('date')

    if not date:
        return jsonify({"status": "error", "message": "Date parameter is missing"}), 400

    try:
        # Try Redis first
        cached_data = _get_totals_remainders_from_redis('totals_remainders_m', current_user.id)
        if cached_data:
            # Find the matching date in cached data
            for row in cached_data:
                if row.get('date') == date:
                    remainder = row.get('remainder', 0)
                    return jsonify({"status": "success", "remainder": remainder})
        
        # Fallback to MySQL if not in Redis
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Query to get the remainder for the specified date
            cursor.execute("""
                SELECT remainder
                FROM totals_remainders_m
                WHERE user_id = %s AND date = %s
            """, (current_user.id, date))
            remainder_data = cursor.fetchone()
            cursor.close()

        if remainder_data:
            return jsonify({"status": "success", "remainder": remainder_data['remainder']})
        else:
            return jsonify({"status": "error", "message": "No remainder found for the specified date"}), 404

    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500

@app.route('/update-processed-status-month-range', methods=['POST'])
@login_required
def update_processed_status_month_range():
    from redis_crud import get_entries, bulk_update_entries
    from datetime import datetime
    
    data = request.get_json()
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income' or 'expense'
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    processed = data.get('processed')

    if not category_id or not category_type or not start_date or not end_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    try:
        # Determine the table name
        if category_type == 'income':
            table_name = 'income_entries'
        elif category_type == 'expense':
            table_name = 'expense_entries'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        # Parse dates
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
        
        # Get all entries for this category (Redis-first)
        all_entries = get_entries(table_name, {'category_id': int(category_id)}, user_id=current_user.id)
        
        app.logger.info(f"[UPDATE PROCESSED MONTH] User {current_user.id}, category {category_id}, date range {start_date} to {end_date}, found {len(all_entries)} total entries")
        
        # Filter entries within date range
        entries_to_update = []
        for entry in all_entries:
            entry_date = entry.get('date')
            # Handle both date objects and string dates
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            
            if start_date_obj <= entry_date <= end_date_obj:
                entries_to_update.append(entry)
        
        app.logger.info(f"[UPDATE PROCESSED MONTH] Found {len(entries_to_update)} entries in date range")
        
        if not entries_to_update:
            return jsonify({'status': 'success', 'message': 'No entries found in date range'})
        
        # Prepare bulk update - convert processed to int
        processed_int = int(processed)
        updates = [{'id': entry['id'], 'processed': processed_int} for entry in entries_to_update]
        
        # Update using Redis-first approach
        success = bulk_update_entries(table_name, updates, user_id=current_user.id)
        
        if success:
            app.logger.info(f"[UPDATE PROCESSED MONTH] Successfully updated {len(updates)} entries")
            
            # Calculate if all entries in this date range for this category are now processed
            all_entries_in_range = get_entries(table_name, {'category_id': int(category_id)}, user_id=current_user.id)
            entries_in_date_range = []
            for entry in all_entries_in_range:
                entry_date = entry.get('date')
                if isinstance(entry_date, str):
                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                if start_date_obj <= entry_date <= end_date_obj:
                    entries_in_date_range.append(entry)
            
            # Check if all entries are processed - convert to int for comparison
            all_processed = all(int(entry.get('processed', 0)) == 1 for entry in entries_in_date_range) if entries_in_date_range else False
            
            return jsonify({
                'status': 'success',
                'all_processed': all_processed,
                'entries_updated': len(updates)
            })
        else:
            return jsonify({'status': 'error', 'message': 'Failed to update entries'}), 500

    except Exception as e:
        app.logger.error(f"Error in update_processed_status_month_range: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

########################################################################################
############################### DASHBOARD MONTH OVERVIEW ###############################
########################################################################################

@app.route('/dashboard_m')
@login_required
def dashboard_m():
    selected_date = request.args.get('date')
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] dashboard_m user settings for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] dashboard_m user settings: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
                FROM users
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] dashboard_m user settings for user {current_user.id}")

        profile_picture = user_data['profile_picture'] if user_data else None
        first_name = user_data['first_name'] if user_data else ''
        last_name = user_data['last_name'] if user_data else ''
        goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False
        balance_threshold = float(user_data.get('balance_threshold', 0)) if user_data else 0
        member_since = user_data['member_since'] if user_data else None
        currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
        landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard_3m'

        # Categories
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_recurring
            FROM income_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        income_categories = cursor.fetchall()

        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
            FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Entries
        # Try Redis first for income entries
        income_entries = _get_entries_from_redis('income_entries', current_user.id)
        if income_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m income_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
                ORDER BY ic.display_order DESC, ie.date ASC
            """, (current_user.id,))
            income_entries = list(cursor.fetchall())
            income_entries = _filter_pending_deletions('income_entries', current_user.id, income_entries)
            # Update Redis cache
            _set_entries_to_redis('income_entries', current_user.id, income_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m income_entries for user {current_user.id}")
            # Enrich with category names from income_categories
            income_cat_map = {cat['id']: cat['name'] for cat in income_categories}
            for entry in income_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = income_cat_map.get(entry.get('category_id'), 'Unknown')

        # Try Redis first for expense entries
        expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
        if expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
                ORDER BY ec.display_order DESC, ee.date ASC
            """, (current_user.id,))
            expense_entries = list(cursor.fetchall())
            expense_entries = _filter_pending_deletions('expense_entries', current_user.id, expense_entries)
            # Update Redis cache
            _set_entries_to_redis('expense_entries', current_user.id, expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m expense_entries for user {current_user.id}")
            # Enrich with category names from expense_categories
            expense_cat_map = {cat['id']: cat['name'] for cat in expense_categories}
            for entry in expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # Totals/remainders
        # Try Redis first
        totals_remainders_d = _get_totals_remainders_from_redis('totals_remainders_d', current_user.id)
        if totals_remainders_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m totals_remainders_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM totals_remainders_d
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            totals_remainders_d = cursor.fetchall()
            # Update Redis cache
            _set_totals_remainders_to_redis('totals_remainders_d', current_user.id, totals_remainders_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m totals_remainders_d for user {current_user.id}")

        # Savings
        # Try Redis first
        savings_entries = _get_savings_entries_from_redis(current_user.id)
        if savings_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m savings_entries for user {current_user.id}")
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            savings_entries = cursor.fetchall()
            # Update Redis cache
            _set_savings_entries_to_redis(current_user.id, savings_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m savings_entries for user {current_user.id}")

        # Credit accounts
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # CA categories
        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order ASC, cec.id ASC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # CA entries
        # Try Redis first
        c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
        if c_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m c_expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT cee.*, cec.name AS category_name
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
                ORDER BY cee.date DESC, cee.id ASC
            """, (current_user.id,))
            c_expense_entries = list(cursor.fetchall())
            c_expense_entries = _filter_pending_deletions('c_expense_entries', current_user.id, c_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('c_expense_entries', current_user.id, c_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m c_expense_entries for user {current_user.id}")
            # Enrich with category names from c_expense_categories
            c_expense_cat_map = {cat['id']: cat['name'] for cat in c_expense_categories}
            for entry in c_expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = c_expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # CA balances (daily)
        # Try Redis first
        c_a_balances_d = _get_ca_balances_from_redis('c_a_balances_d', current_user.id)
        if c_a_balances_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_m c_a_balances_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                )
                ORDER BY account_id ASC, date ASC
            """, (current_user.id,))
            c_a_balances_d = cursor.fetchall()
            # Update Redis cache
            _set_ca_balances_to_redis('c_a_balances_d', current_user.id, c_a_balances_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_m c_a_balances_d for user {current_user.id}")
        cursor.close()

    return render_template(
        'dashboard_m.html',
        selected_date=selected_date,
        goofy_week_mode=goofy_week_mode,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        income_categories=income_categories,
        expense_categories=expense_categories,
        income_entries=income_entries,
        expense_entries=expense_entries,
        totals_remainders_d=totals_remainders_d,
        balance_threshold=balance_threshold,
        savings_entries=savings_entries,
        member_since=member_since,
        landing_page=landing_page,
        currency_type=currency_type,
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances_d=c_a_balances_d
    )

@app.route('/get_dashboard_m_data', methods=['GET'])
@login_required
def get_dashboard_m_data():
    user_id = current_user.id

    # Support both month and explicit date range
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if start_date and end_date:
        from_date = start_date
        to_date = end_date
    else:
        # fallback to month param for backward compatibility
        month = request.args.get('month')
        if not month:
            return {"status": "error", "message": "No date range provided"}
        from calendar import monthrange
        year, month_num = map(int, month.split('-'))
        from_date = date(year, month_num, 1)
        to_date = date(year, month_num, monthrange(year, month_num)[1])

    # Try Redis first for daily totals/remainders
    totals_remainders_d = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    if totals_remainders_d is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_m_data totals_remainders_d for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM totals_remainders_d
                WHERE user_id = %s
                ORDER BY date ASC
            """, (user_id,))
            totals_remainders_d = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_totals_remainders_to_redis('totals_remainders_d', user_id, totals_remainders_d)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_m_data totals_remainders_d for user {user_id}")
    
    # Filter by date range
    from_date_obj = datetime.strptime(str(from_date), '%Y-%m-%d').date() if isinstance(from_date, str) else from_date
    to_date_obj = datetime.strptime(str(to_date), '%Y-%m-%d').date() if isinstance(to_date, str) else to_date
    totals_remainders_d = [
        row for row in totals_remainders_d 
        if from_date_obj <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= to_date_obj
    ]

    # Try Redis first for CA balances
    c_a_balances_d = _get_ca_balances_from_redis('c_a_balances_d', user_id)
    if c_a_balances_d is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_m_data c_a_balances_d for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                ORDER BY account_id ASC, date ASC
            """, (user_id,))
            c_a_balances_d = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_ca_balances_to_redis('c_a_balances_d', user_id, c_a_balances_d)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_m_data c_a_balances_d for user {user_id}")
    
    # Filter by date range
    c_a_balances_d = [
        row for row in c_a_balances_d 
        if from_date_obj <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= to_date_obj
    ]

    # Try Redis first for savings entries
    savings_entries = _get_savings_entries_from_redis(user_id)
    if savings_entries is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_m_data savings_entries for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (user_id,))
            savings_entries = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_savings_entries_to_redis(user_id, savings_entries)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_m_data savings_entries for user {user_id}")
    
    # Filter by date range
    savings_entries = [
        row for row in savings_entries 
        if from_date_obj <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= to_date_obj
    ]

    return {
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries
    }

############################################################################################
############################### DASHBOARD YEAR OVERVIEW ####################################
############################################################################################

@app.route('/dashboard_y')
@login_required
def dashboard_y():
    selected_date = request.args.get('date')
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] dashboard_y user settings for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] dashboard_y user settings: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
                FROM users
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] dashboard_y user settings for user {current_user.id}")

        profile_picture = user_data['profile_picture'] if user_data else None
        first_name = user_data['first_name'] if user_data else ''
        last_name = user_data['last_name'] if user_data else ''
        goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False
        balance_threshold = float(user_data.get('balance_threshold', 0)) if user_data else 0
        member_since = user_data['member_since'] if user_data else None
        currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
        landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard_3m'

        # Categories
        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_recurring
            FROM income_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        income_categories = cursor.fetchall()

        cursor.execute("""
            SELECT id, name, is_auto_adjustment, hidden, is_bud, is_recurring, is_credit_account
            FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Entries
        # Try Redis first for income entries
        income_entries = _get_entries_from_redis('income_entries', current_user.id)
        if income_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y income_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.user_id = %s
                ORDER BY ic.display_order DESC, ie.date ASC
            """, (current_user.id,))
            income_entries = list(cursor.fetchall())
            income_entries = _filter_pending_deletions('income_entries', current_user.id, income_entries)
            # Update Redis cache
            _set_entries_to_redis('income_entries', current_user.id, income_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y income_entries for user {current_user.id}")
            # Enrich with category names from income_categories
            income_cat_map = {cat['id']: cat['name'] for cat in income_categories}
            for entry in income_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = income_cat_map.get(entry.get('category_id'), 'Unknown')

        # Try Redis first for expense entries
        expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
        if expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ec.user_id = %s
                ORDER BY ec.display_order DESC, ee.date ASC
            """, (current_user.id,))
            expense_entries = list(cursor.fetchall())
            expense_entries = _filter_pending_deletions('expense_entries', current_user.id, expense_entries)
            # Update Redis cache
            _set_entries_to_redis('expense_entries', current_user.id, expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y expense_entries for user {current_user.id}")
            # Enrich with category names from expense_categories
            expense_cat_map = {cat['id']: cat['name'] for cat in expense_categories}
            for entry in expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # Totals/remainders
        # Try Redis first
        totals_remainders_d = _get_totals_remainders_from_redis('totals_remainders_d', current_user.id)
        if totals_remainders_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y totals_remainders_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM totals_remainders_d
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            totals_remainders_d = cursor.fetchall()
            # Update Redis cache
            _set_totals_remainders_to_redis('totals_remainders_d', current_user.id, totals_remainders_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y totals_remainders_d for user {current_user.id}")

        # Savings
        # Try Redis first
        savings_entries = _get_savings_entries_from_redis(current_user.id)
        if savings_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y savings_entries for user {current_user.id}")
            cursor.execute("""
                SELECT date, amount FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (current_user.id,))
            savings_entries = cursor.fetchall()
            # Update Redis cache
            _set_savings_entries_to_redis(current_user.id, savings_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y savings_entries for user {current_user.id}")

        # Credit accounts
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # CA categories
        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order ASC, cec.id ASC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # CA entries
        # Try Redis first
        c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
        if c_expense_entries is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y c_expense_entries for user {current_user.id}")
            cursor.execute("""
                SELECT cee.*, cec.name AS category_name
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
                ORDER BY cee.date DESC, cee.id ASC
            """, (current_user.id,))
            c_expense_entries = list(cursor.fetchall())
            c_expense_entries = _filter_pending_deletions('c_expense_entries', current_user.id, c_expense_entries)
            # Update Redis cache
            _set_entries_to_redis('c_expense_entries', current_user.id, c_expense_entries)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y c_expense_entries for user {current_user.id}")
            # Enrich with category names from c_expense_categories
            c_expense_cat_map = {cat['id']: cat['name'] for cat in c_expense_categories}
            for entry in c_expense_entries:
                if 'category_name' not in entry:
                    entry['category_name'] = c_expense_cat_map.get(entry.get('category_id'), 'Unknown')

        # CA balances (daily)
        # Try Redis first
        c_a_balances_d = _get_ca_balances_from_redis('c_a_balances_d', current_user.id)
        if c_a_balances_d is None:
            # Redis miss - fallback to MySQL
            app.logger.info(f"[REDIS MISS] dashboard_y c_a_balances_d for user {current_user.id}")
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (
                    SELECT id FROM credit_accounts WHERE user_id = %s
                )
                ORDER BY account_id ASC, date ASC
            """, (current_user.id,))
            c_a_balances_d = cursor.fetchall()
            # Update Redis cache
            _set_ca_balances_to_redis('c_a_balances_d', current_user.id, c_a_balances_d)
        else:
            app.logger.debug(f"[REDIS HIT] dashboard_y c_a_balances_d for user {current_user.id}")
        cursor.close()

    return render_template(
        'dashboard_y.html',
        selected_date=selected_date,
        goofy_week_mode=goofy_week_mode,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        income_categories=income_categories,
        expense_categories=expense_categories,
        income_entries=income_entries,
        expense_entries=expense_entries,
        totals_remainders_d=totals_remainders_d,
        balance_threshold=balance_threshold,
        savings_entries=savings_entries,
        member_since=member_since,
        landing_page=landing_page,
        currency_type=currency_type,
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances_d=c_a_balances_d
    )

@app.route('/get_dashboard_y_data', methods=['GET'])
@login_required
def get_dashboard_y_data():
    year = int(request.args.get('year', datetime.utcnow().year))
    user_id = current_user.id
    first_day = date(year, 1, 1)
    last_day = date(year, 12, 31)

    # Try Redis first for totals/remainders
    totals_remainders_d = _get_totals_remainders_from_redis('totals_remainders_d', user_id)
    if totals_remainders_d is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_y_data totals_remainders_d for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM totals_remainders_d
                WHERE user_id = %s
                ORDER BY date ASC
            """, (user_id,))
            totals_remainders_d = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_totals_remainders_to_redis('totals_remainders_d', user_id, totals_remainders_d)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_y_data totals_remainders_d for user {user_id}")
    
    # Filter by year
    totals_remainders_d = [
        row for row in totals_remainders_d 
        if first_day <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= last_day
    ]

    # Try Redis first for CA balances
    c_a_balances_d = _get_ca_balances_from_redis('c_a_balances_d', user_id)
    if c_a_balances_d is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_y_data c_a_balances_d for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM c_a_balances_d
                WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
                ORDER BY account_id ASC, date ASC
            """, (user_id,))
            c_a_balances_d = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_ca_balances_to_redis('c_a_balances_d', user_id, c_a_balances_d)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_y_data c_a_balances_d for user {user_id}")
    
    # Filter by year
    c_a_balances_d = [
        row for row in c_a_balances_d 
        if first_day <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= last_day
    ]

    # Try Redis first for savings entries
    savings_entries = _get_savings_entries_from_redis(user_id)
    if savings_entries is None:
        # Redis miss - fallback to MySQL
        app.logger.info(f"[REDIS MISS] get_dashboard_y_data savings_entries for user {user_id}")
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM savings_entries
                WHERE user_id = %s
                ORDER BY date ASC
            """, (user_id,))
            savings_entries = cursor.fetchall()
            cursor.close()
        # Update Redis cache
        _set_savings_entries_to_redis(user_id, savings_entries)
    else:
        app.logger.debug(f"[REDIS HIT] get_dashboard_y_data savings_entries for user {user_id}")
    
    # Filter by year
    savings_entries = [
        row for row in savings_entries 
        if first_day <= (datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']) <= last_day
    ]

    return {
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries
    }

############################################################################################
############################### PROFILE PAGE ###############################################
############################################################################################

@app.route('/profile', methods=['GET'])
@login_required
def profile():
    # Try to get user settings from Redis first
    redis_key = f"users:v1:{current_user.id}"
    user_data = None
    
    if app.config.get('REDIS_OK'):
        try:
            cached = _redis_client.get(redis_key)
            if cached:
                user_data = json.loads(cached)
        except Exception as e:
            app.logger.warning(f"[REDIS][user_settings] GET error: {e}")
    
    # If not in Redis, load from MySQL
    if not user_data:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch profile picture, first name, last name, balance threshold, goofy_week_mode, landing_page, currency_type, and mfa_secret
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, landing_page, currency_type, mfa_secret
                FROM users 
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            cursor.close()
    
    # Fetch starting balance from income_entries (check Redis first)
    income_entries = _get_entries_from_redis('income_entries', current_user.id)
    
    starting_balance_data = None
    if income_entries is None:
        # Load from MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT ie.amount, ic.id as category_id
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ic.name = 'Starting Balance' AND ic.user_id = %s 
                LIMIT 1
            """, (current_user.id,))
            starting_balance_data = cursor.fetchone()
            cursor.close()
    else:
        # Find Starting Balance entry in Redis data
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT id FROM income_categories WHERE name = 'Starting Balance' AND user_id = %s LIMIT 1
            """, (current_user.id,))
            cat_row = cursor.fetchone()
            cursor.close()
        
        if cat_row:
            starting_balance_entry = next((e for e in income_entries if int(e.get('category_id', 0)) == int(cat_row['id'])), None)
            if starting_balance_entry:
                starting_balance_data = {'amount': starting_balance_entry.get('amount', 0)}

    # Extract values from the query result
    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = int(user_data['balance_threshold']) if user_data and user_data['balance_threshold'] is not None else 0
    goofy_week_mode = user_data['goofy_week_mode'] if user_data else 0
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard_3m'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    starting_balance = int(starting_balance_data['amount']) if starting_balance_data and starting_balance_data['amount'] is not None else 0
    mfa_enabled = bool(user_data['mfa_secret']) if user_data and 'mfa_secret' in user_data else False

    # Pass all retrieved data to the template
    return render_template(
        'profile.html',
        current_username=current_user.username,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        starting_balance=starting_balance,
        balance_threshold=balance_threshold,
        goofy_week_mode=goofy_week_mode,
        landing_page=landing_page,
        currency_type=currency_type,
        mfa_enabled=mfa_enabled
    )

@app.route('/settings', methods=['GET'])
@login_required
def settings():
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Fetch profile picture, first name, last name, balance threshold, goofy_week_mode, landing_page, currency_type, and mfa_secret
        # Try Redis first
        user_data = None
        redis_key = f"users:v1:{current_user.id}"
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    app.logger.debug(f"[REDIS HIT] settings user data for user {current_user.id}")
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] settings user data: {str(e)}")
        
        # Fallback to MySQL
        if not user_data:
            cursor.execute("""
                SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, landing_page, currency_type, mfa_secret
                FROM users 
                WHERE id = %s
            """, (current_user.id,))
            user_data = cursor.fetchone()
            app.logger.debug(f"[MYSQL] settings user data for user {current_user.id}")

        # Fetch starting balance from income_entries where category is "Starting Balance"
        cursor.execute("""
            SELECT amount
            FROM income_entries 
            WHERE category_id = (
                SELECT id FROM income_categories WHERE name = 'Starting Balance' AND user_id = %s LIMIT 1
            ) 
            LIMIT 1
        """, (current_user.id,))
        starting_balance_data = cursor.fetchone()

        cursor.close()

    # Extract values from the query result
    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = int(user_data['balance_threshold']) if user_data and user_data['balance_threshold'] is not None else 0
    goofy_week_mode = user_data['goofy_week_mode'] if user_data else 0
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard_3m'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    starting_balance = int(starting_balance_data['amount']) if starting_balance_data and starting_balance_data['amount'] is not None else 0
    mfa_enabled = bool(user_data['mfa_secret']) if user_data and 'mfa_secret' in user_data else False

    # Pass all retrieved data to the template
    return render_template(
        'settings.html',
        current_username=current_user.username,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        starting_balance=starting_balance,
        balance_threshold=balance_threshold,
        goofy_week_mode=goofy_week_mode,
        landing_page=landing_page,
        currency_type=currency_type,
        mfa_enabled=mfa_enabled
    )

@app.route('/update_goofy_week_mode', methods=['POST'])
@login_required
def update_goofy_week_mode():
    goofy_week_mode = request.form.get('goofy_week_mode', type=int)
    
    try:
        # Update in Redis only - flush worker will persist to MySQL
        _update_user_setting_in_redis(current_user.id, 'goofy_week_mode', goofy_week_mode)
        
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/update_profile_picture', methods=['POST'])
@login_required
def update_profile_picture():
    if 'profile_picture' not in request.files:
        flash('No file part')
        return redirect(url_for('profile'))

    file = request.files['profile_picture']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('profile'))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        # Get the current user's profile picture from Redis first
        redis_key = f"users:v1:{current_user.id}"
        user_data = None
        if app.config.get('REDIS_OK'):
            try:
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
            except Exception as e:
                app.logger.error(f"[REDIS ERROR] profile picture lookup: {str(e)}")
        
        # Fallback to MySQL if not in Redis
        if not user_data:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("SELECT profile_picture FROM users WHERE id = %s", (current_user.id,))
                user_data = cursor.fetchone()
                cursor.close()

        # Delete the old profile picture file if it exists
        if user_data and user_data.get('profile_picture'):
            old_filepath = os.path.join(app.config['UPLOAD_FOLDER'], user_data['profile_picture'])
            if os.path.exists(old_filepath):
                os.remove(old_filepath)

        # Save the new profile picture file
        file.save(filepath)

        # Update in Redis only - flush worker will persist to MySQL
        _update_user_setting_in_redis(current_user.id, 'profile_picture', filename)

        flash('Profile picture updated successfully.')
        return redirect(url_for('profile'))
    else:
        flash('Invalid file type')
        return redirect(url_for('profile'))

@app.route('/update_first_name', methods=['POST'])
@login_required
def update_first_name():
    new_first_name = request.form['first_name']
    
    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'first_name', new_first_name)

    flash('First name updated successfully.')
    return redirect(url_for('profile', success='first_name'))

@app.route('/update_last_name', methods=['POST'])
@login_required
def update_last_name():
    new_last_name = request.form['last_name']
    
    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'last_name', new_last_name)

    flash('Last name updated successfully.')
    return redirect(url_for('profile', success='last_name'))

@app.route('/update_username', methods=['POST'])
@login_required
def update_username():
    new_username = request.form['username']

    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'username', new_username)

    flash('Username updated successfully.')
    return redirect(url_for('profile', success='email'))

@app.route('/update_password', methods=['POST'])
@login_required
def update_password():
    new_password = request.form['password']

    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'password', hashed_password)

    flash('Password updated successfully.')
    return redirect(url_for('profile', success='password'))

@app.route('/enable_mfa', methods=['POST'])
@login_required
def enable_mfa():
    try:
        # Generate a new secret
        secret = pyotp.random_base32()
        
        # Update in Redis only - flush worker will persist to MySQL
        _update_user_setting_in_redis(current_user.id, 'mfa_secret', secret)

        # Generate provisioning URI for Google Authenticator
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name="Blankee")

        # Generate QR code as base64
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        qr_url = f"data:image/png;base64,{qr_b64}"

        return jsonify({'status': 'success', 'qr_url': qr_url})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/verify_mfa', methods=['POST'])
@login_required
def verify_mfa():
    code = request.form.get('code')
    
    # Get mfa_secret from Redis first
    redis_key = f"users:v1:{current_user.id}"
    user_data = None
    secret = None
    
    if app.config.get('REDIS_OK'):
        try:
            cached = _redis_client.get(redis_key)
            if cached:
                user_data = json.loads(cached)
                secret = user_data.get('mfa_secret')
        except Exception as e:
            app.logger.error(f"[REDIS ERROR] verify_mfa lookup: {str(e)}")
    
    # Fallback to MySQL if not in Redis
    if not secret:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT mfa_secret FROM users WHERE id = %s", (current_user.id,))
            result = cursor.fetchone()
            cursor.close()
            
            if result and result[0]:
                secret = result[0]
        
    if not secret:
        return jsonify({'status': 'error', 'message': 'No MFA secret set'}), 400
    
    totp = pyotp.TOTP(secret)
    if totp.verify(code):
        # Optionally set a session flag for MFA
        return jsonify({'status': 'success'})
    else:
        return jsonify({'status': 'error', 'message': 'Invalid code'}), 400

@app.route('/disable_mfa', methods=['POST'])
@login_required
def disable_mfa():
    # Update in Redis only - flush worker will persist to MySQL
    _update_user_setting_in_redis(current_user.id, 'mfa_secret', None)
    
    return jsonify({'status': 'success'})

@app.route('/update_starting_balance', methods=['POST'])
@login_required
def update_starting_balance():
    # Retrieve the new starting balance from the form
    new_balance = request.form.get('starting_balance')

    if new_balance:
        # Update starting_savings in Redis (will be flushed to MySQL)
        _update_user_setting_in_redis(current_user.id, 'starting_savings', float(new_balance))
        
        # Also update the Starting Balance income entry
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Find the 'Starting Balance' category for the current user
            cursor.execute("""
                SELECT id FROM income_categories 
                WHERE name = 'Starting Balance' AND user_id = %s LIMIT 1
            """, (current_user.id,))
            
            result = cursor.fetchone()

            if result:
                STARTING_BALANCE_CATEGORY_ID = result['id']

                # Check if there is already an entry in Redis or MySQL
                income_entries = _get_entries_from_redis('income_entries', current_user.id)
                
                if income_entries is None:
                    # Load from MySQL
                    cursor.execute("""
                        SELECT ie.* FROM income_entries ie
                        JOIN income_categories ic ON ie.category_id = ic.id
                        WHERE ic.user_id = %s
                    """, (current_user.id,))
                    income_entries = list(cursor.fetchall())
                
                # Find existing starting balance entry
                existing_entry = next((e for e in income_entries if int(e.get('category_id', 0)) == int(STARTING_BALANCE_CATEGORY_ID)), None)
                
                if existing_entry:
                    # Update existing entry in Redis
                    entry_date = existing_entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    _update_entry_in_redis('income_entries', current_user.id, 
                                         STARTING_BALANCE_CATEGORY_ID, entry_date, 
                                         float(new_balance))
                else:
                    # Create new entry in Redis
                    _update_entry_in_redis('income_entries', current_user.id, 
                                         STARTING_BALANCE_CATEGORY_ID, date.today(), 
                                         float(new_balance))

            cursor.close()

    return redirect(url_for('profile'))

@app.route('/update_balance_threshold', methods=['POST'])
@login_required
def update_balance_threshold():
    # Retrieve the new balance threshold from the form
    new_threshold = request.form.get('balance_threshold')
    
    app.logger.info(f"[UPDATE THRESHOLD] User {current_user.id}: new_threshold='{new_threshold}' (type: {type(new_threshold)})")
    app.logger.info(f"[UPDATE THRESHOLD] request.form: {dict(request.form)}")
    
    if new_threshold:
        # Update balance_threshold in Redis (will be flushed to MySQL)
        _update_user_setting_in_redis(current_user.id, 'balance_threshold', float(new_threshold))
        app.logger.info(f"[UPDATE THRESHOLD] Successfully called _update_user_setting_in_redis for user {current_user.id}")
    else:
        app.logger.warning(f"[UPDATE THRESHOLD] new_threshold is empty/None for user {current_user.id}")

    return redirect(url_for('profile'))



@app.route('/remove_profile_picture', methods=['POST'])
@login_required
def remove_profile_picture():
    # Get current profile picture from Redis first
    redis_key = f"users:v1:{current_user.id}"
    user_data = None
    profile_pic = None
    
    if app.config.get('REDIS_OK'):
        try:
            cached = _redis_client.get(redis_key)
            if cached:
                user_data = json.loads(cached)
                profile_pic = user_data.get('profile_picture')
        except Exception as e:
            app.logger.error(f"[REDIS ERROR] remove_profile_picture lookup: {str(e)}")
    
    # Fallback to MySQL if not in Redis
    if profile_pic is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT profile_picture FROM users WHERE id = %s", (current_user.id,))
            result = cursor.fetchone()
            cursor.close()
            
            if result and result[0]:
                profile_pic = result[0]

    if profile_pic:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], profile_pic)
        if os.path.exists(filepath) and profile_pic != 'DefaultProfilePicture.svg':
            os.remove(filepath)  # Delete the file from the server

        # Update in Redis only - flush worker will persist to MySQL
        _update_user_setting_in_redis(current_user.id, 'profile_picture', None)

    return jsonify({'status': 'success'})  # Return a JSON response indicating success

@app.route('/delete_user/<username>', methods=['POST'])
@login_required
def delete_user(username):
    if username != current_user.username:
        flash('You can only delete your own account.')
        return jsonify({'status': 'error', 'message': 'You can only delete your own account.'})

    # Get user_id before deletion
    user_id = current_user.id

    # Dehydrate user data from Redis before deleting from MySQL
    if app.config.get('REDIS_OK'):
        try:
            from redis_manager import invalidate_user_cache
            invalidate_user_cache(user_id)
            app.logger.info(f"[USER DELETE] Dehydrated user {user_id} ({username}) from Redis")
        except Exception as e:
            app.logger.warning(f"[USER DELETE] Failed to dehydrate user {user_id}: {e}")

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT profile_picture FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()

        if user_data and user_data[0]:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], user_data[0])
            if os.path.exists(filepath) and user_data[0] != 'DefaultProfilePicture.svg':
                os.remove(filepath)

        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        cursor.close()

    logout_user()
    return jsonify({'status': 'success'})

if __name__ == "__main__":
    app.run(debug=True)

from flask import jsonify, request

@app.route('/update_currency_type', methods=['POST'])
@login_required
def update_currency_type():
    currency_type = request.form.get('currency_type')
    if currency_type not in ['USD', 'EUR']:
        return jsonify({'status': 'error', 'message': 'Invalid currency type'}), 400

    try:
        # Update in Redis only - flush worker will persist to MySQL
        _update_user_setting_in_redis(current_user.id, 'currency_type', currency_type)
        
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

################################################################################
############################### RECURRING INCOME ###############################
################################################################################

@app.route('/recurring-income')
@login_required
def recurring_income():
    # Try Redis first
    recurring_income_records = _get_recurring_from_redis('recurring_income', current_user.id)
    
    if recurring_income_records is None:
        # Fallback to MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            # Fetch recurring income records, including no_end_date and name from income_categories
            cursor.execute("""
                SELECT ri.id, ri.user_id, ri.category_id, ic.name as category_name, ri.amount, 
                       ri.cadence_interval, ri.cadence_unit, ri.weekdays, ri.monthly_days, 
                       ri.start_date, ri.end_date, ri.yearly_day, ri.yearly_month,
                       ic.no_end_date
                FROM recurring_income ri
                JOIN income_categories ic ON ri.category_id = ic.id
                WHERE ri.user_id = %s
            """, (current_user.id,))

            recurring_income_records = cursor.fetchall()
            cursor.close()
            
            # Cache to Redis
            _set_recurring_to_redis('recurring_income', current_user.id, recurring_income_records)
    
    # Fetch user profile data
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT profile_picture, first_name, last_name, landing_page, currency_type FROM users WHERE id = %s", (current_user.id,))
        user_data = cursor.fetchone()
        cursor.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard_3m'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    # Calculate December 31st, 3 years from now
    current_date = date.today()
    no_end_date = date(current_date.year + 3, 12, 31)

    # Format cadence for display and check for 'No end date'
    for record in recurring_income_records:
        record['cadence_description'] = get_cadence_description(
            record['cadence_interval'], 
            record['cadence_unit'], 
            record['weekdays'], 
            record['monthly_days'], 
            record['yearly_day'], 
            record['yearly_month']
        )
        # Check if the end_date equals 'no_end_date'
        if record['end_date'] == no_end_date.strftime('%Y-%m-%d'):
            record['display_end_date'] = 'No end date'
        else:
            record['display_end_date'] = record['end_date']

    # Pass landing_page to the template
    return render_template(
        'recurring_i.html', 
        recurring_income_records=recurring_income_records,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        current_date=current_date,
        landing_page=landing_page,
        currency_type=currency_type
    )

@app.route('/add-recurring-income', methods=['POST'])
@login_required
def add_recurring_income():
    # Extract JSON data from the request
    data = request.get_json()

    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    # Extract data from the request
    category_name = data.get('category_name')
    amount = data.get('amount')
    cadence_interval = data.get('cadence_interval')
    cadence_unit = data.get('cadence_unit')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    weekdays = data.get('weekdays')  # Extract the weekdays array
    monthly_days = data.get('monthly_days')  # Extract the multiple monthly days array, if any
    yearly_day = data.get('yearly_day')  # Extract the yearly day, if any
    yearly_month = data.get('yearly_month')  # Extract the yearly month, if any
    no_end_date = data.get('no_end_date', 0)

    # Validate the data
    if not all([category_name, amount, cadence_interval, cadence_unit, start_date, end_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 1: Find the current max display order for income categories
            cursor.execute("""
                SELECT COALESCE(MAX(display_order), 0) FROM income_categories WHERE user_id = %s
            """, (current_user.id,))
            max_display_order = cursor.fetchone()[0]

            # Step 2: Insert the new category with the next display order
            cursor.execute("""
                INSERT INTO income_categories (user_id, name, display_order, is_recurring, no_end_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (current_user.id, category_name, max_display_order + 1, 1, no_end_date))

            # Get the ID of the newly inserted income category
            category_id = cursor.lastrowid

            # Step 3: Create recurring income record in Redis
            recurring_data = {
                'id': None,  # Will be generated by Redis helper
                'user_id': current_user.id,
                'category_id': category_id,
                'category_name': category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': ','.join(map(str, monthly_days)) if monthly_days else None,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_income', current_user.id, recurring_data)
            
            # Use a temporary ID for generating entries (will be replaced on flush)
            recurring_id = recurring_data['id']
            
            # Commit the category changes
            conn.commit()
            cursor.close()

        # Generate income entries based on the cadence
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_income_entries(
                recurring_id, category_id, amount, cadence_interval, cadence_unit, 
                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
            )

        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring income added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring income: {str(e)}'}), 500


def generate_income_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    # Get user_id from the category
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT user_id FROM income_categories WHERE id = %s", (category_id,))
        result = cursor.fetchone()
        cursor.close()
        if not result:
            return
        user_id = result['user_id']

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            # Insert entry to Redis
            _update_entry_in_redis('income_entries', user_id, category_id, current_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    # Insert entry to Redis
                    _update_entry_in_redis('income_entries', user_id, category_id, weekday_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(weeks=int(cadence_interval))

        elif cadence_unit == 'months':
                if monthly_days:
                    # Convert all days to int, except 'Last Day'
                    monthly_days_cleaned = []
                    for day in monthly_days:
                        if str(day).lower() == 'last day':
                            monthly_days_cleaned.append('Last Day')
                        else:
                            try:
                                monthly_days_cleaned.append(int(day))
                            except Exception:
                                continue

                    # Loop through each month from start_date to end_date
                    year = current_date.year
                    month = current_date.month
                    while True:
                        for day in monthly_days_cleaned:
                            try:
                                if str(day).lower() == 'last day':
                                    day_num = calendar.monthrange(year, month)[1]
                                else:
                                    day_num = int(day)
                                    last_day_of_month = calendar.monthrange(year, month)[1]
                                    if day_num > last_day_of_month:
                                        continue  # Skip invalid days
                                entry_date = date(year=year, month=month, day=day_num)
                            except Exception:
                                continue
                            if entry_date < start_date:
                                continue
                            if entry_date > end_date:
                                continue
                            # Insert entry to Redis
                            _update_entry_in_redis('income_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        # Move to next month by cadence_interval
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        # Stop if we've passed the end date's year and month
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                else:
                    # Default to the first day of each month
                    year = current_date.year
                    month = current_date.month
                    while True:
                        entry_date = date(year=year, month=month, day=1)
                        if entry_date < start_date:
                            pass
                        elif entry_date > end_date:
                            break
                        else:
                            # Insert entry to Redis
                            _update_entry_in_redis('income_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        # Move to next month by cadence_interval
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                break  # Exit the outer while loop after handling months

        elif cadence_unit == 'years':
            if yearly_day and yearly_month:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    try:
                        yearly_entry_date = date(year=year, month=int(yearly_month), day=int(yearly_day))
                    except ValueError:
                        year += interval
                        continue
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('income_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval
            else:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    yearly_entry_date = date(year=year, month=1, day=1)
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('income_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval

        # Increment current_date
        if delta:
            current_date += delta
        else:
            break

@app.route('/delete-recurring-income', methods=['POST'])
@login_required
def delete_recurring_income():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        # First, try to find the recurring record in Redis
        category_id = None
        cached_recurring = _get_recurring_from_redis('recurring_income', current_user.id)
        if cached_recurring:
            for rec in cached_recurring:
                if rec.get('id') == int(recurring_id):
                    category_id = rec.get('category_id')
                    break
        
        # If not found in Redis (or Redis not available), check MySQL
        if category_id is None:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_id FROM recurring_income WHERE id = %s AND user_id = %s
                """, (recurring_id, current_user.id))
                category = cursor.fetchone()
                cursor.close()
                if not category:
                    return jsonify({'status': 'error', 'message': 'Recurring income not found.'}), 404
                category_id = category[0]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Find Auto Adjustments income category for this user
            cursor.execute("""
                SELECT id FROM income_categories WHERE user_id = %s AND name = 'Auto Adjustments' LIMIT 1
            """, (current_user.id,))
            auto_adj_row = cursor.fetchone()
            auto_adj_id = auto_adj_row[0] if auto_adj_row else None

            today = date.today()
            cursor.execute("""
                SELECT id, date, amount FROM income_entries
                WHERE recurring_id = %s AND date < %s
            """, (recurring_id, today))
            old_entries = cursor.fetchall()
            for entry_id, entry_date, amount in old_entries:
                cursor.execute("""
                    SELECT id, amount FROM income_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE income_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    cursor.execute("""
                        INSERT INTO income_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Set recurring_id to NULL when moving
                cursor.execute("UPDATE income_entries SET recurring_id = NULL WHERE id = %s", (entry_id,))
                cursor.execute("DELETE FROM income_entries WHERE id = %s", (entry_id,))

            # Delete all future entries from Redis
            _delete_entry_in_redis('income_entries', current_user.id, category_id, today, date(9999, 12, 31))
            
            # Delete the recurring record from Redis (this will mark it for deletion)
            _delete_recurring_in_redis('recurring_income', current_user.id, recurring_id)
            
            # Delete the category from MySQL immediately
            # Note: Categories aren't separately cached in Redis yet, but they're included in recurring records
            # The _delete_recurring_in_redis call above handles removing the recurring record (with its category_name)
            cursor.execute("DELETE FROM income_categories WHERE id = %s AND user_id = %s", (category_id, current_user.id))

            conn.commit()
            cursor.close()
        
        return jsonify({'status': 'success', 'message': 'Recurring income and associated category deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring income.'}), 500

@app.route('/update-recurring-income', methods=['POST'])
@login_required
def update_recurring_income():
    data = request.get_json()
    return update_recurring_income_inner(data, current_user.id)

def update_recurring_income_inner(data, user_id):
    try:
        # Get the request data sent via AJAX
        recurring_id = data.get('recurring_id')
        category_name = data.get('category_name')
        amount = data.get('amount')
        cadence_interval = data.get('cadence_interval')
        cadence_unit = data.get('cadence_unit')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        weekdays = data.get('weekdays')  # Extract the weekdays array
        monthly_days = data.get('monthly_days')  # Extract the array of monthly days, if any
        yearly_day = data.get('yearly_day')  # Extract the yearly day, if any
        yearly_month = data.get('yearly_month')  # Extract the yearly month, if any
        no_end_date = data.get('no_end_date', 0)

        # Validate the data
        if not all([recurring_id, category_name, amount, cadence_interval, cadence_unit, start_date, end_date]):
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        # Ensure that the start date is today or later
        today = datetime.today().date()
        if datetime.strptime(start_date, '%Y-%m-%d').date() < today:
            return jsonify({'status': 'error', 'message': 'Start date cannot be earlier than today'}), 400

        # Ensure that the end date is not earlier than the start date
        if datetime.strptime(end_date, '%Y-%m-%d') < datetime.strptime(start_date, '%Y-%m-%d'):
            return jsonify({'status': 'error', 'message': 'End date cannot be earlier than start date'}), 400

        # First, try to find the recurring record in Redis (handles temp negative IDs)
        cached_recurring = _get_recurring_from_redis('recurring_income', user_id)
        category_id = None
        
        if cached_recurring:
            for rec in cached_recurring:
                if int(rec.get('id')) == int(recurring_id):
                    category_id = rec.get('category_id')
                    break
        
        # If not in Redis, fall back to MySQL
        if not category_id:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_id FROM recurring_income WHERE id = %s AND user_id = %s
                """, (recurring_id, user_id))
                result = cursor.fetchone()
                cursor.close()
                if not result:
                    return jsonify({'status': 'error', 'message': 'Recurring income not found'}), 404
                category_id = result[0]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 2: Update the category name in the income_categories table
            cursor.execute("""
                UPDATE income_categories
                SET name = %s, is_recurring = 1, no_end_date = %s
                WHERE id = %s
            """, (category_name, no_end_date, category_id))

            # Convert the monthly_days array into a comma-separated string for storage, if it exists
            monthly_days_str = ','.join(map(str, monthly_days)) if monthly_days else None

            # Step 2: Update the recurring income record in Redis
            recurring_data = {
                'id': recurring_id,
                'user_id': user_id,
                'category_id': category_id,
                'category_name': category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': monthly_days_str,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_income', user_id, recurring_data)

            # Step 3: Delete old income entries for today and the future from Redis
            _delete_entry_in_redis('income_entries', user_id, category_id, today, date(9999, 12, 31))

            # Commit the category changes
            conn.commit()
            cursor.close()

        # Step 4: Recreate the income entries with the updated details (only for today and future)
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_income_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit,
                                    start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month)

        return jsonify({'status': 'success', 'message': 'Recurring income updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500


def get_cadence_description(cadence_interval, cadence_unit, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    # Mapping for custom weekday abbreviations
    custom_weekday_abbreviations = {
        'monday': 'M',
        'tuesday': 'T',
        'wednesday': 'W',
        'thursday': 'Th',
        'friday': 'F',
        'saturday': 'S',
        'sunday': 'Su'
    }

    if cadence_unit == 'days':
        return f"Every {cadence_interval} day(s)"
    
    elif cadence_unit == 'weeks':
        if weekdays:
            # If weekdays is a string, split it into a list
            if isinstance(weekdays, str):
                weekdays = weekdays.split(',')

            # Ensure only valid weekdays are included and skip None/empty values
            valid_weekdays = [day.strip() for day in weekdays if day.strip().lower() in custom_weekday_abbreviations]
            
            if valid_weekdays:
                weekdays_str = ', '.join([custom_weekday_abbreviations[day.lower()] for day in valid_weekdays])
                return f"Every {cadence_interval} week(s) on {weekdays_str}"
            else:
                return f"Every {cadence_interval} week(s)"  # No valid weekdays to display
        else:
            return f"Every {cadence_interval} week(s)"
    
    elif cadence_unit == 'months':
        if monthly_days:
            # Clean the monthly_days list by splitting if it's a string
            if isinstance(monthly_days, str):
                monthly_days = [day.strip() for day in monthly_days.split(',') if day.strip()]

            # Remove duplicates and sort the days numerically
            monthly_days_cleaned = sorted(set(monthly_days), key=lambda x: int(x) if x.isdigit() else float('inf'))

            if monthly_days_cleaned:
                monthly_days_str = ', '.join(monthly_days_cleaned)
                return f"Every {cadence_interval} month(s) on days {monthly_days_str}"
            else:
                return f"Every {cadence_interval} month(s)"  # No valid monthly days
        else:
            return f"Every {cadence_interval} month(s)"
    
    elif cadence_unit == 'years':
        if yearly_day and yearly_month:
            # Get the month name based on the integer month
            month_name = calendar.month_name[int(yearly_month)]
            return f"Every {cadence_interval} year(s) on {month_name} {yearly_day}"
        else:
            return f"Every {cadence_interval} year(s)"

    # Fallback for unexpected or custom cadence cases
    return f"Custom Cadence: every {cadence_interval} {cadence_unit}(s)"

####################################################################################
############################### RECURRING EXPENSES #################################
####################################################################################

@app.route('/recurring-expense')
@login_required
def recurring_expense():
    # Try Redis first
    recurring_expense_records = _get_recurring_from_redis('recurring_expense', current_user.id)
    
    if recurring_expense_records is None:
        # Fallback to MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            # Fetch recurring expense records, including no_end_date and name from expense_categories
            cursor.execute("""
                SELECT ri.id, ri.user_id, ri.category_id, ic.name as category_name, ri.amount, 
                       ri.cadence_interval, ri.cadence_unit, ri.weekdays, ri.monthly_days, 
                       ri.start_date, ri.end_date, ri.yearly_day, ri.yearly_month,
                       ic.no_end_date
                FROM recurring_expense ri
                JOIN expense_categories ic ON ri.category_id = ic.id
                WHERE ri.user_id = %s
            """, (current_user.id,))

            recurring_expense_records = cursor.fetchall()
            cursor.close()
            
            # Cache to Redis
            _set_recurring_to_redis('recurring_expense', current_user.id, recurring_expense_records)
    
    # Fetch user profile data
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT profile_picture, first_name, last_name, landing_page, currency_type FROM users WHERE id = %s", (current_user.id,))
        user_data = cursor.fetchone()
        cursor.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard_3m'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    # Calculate December 31st, 3 years from now
    current_date = date.today()
    no_end_date = date(current_date.year + 3, 12, 31)

    # Format cadence for display and check for 'No end date'
    for record in recurring_expense_records:
        record['cadence_description'] = get_cadence_description(
            record['cadence_interval'], 
            record['cadence_unit'], 
            record['weekdays'], 
            record['monthly_days'], 
            record['yearly_day'], 
            record['yearly_month']
        )
        # Check if the end_date equals 'no_end_date'
        if record['end_date'] == no_end_date.strftime('%Y-%m-%d'):
            record['display_end_date'] = 'No end date'
        else:
            record['display_end_date'] = record['end_date']

    # Pass landing_page to the template
    return render_template(
        'recurring_e.html', 
        recurring_expense_records=recurring_expense_records,
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        current_date=current_date,
        landing_page=landing_page,
        currency_type=currency_type
    )

@app.route('/add-recurring-expense', methods=['POST'])
@login_required
def add_recurring_expense():
    # Extract JSON data from the request
    data = request.get_json()

    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    # Extract data from the request
    category_name = data.get('category_name')
    amount = data.get('amount')
    cadence_interval = data.get('cadence_interval')
    cadence_unit = data.get('cadence_unit')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    weekdays = data.get('weekdays')  # Extract the weekdays array
    monthly_days = data.get('monthly_days')  # Extract the multiple monthly days array, if any
    yearly_day = data.get('yearly_day')  # Extract the yearly day, if any
    yearly_month = data.get('yearly_month')  # Extract the yearly month, if any
    no_end_date = data.get('no_end_date', 0)

    # Validate the data
    if not all([category_name, amount, cadence_interval, cadence_unit, start_date, end_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 1: Find the current max display order for expense categories
            cursor.execute("""
                SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s
            """, (current_user.id,))
            max_display_order = cursor.fetchone()[0]

            # Step 2: Insert the new category with the next display order
            cursor.execute("""
                INSERT INTO expense_categories (user_id, name, display_order, is_recurring, no_end_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (current_user.id, category_name, max_display_order + 1, 1, no_end_date))

            # Get the ID of the newly inserted expense category
            category_id = cursor.lastrowid

            # Step 3: Create recurring expense record in Redis
            recurring_data = {
                'id': None,  # Will be generated by Redis helper
                'user_id': current_user.id,
                'category_id': category_id,
                'category_name': category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': ','.join(map(str, monthly_days)) if monthly_days else None,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_expense', current_user.id, recurring_data)
            
            # Use a temporary ID for generating entries (will be replaced on flush)
            recurring_id = recurring_data['id']
            
            # Commit the category changes
            conn.commit()
            cursor.close()

        # Generate expense entries based on the cadence
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_expense_entries(
                recurring_id, category_id, amount, cadence_interval, cadence_unit,
                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
            )
        
        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring expense added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring expense: {str(e)}'}), 500


def generate_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    # Get user_id from the category
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT user_id FROM expense_categories WHERE id = %s", (category_id,))
        result = cursor.fetchone()
        cursor.close()
        if not result:
            return
        user_id = result['user_id']

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            # Insert entry to Redis
            _update_entry_in_redis('expense_entries', user_id, category_id, current_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    # Insert entry to Redis
                    _update_entry_in_redis('expense_entries', user_id, category_id, weekday_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(weeks=int(cadence_interval))

        elif cadence_unit == 'months':
                if monthly_days:
                    # Convert all days to int, except 'Last Day'
                    monthly_days_cleaned = []
                    for day in monthly_days:
                        if str(day).lower() == 'last day':
                            monthly_days_cleaned.append('Last Day')
                        else:
                            try:
                                monthly_days_cleaned.append(int(day))
                            except Exception:
                                continue

                    # Loop through each month from start_date to end_date
                    year = current_date.year
                    month = current_date.month
                    while True:
                        for day in monthly_days_cleaned:
                            try:
                                if str(day).lower() == 'last day':
                                    day_num = calendar.monthrange(year, month)[1]
                                else:
                                    day_num = int(day)
                                    last_day_of_month = calendar.monthrange(year, month)[1]
                                    if day_num > last_day_of_month:
                                        continue  # Skip invalid days
                                entry_date = date(year=year, month=month, day=day_num)
                            except Exception:
                                continue
                            if entry_date < start_date:
                                continue
                            if entry_date > end_date:
                                continue
                            # Insert entry to Redis
                            _update_entry_in_redis('expense_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        # Move to next month by cadence_interval
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        # Stop if we've passed the end date's year and month
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                else:
                    # Default to the first day of each month
                    year = current_date.year
                    month = current_date.month
                    while True:
                        entry_date = date(year=year, month=month, day=1)
                        if entry_date < start_date:
                            pass
                        elif entry_date > end_date:
                            break
                        else:
                            # Insert entry to Redis
                            _update_entry_in_redis('expense_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        # Move to next month by cadence_interval
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                break  # Exit the outer while loop after handling months

        elif cadence_unit == 'years':
            if yearly_day and yearly_month:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    try:
                        yearly_entry_date = date(year=year, month=int(yearly_month), day=int(yearly_day))
                    except ValueError:
                        year += interval
                        continue
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('expense_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval
            else:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    yearly_entry_date = date(year=year, month=1, day=1)
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('expense_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval

        # Increment current_date
        if delta:
            current_date += delta
        else:
            break

@app.route('/delete-recurring-expense', methods=['POST'])
@login_required
def delete_recurring_expense():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        # First, try to find the recurring record in Redis
        category_id = None
        cached_recurring = _get_recurring_from_redis('recurring_expense', current_user.id)
        if cached_recurring:
            for rec in cached_recurring:
                if rec.get('id') == int(recurring_id):
                    category_id = rec.get('category_id')
                    break
        
        # If not found in Redis (or Redis not available), check MySQL
        if category_id is None:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_id FROM recurring_expense WHERE id = %s AND user_id = %s
                """, (recurring_id, current_user.id))
                category = cursor.fetchone()
                cursor.close()
                if not category:
                    return jsonify({'status': 'error', 'message': 'Recurring expense not found.'}), 404
                category_id = category[0]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Find Auto Adjustments expense category for this user
            cursor.execute("""
                SELECT id FROM expense_categories WHERE user_id = %s AND name = 'Auto Adjustments' LIMIT 1
            """, (current_user.id,))
            auto_adj_row = cursor.fetchone()
            auto_adj_id = auto_adj_row[0] if auto_adj_row else None

            today = date.today()
            cursor.execute("""
                SELECT id, date, amount FROM expense_entries
                WHERE recurring_id = %s AND date < %s
            """, (recurring_id, today))
            old_entries = cursor.fetchall()
            for entry_id, entry_date, amount in old_entries:
                cursor.execute("""
                    SELECT id, amount FROM expense_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE expense_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    cursor.execute("""
                        INSERT INTO expense_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Set recurring_id to NULL when moving
                cursor.execute("UPDATE expense_entries SET recurring_id = NULL WHERE id = %s", (entry_id,))
                cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry_id,))

            # Delete all future entries from Redis
            _delete_entry_in_redis('expense_entries', current_user.id, category_id, today, date(9999, 12, 31))
            
            # Delete the recurring record from Redis (this will mark it for deletion)
            _delete_recurring_in_redis('recurring_expense', current_user.id, recurring_id)
            
            # Delete the category from MySQL immediately
            # Note: Categories aren't separately cached in Redis yet, but they're included in recurring records
            # The _delete_recurring_in_redis call above handles removing the recurring record (with its category_name)
            cursor.execute("DELETE FROM expense_categories WHERE id = %s AND user_id = %s", (category_id, current_user.id))

            conn.commit()
            cursor.close()
        
        return jsonify({'status': 'success', 'message': 'Recurring expense and associated category deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring expense.'}), 500

@app.route('/update-recurring-expense', methods=['POST'])
@login_required
def update_recurring_expense():
    data = request.get_json()
    return update_recurring_expense_inner(data, current_user.id)

def update_recurring_expense_inner(data, user_id):
    try:
        # Get the request data sent via AJAX
        recurring_id = data.get('recurring_id')
        category_name = data.get('category_name')
        amount = data.get('amount')
        cadence_interval = data.get('cadence_interval')
        cadence_unit = data.get('cadence_unit')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        weekdays = data.get('weekdays')  # Extract the weekdays array
        monthly_days = data.get('monthly_days')  # Extract the array of monthly days, if any
        yearly_day = data.get('yearly_day')  # Extract the yearly day, if any
        yearly_month = data.get('yearly_month')  # Extract the yearly month, if any
        no_end_date = data.get('no_end_date', 0)

        # Validate the data
        if not all([recurring_id, category_name, amount, cadence_interval, cadence_unit, start_date, end_date]):
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        # Ensure that the start date is today or later
        today = datetime.today().date()
        if datetime.strptime(start_date, '%Y-%m-%d').date() < today:
            return jsonify({'status': 'error', 'message': 'Start date cannot be earlier than today'}), 400

        # Ensure that the end date is not earlier than the start date
        if datetime.strptime(end_date, '%Y-%m-%d') < datetime.strptime(start_date, '%Y-%m-%d'):
            return jsonify({'status': 'error', 'message': 'End date cannot be earlier than start date'}), 400

        # First, try to find the recurring record in Redis (handles temp negative IDs)
        cached_recurring = _get_recurring_from_redis('recurring_expense', user_id)
        category_id = None
        
        if cached_recurring:
            for rec in cached_recurring:
                if int(rec.get('id')) == int(recurring_id):
                    category_id = rec.get('category_id')
                    break
        
        # If not in Redis, fall back to MySQL
        if not category_id:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_id FROM recurring_expense WHERE id = %s AND user_id = %s
                """, (recurring_id, user_id))
                result = cursor.fetchone()
                cursor.close()
                if not result:
                    return jsonify({'status': 'error', 'message': 'Recurring expense not found'}), 404
                category_id = result[0]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 2: Update the category name in the expense_categories table
            cursor.execute("""
                UPDATE expense_categories
                SET name = %s, is_recurring = 1, no_end_date = %s
                WHERE id = %s
            """, (category_name, no_end_date, category_id))

            # Convert the monthly_days array into a comma-separated string for storage, if it exists
            monthly_days_str = ','.join(map(str, monthly_days)) if monthly_days else None

            # Step 2: Update the recurring expense record in Redis
            recurring_data = {
                'id': recurring_id,
                'user_id': user_id,
                'category_id': category_id,
                'category_name': category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': monthly_days_str,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_expense', user_id, recurring_data)

            # Step 3: Delete old expense entries for today and the future from Redis
            _delete_entry_in_redis('expense_entries', user_id, category_id, today, date(9999, 12, 31))

            # Commit the category changes
            conn.commit()
            cursor.close()
        
        # Step 4: Recreate the expense entries with the updated details (only for today and future)
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, 
                                    start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month)
        
        return jsonify({'status': 'success', 'message': 'Recurring expense updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500

def get_cadence_description(cadence_interval, cadence_unit, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    # Mapping for custom weekday abbreviations
    custom_weekday_abbreviations = {
        'monday': 'M',
        'tuesday': 'T',
        'wednesday': 'W',
        'thursday': 'Th',
        'friday': 'F',
        'saturday': 'S',
        'sunday': 'Su'
    }

    if cadence_unit == 'days':
        return f"Every {cadence_interval} day(s)"
    
    elif cadence_unit == 'weeks':
        if weekdays:
            # If weekdays is a string, split it into a list
            if isinstance(weekdays, str):
                weekdays = weekdays.split(',')

            # Ensure only valid weekdays are included and skip None/empty values
            valid_weekdays = [day.strip() for day in weekdays if day.strip().lower() in custom_weekday_abbreviations]
            
            if valid_weekdays:
                weekdays_str = ', '.join([custom_weekday_abbreviations[day.lower()] for day in valid_weekdays])
                return f"Every {cadence_interval} week(s) on {weekdays_str}"
            else:
                return f"Every {cadence_interval} week(s)"  # No valid weekdays to display
        else:
            return f"Every {cadence_interval} week(s)"
    
    elif cadence_unit == 'months':
        if monthly_days:
            # Clean the monthly_days list by splitting if it's a string
            if isinstance(monthly_days, str):
                monthly_days = [day.strip() for day in monthly_days.split(',') if day.strip()]

            # Remove duplicates and sort the days numerically
            monthly_days_cleaned = sorted(set(monthly_days), key=lambda x: int(x) if x.isdigit() else float('inf'))

            if monthly_days_cleaned:
                monthly_days_str = ', '.join(monthly_days_cleaned)
                return f"Every {cadence_interval} month(s) on days {monthly_days_str}"
            else:
                return f"Every {cadence_interval} month(s)"  # No valid monthly days
        else:
            return f"Every {cadence_interval} month(s)"
    
    elif cadence_unit == 'years':
        if yearly_day and yearly_month:
            # Get the month name based on the integer month
            month_name = calendar.month_name[int(yearly_month)]
            return f"Every {cadence_interval} year(s) on {month_name} {yearly_day}"
        else:
            return f"Every {cadence_interval} year(s)"

    # Fallback for unexpected or custom cadence cases
    return f"Custom Cadence: every {cadence_interval} {cadence_unit}(s)"

###############################################################################################################
##################################### RECURRING CREDIT ACCOUNT EXPENSES #######################################
###############################################################################################################

@app.route('/recurring-ca-expense')
@login_required
def recurring_ca_expense():
    # Try Redis first
    recurring_ca_expense_records = _get_recurring_from_redis('recurring_c_expense', current_user.id)
    
    if recurring_ca_expense_records is None:
        # Fallback to MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Fetch recurring CA expense records, including no_end_date and name from c_expense_categories
            cursor.execute("""
                SELECT rce.id, rce.user_id, rce.category_id, cec.account_id, cec.name as category_name, rce.amount, 
                       rce.cadence_interval, rce.cadence_unit, rce.weekdays, rce.monthly_days, 
                       rce.start_date, rce.end_date, rce.yearly_day, rce.yearly_month,
                       cec.no_end_date
                FROM recurring_c_expense rce
                JOIN c_expense_categories cec ON rce.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE rce.user_id = %s
            """, (current_user.id,))
            recurring_ca_expense_records = cursor.fetchall()
            cursor.close()
            
            # Cache to Redis
            _set_recurring_to_redis('recurring_c_expense', current_user.id, recurring_ca_expense_records)
    
    # Fetch credit accounts and user profile data
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Fetch all credit accounts for the current user
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # Fetch user profile data
        cursor.execute("SELECT profile_picture, first_name, last_name, landing_page, currency_type FROM users WHERE id = %s", (current_user.id,))
        user_data = cursor.fetchone()
        cursor.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard_3m'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    current_date = date.today()
    no_end_date = date(current_date.year + 3, 12, 31)

    for record in recurring_ca_expense_records:
        record['cadence_description'] = get_cadence_description(
            record['cadence_interval'],
            record['cadence_unit'],
            record['weekdays'],
            record['monthly_days'],
            record['yearly_day'],
            record['yearly_month']
        )
        if record['end_date'] == no_end_date.strftime('%Y-%m-%d'):
            record['display_end_date'] = 'No end date'
        else:
            record['display_end_date'] = record['end_date']

    return render_template(
        'recurring_ca_e.html',
        recurring_ca_expense_records=recurring_ca_expense_records,
        credit_accounts=credit_accounts,  # <-- Pass this to the template
        profile_picture=profile_picture,
        first_name=first_name,
        last_name=last_name,
        current_date=current_date,
        landing_page=landing_page,
        currency_type=currency_type
    )

@app.route('/add-recurring-ca-expense', methods=['POST'])
@login_required
def add_recurring_ca_expense():
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    account_id = data.get('account_id')
    category_name = data.get('category_name')
    amount = data.get('amount')
    cadence_interval = data.get('cadence_interval')
    cadence_unit = data.get('cadence_unit')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    weekdays = data.get('weekdays')
    monthly_days = data.get('monthly_days')
    yearly_day = data.get('yearly_day')
    yearly_month = data.get('yearly_month')
    no_end_date = data.get('no_end_date', 0)

    # Validate the data
    if not all([account_id, category_name, amount, cadence_interval, cadence_unit, start_date, end_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 1: Find the current max display order for CA categories for this account
            cursor.execute("""
                SELECT COALESCE(MAX(display_order), 0) FROM c_expense_categories WHERE account_id = %s
            """, (account_id,))
            max_display_order = cursor.fetchone()[0]

            # Step 2: Insert the new category with the next display order
            cursor.execute("""
                INSERT INTO c_expense_categories (account_id, name, display_order, is_recurring, no_end_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (account_id, category_name, max_display_order + 1, 1, no_end_date))

            # Get the ID of the newly inserted CA expense category
            category_id = cursor.lastrowid
            
            # Fetch account_id for the category we just created
            cursor.execute("SELECT account_id FROM c_expense_categories WHERE id = %s", (category_id,))
            account_row = cursor.fetchone()
            account_id_value = account_row[0] if account_row else None

            # Step 3: Create recurring CA expense record in Redis
            recurring_data = {
                'id': None,  # Will be generated by Redis helper
                'user_id': current_user.id,
                'category_id': category_id,
                'account_id': account_id_value,  # Include the account_id
                'category_name': category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': ','.join(map(str, monthly_days)) if monthly_days else None,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_c_expense', current_user.id, recurring_data)
            
            # Use a temporary ID for generating entries (will be replaced on flush)
            recurring_id = recurring_data['id']
            
            conn.commit()
            cursor.close()

        # Generate CA expense entries based on the cadence
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_ca_expense_entries(
                recurring_id, category_id, amount, cadence_interval, cadence_unit,
                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
            )

        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring CA expense added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring CA expense: {str(e)}'}), 500

def generate_ca_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    # Get user_id from the category via credit_accounts
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT ca.user_id 
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE cec.id = %s
        """, (category_id,))
        result = cursor.fetchone()
        cursor.close()
        if not result:
            return
        user_id = result['user_id']

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            # Insert entry to Redis
            _update_entry_in_redis('c_expense_entries', user_id, category_id, current_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    # Insert entry to Redis
                    _update_entry_in_redis('c_expense_entries', user_id, category_id, weekday_date, float(amount), processed=0, entry_id=None)
            delta = timedelta(weeks=int(cadence_interval))

        elif cadence_unit == 'months':
                if monthly_days:
                    monthly_days_cleaned = []
                    for day in monthly_days:
                        if str(day).lower() == 'last day':
                            monthly_days_cleaned.append('Last Day')
                        else:
                            try:
                                monthly_days_cleaned.append(int(day))
                            except Exception:
                                continue
                    year = current_date.year
                    month = current_date.month
                    while True:
                        for day in monthly_days_cleaned:
                            try:
                                if str(day).lower() == 'last day':
                                    day_num = calendar.monthrange(year, month)[1]
                                else:
                                    day_num = int(day)
                                    last_day_of_month = calendar.monthrange(year, month)[1]
                                    if day_num > last_day_of_month:
                                        continue
                                entry_date = date(year=year, month=month, day=day_num)
                            except Exception:
                                continue
                            if entry_date < start_date:
                                continue
                            if entry_date > end_date:
                                continue
                            # Insert entry to Redis
                            _update_entry_in_redis('c_expense_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                else:
                    year = current_date.year
                    month = current_date.month
                    while True:
                        entry_date = date(year=year, month=month, day=1)
                        if entry_date < start_date:
                            pass
                        elif entry_date > end_date:
                            break
                        else:
                            # Insert entry to Redis
                            _update_entry_in_redis('c_expense_entries', user_id, category_id, entry_date, float(amount), processed=0, entry_id=None)
                        month += int(cadence_interval)
                        while month > 12:
                            month -= 12
                            year += 1
                        if (year > end_date.year) or (year == end_date.year and month > end_date.month):
                            break
                break

        elif cadence_unit == 'years':
            if yearly_day and yearly_month:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    try:
                        yearly_entry_date = date(year=year, month=int(yearly_month), day=int(yearly_day))
                    except ValueError:
                        year += interval
                        continue
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('c_expense_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval
            else:
                interval = int(cadence_interval)
                year = start_date.year
                while True:
                    yearly_entry_date = date(year=year, month=1, day=1)
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    # Insert entry to Redis
                    _update_entry_in_redis('c_expense_entries', user_id, category_id, yearly_entry_date, float(amount), processed=0, entry_id=None)
                    year += interval

        # Increment current_date
        if delta:
            current_date += delta
        else:
            break

@app.route('/delete-recurring-ca-expense', methods=['POST'])
@login_required
def delete_recurring_ca_expense():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        # First, try to find the recurring record in Redis
        category_id = None
        cached_recurring = _get_recurring_from_redis('recurring_c_expense', current_user.id)
        if cached_recurring:
            for rec in cached_recurring:
                if rec.get('id') == int(recurring_id):
                    category_id = rec.get('category_id')
                    break
        
        # If not found in Redis (or Redis not available), check MySQL
        if category_id is None:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_id FROM recurring_c_expense WHERE id = %s AND user_id = %s
                """, (recurring_id, current_user.id))
                category = cursor.fetchone()
                cursor.close()
                if not category:
                    return jsonify({'status': 'error', 'message': 'Recurring CA expense not found.'}), 404
                category_id = category[0]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Find account_id for this CA category
            cursor.execute("SELECT account_id FROM c_expense_categories WHERE id = %s", (category_id,))
            account_row = cursor.fetchone()
            account_id = account_row[0] if account_row else None

            # Find Auto Adjustments CA category for this account
            cursor.execute("""
                SELECT id FROM c_expense_categories WHERE account_id = %s AND name = 'Auto Adjustments' LIMIT 1
            """, (account_id,))
            auto_adj_row = cursor.fetchone()
            auto_adj_id = auto_adj_row[0] if auto_adj_row else None

            today = date.today()
            cursor.execute("""
                SELECT id, date, amount FROM c_expense_entries
                WHERE recurring_id = %s AND date < %s
            """, (recurring_id, today))
            old_entries = cursor.fetchall()
            for entry_id, entry_date, amount in old_entries:
                cursor.execute("""
                    SELECT id, amount FROM c_expense_entries
                    WHERE category_id = %s AND date = %s
                """, (auto_adj_id, entry_date))
                auto_entry = cursor.fetchone()
                if auto_entry:
                    new_amount = auto_entry[1] + amount
                    cursor.execute("""
                        UPDATE c_expense_entries SET amount = %s, processed = 1 WHERE id = %s
                    """, (new_amount, auto_entry[0]))
                else:
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_id, entry_date, amount))
                # Set recurring_id to NULL when moving
                cursor.execute("UPDATE c_expense_entries SET recurring_id = NULL WHERE id = %s", (entry_id,))
                cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry_id,))

            # Delete all future entries from Redis
            _delete_entry_in_redis('c_expense_entries', current_user.id, category_id, today, date(9999, 12, 31))
            
            # Delete the recurring record from Redis (this will mark it for deletion)
            _delete_recurring_in_redis('recurring_c_expense', current_user.id, recurring_id)
            
            # Delete the category from MySQL immediately
            # Note: Categories aren't separately cached in Redis yet, but they're included in recurring records
            # The _delete_recurring_in_redis call above handles removing the recurring record (with its category_name and account_id)
            cursor.execute("DELETE FROM c_expense_categories WHERE id = %s", (category_id,))

            conn.commit()
            cursor.close()
        
        return jsonify({'status': 'success', 'message': 'Recurring CA expense deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring CA expense.'}), 500


@app.route('/update-recurring-ca-expense', methods=['POST'])
@login_required
def update_recurring_ca_expense():
    data = request.get_json()
    return update_recurring_ca_expense_inner(data, current_user.id)

def update_recurring_ca_expense_inner(data, user_id):
    try:
        recurring_id = data.get('recurring_id')
        amount = data.get('amount')
        cadence_interval = data.get('cadence_interval')
        cadence_unit = data.get('cadence_unit')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        weekdays = data.get('weekdays')
        monthly_days = data.get('monthly_days')
        yearly_day = data.get('yearly_day')
        yearly_month = data.get('yearly_month')
        no_end_date = data.get('no_end_date', 0)

        if not all([recurring_id, amount, cadence_interval, cadence_unit, start_date, end_date]):
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        today = datetime.today().date()
        if datetime.strptime(start_date, '%Y-%m-%d').date() < today:
            return jsonify({'status': 'error', 'message': 'Start date cannot be earlier than today'}), 400
        if datetime.strptime(end_date, '%Y-%m-%d') < datetime.strptime(start_date, '%Y-%m-%d'):
            return jsonify({'status': 'error', 'message': 'End date cannot be earlier than start date'}), 400

        # First, try to find the recurring record in Redis (handles temp negative IDs)
        cached_recurring = _get_recurring_from_redis('recurring_c_expense', user_id)
        category_id = None
        existing_category_name = None
        account_id_value = None
        
        if cached_recurring:
            for rec in cached_recurring:
                if int(rec.get('id')) == int(recurring_id):
                    category_id = rec.get('category_id')
                    existing_category_name = rec.get('category_name')
                    account_id_value = rec.get('account_id')
                    break
        
        # If not in Redis, fall back to MySQL
        if not category_id:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT rce.category_id, cec.name as category_name, cec.account_id 
                    FROM recurring_c_expense rce
                    JOIN c_expense_categories cec ON rce.category_id = cec.id
                    WHERE rce.id = %s AND rce.user_id = %s
                """, (recurring_id, user_id))
                result = cursor.fetchone()
                cursor.close()
                if not result:
                    return jsonify({'status': 'error', 'message': 'Recurring CA expense not found'}), 404
                category_id = result[0]
                existing_category_name = result[1]
                account_id_value = result[2]
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()

            # Step 2: Update the recurring CA expense record in Redis
            recurring_data = {
                'id': recurring_id,
                'user_id': user_id,
                'category_id': category_id,
                'account_id': account_id_value,  # Include the account_id
                'category_name': existing_category_name,  # Include the category name
                'amount': float(amount),
                'cadence_interval': cadence_interval,
                'cadence_unit': cadence_unit,
                'weekdays': ','.join(weekdays) if weekdays else None,
                'monthly_days': ','.join(map(str, monthly_days)) if monthly_days else None,
                'yearly_day': yearly_day if yearly_day else None,
                'yearly_month': yearly_month if yearly_month else None,
                'start_date': start_date,
                'end_date': end_date,
                'no_end_date': no_end_date
            }
            
            _update_recurring_in_redis('recurring_c_expense', user_id, recurring_data)

            # Step 3: Delete old CA expense entries for today and the future from Redis
            _delete_entry_in_redis('c_expense_entries', user_id, category_id, today, date(9999, 12, 31))

            conn.commit()
            cursor.close()

        # Step 4: Recreate the CA expense entries with the updated details (only for today and future)
        # Now writes to Redis, so works with both temp (negative) and real (positive) IDs
        if recurring_id:
            generate_ca_expense_entries(
                recurring_id, category_id, amount, cadence_interval, cadence_unit,
                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
            )

        return jsonify({'status': 'success', 'message': 'Recurring CA expense updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500

####################################################################################
##################################### FOOTER #######################################
####################################################################################

@app.route('/footer_add_entry', methods=['POST'])
@login_required
def footer_add_entry():
    data = request.get_json()
    entry_type = data.get('entryType')
    category_id = data.get('category')
    amount = data.get('amount')
    entry_date = data.get('date')
    
    app.logger.info(f"[FOOTER ADD ENTRY] User {current_user.id}: type={entry_type}, category={category_id}, amount={amount}, date={entry_date}")

    if not all([entry_type, category_id, amount, entry_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    # Establish a database connection
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        ca_triggered = False

        # Determine the appropriate table
        if entry_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
        elif entry_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
        elif entry_type.startswith('ca_') or entry_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
        else:
            cursor.close()
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

        # Check that the category exists and belongs to the user
        if entry_type.startswith('ca_') or entry_type == 'ca':
            cursor.execute("""
                SELECT cec.id
                FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE cec.id = %s AND ca.user_id = %s
            """, (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        else:
            cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        
        # Make a copy of cat_row data before closing cursor
        cat_data = dict(cat_row) if cat_row else {}
        app.logger.info(f"[FOOTER ADD ENTRY] Category data for category {category_id}: name='{cat_data.get('name')}', is_credit_account={cat_data.get('is_credit_account', 'MISSING')}")
        cursor.close()

    # Add to Redis only - flush worker will persist to MySQL
    # Get existing entries to check if we need to add or update
    existing_data = _get_entries_from_redis(table_name, current_user.id)
    
    # If not in Redis, load from MySQL first
    if existing_data is None:
        existing_data = []
        with get_db_pool().get_connection() as conn:
            cursor2 = conn.cursor(pymysql.cursors.DictCursor)
            if entry_type == 'income':
                cursor2.execute("""
                    SELECT ie.* FROM income_entries ie
                    JOIN income_categories ic ON ie.category_id = ic.id
                    WHERE ic.user_id = %s
                """, (current_user.id,))
            elif entry_type == 'expense':
                cursor2.execute("""
                    SELECT ee.* FROM expense_entries ee
                    JOIN expense_categories ec ON ee.category_id = ec.id
                    WHERE ec.user_id = %s
                """, (current_user.id,))
            elif entry_type.startswith('ca_') or entry_type == 'ca':
                cursor2.execute("""
                    SELECT cee.* FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (current_user.id,))
            existing_data = list(cursor2.fetchall())
            cursor2.close()
        # Filter out entries marked for deletion
        existing_data = _filter_pending_deletions(table_name, current_user.id, existing_data)
        app.logger.info(f"[REDIS][{table_name}] Loaded {len(existing_data)} entries from MySQL (after filtering pending deletions)")
    
    existing_entry = None
    
    if existing_data:
        for entry in existing_data:
            if str(entry.get('category_id')) == str(category_id) and str(entry.get('date')) == str(entry_date):
                existing_entry = entry
                break
    
    if existing_entry:
        new_amount = Decimal(existing_entry.get('amount', 0)) + Decimal(amount)
        _update_entry_in_redis(table_name, current_user.id, category_id, entry_date, float(new_amount))
    else:
        _update_entry_in_redis(table_name, current_user.id, category_id, entry_date, float(amount))

    # Check if this is a savings category - update savings if so
    is_savings_category = False
    if entry_type in ['income', 'expense'] and cat_data.get('name') == 'Savings':
        is_savings_category = True

    # If this is an expense category and is_credit_account=1, add payment entry and trigger CA balance update
    app.logger.info(f"[FOOTER CA PAYMENT DEBUG] entry_type={entry_type}, cat_data={cat_data}")
    if entry_type == 'expense' and cat_data.get('is_credit_account', 0) == 1:
        ca_triggered = True
        app.logger.info(f"[FOOTER CA PAYMENT] Detected payment category for user {current_user.id}, category {category_id}")
        # Find the credit account by matching category name
        category_name = cat_data.get('name', '')
        app.logger.info(f"[FOOTER CA PAYMENT] Category name: '{category_name}'")
        if category_name.endswith(' payment'):
            account_name = category_name[:-8]  # Remove ' payment' suffix
            app.logger.info(f"[FOOTER CA PAYMENT] Looking for credit account with name: '{account_name}'")
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s
                """, (current_user.id, account_name))
                account_row = cursor.fetchone()
                app.logger.info(f"[FOOTER CA PAYMENT] Credit account query result: {account_row}")
                if account_row:
                    account_id = account_row['id']
                    app.logger.info(f"[FOOTER CA PAYMENT] Found credit account_id={account_id}, adding payment entry for date={entry_date}, amount={amount}")
                    # Add or update payment entry in Redis
                    payment_entries = _get_entries_from_redis('c_payment_entries', current_user.id)
                    app.logger.info(f"[FOOTER CA PAYMENT] Current payment_entries from Redis: {len(payment_entries) if payment_entries else 'None'}")
                    if payment_entries is None:
                        # Load from MySQL first
                        cursor.execute("""
                            SELECT cpe.* FROM c_payment_entries cpe
                            JOIN credit_accounts ca ON cpe.account_id = ca.id
                            WHERE ca.user_id = %s
                        """, (current_user.id,))
                        payment_entries = list(cursor.fetchall())
                        payment_entries = _filter_pending_deletions('c_payment_entries', current_user.id, payment_entries)
                        app.logger.info(f"[FOOTER CA PAYMENT] Loaded {len(payment_entries)} payment_entries from MySQL")
                    
                    # Check if payment entry already exists for this date/account
                    existing_payment = None
                    for pe in payment_entries:
                        if str(pe.get('account_id')) == str(account_id) and str(pe.get('date')) == str(entry_date):
                            existing_payment = pe
                            break
                    
                    if existing_payment:
                        new_payment_amount = Decimal(existing_payment.get('amount', 0)) + Decimal(amount)
                        app.logger.info(f"[FOOTER CA PAYMENT] Updating existing payment: {existing_payment.get('amount')} + {amount} = {new_payment_amount}")
                        _update_payment_entry_in_redis(current_user.id, account_id, entry_date, float(new_payment_amount))
                    else:
                        app.logger.info(f"[FOOTER CA PAYMENT] Creating new payment entry: account_id={account_id}, date={entry_date}, amount={amount}")
                        _update_payment_entry_in_redis(current_user.id, account_id, entry_date, float(amount))
                    app.logger.info(f"[FOOTER CA PAYMENT] Payment entry operation completed")
                else:
                    app.logger.warning(f"[FOOTER CA PAYMENT] No credit account found with name '{account_name}' for user {current_user.id}")
                cursor.close()
        else:
            app.logger.warning(f"[FOOTER CA PAYMENT] Category name '{category_name}' does not end with ' payment'")
    else:
        app.logger.info(f"[FOOTER CA PAYMENT] Not a payment category: entry_type={entry_type}, is_credit_account={cat_data.get('is_credit_account', 0)}")

    # For CA, update balances
    if entry_type.startswith('ca_') or entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()
    
    # Update totals and savings if this is a savings category
    if is_savings_category:
        save_totals_remainders_d()

    return jsonify({"status": "success"})

####################################################################################
##################################### BUDS #########################################
####################################################################################

@app.route('/buds')
@login_required
def buds():
    bud_id = request.args.get('bud_id', type=int)

    # Try Redis first for buds
    buds_list = _get_buds_from_redis(current_user.id)
    if buds_list is None:
        # Fallback to MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT b.id, b.name, b.expense_category_id, b.created_at, b.active, ec.name AS category_name
                FROM buds b
                LEFT JOIN expense_categories ec ON b.expense_category_id = ec.id
                WHERE b.user_id = %s
                ORDER BY b.created_at DESC
            """, (current_user.id,))
            buds_list = cursor.fetchall()
            cursor.close()
    else:
        # Ensure buds from Redis have category_name field for template compatibility
        for bud in buds_list:
            if 'category_name' not in bud:
                bud['category_name'] = None

    # Determine selected bud
    selected_bud = None
    if buds_list:
        if bud_id:
            selected_bud = next((bud for bud in buds_list if int(bud['id']) == bud_id), buds_list[0])
        else:
            selected_bud = buds_list[0]

    # Try Redis first for bud_items
    all_bud_items = _get_bud_items_from_redis(current_user.id)
    if all_bud_items is None:
        # Fallback to MySQL - get ALL bud_items for this user's buds
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT bi.* FROM bud_items bi
                INNER JOIN buds b ON bi.bud_id = b.id
                WHERE b.user_id = %s
                ORDER BY bi.id DESC
            """, (current_user.id,))
            all_bud_items = cursor.fetchall()
            cursor.close()

    # Ensure all_bud_items is at least an empty list
    if all_bud_items is None:
        all_bud_items = []

    # Group all bud_items by bud_id for all buds
    bud_items_by_bud = {}
    if buds_list:
        # Create a set of valid bud IDs for quick lookup
        valid_bud_ids = {int(bud['id']) for bud in buds_list}
        
        for bud in buds_list:
            bud_items = [item for item in all_bud_items if int(item['bud_id']) == int(bud['id'])]
            bud_items_by_bud[bud['id']] = bud_items
        
        # Check for orphaned bud_items (items with bud_id not in buds_list)
        orphaned_items = [item for item in all_bud_items if int(item['bud_id']) not in valid_bud_ids]
        if orphaned_items:
            app.logger.warning(f"[BUDS] Found {len(orphaned_items)} orphaned bud_items for user {current_user.id}: {[{'id': item['id'], 'bud_id': item['bud_id'], 'name': item['name']} for item in orphaned_items]}")
            # Optionally: add orphaned items to a special "Orphaned Items" bud for visibility
            # For now, we just log them

    # Fetch other data from MySQL (categories, accounts, user settings)
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Fetch all expense categories for the user
        cursor.execute("""
            SELECT * FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
        expense_categories = cursor.fetchall()

        # Fetch all credit accounts for the user
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # Fetch landing_page and currency_type from users table
        cursor.execute("""
            SELECT landing_page, currency_type FROM users WHERE id = %s
        """, (current_user.id,))
        user_row = cursor.fetchone()
        landing_page = user_row['landing_page'] if user_row and 'landing_page' in user_row else 'dashboard_3m'
        currency_type = user_row['currency_type'] if user_row and 'currency_type' in user_row else 'USD'

        cursor.close()

    return render_template(
        'buds.html',
        buds=buds_list,
        selected_bud=selected_bud,
        bud_items_by_bud=bud_items_by_bud,
        expense_categories=expense_categories,
        credit_accounts=credit_accounts,
        landing_page=landing_page,
        currency_type=currency_type
    )

@app.route('/add-bud', methods=['POST'])
@login_required
def add_bud():
    data = request.get_json()
    bud_name = data.get('name', '').strip()
    if not bud_name:
        return jsonify({'status': 'error', 'message': 'Bud name required.'}), 400

    # Insert to MySQL first to get real ID
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            INSERT INTO buds (user_id, name, expense_category_id, active, created_at)
            VALUES (%s, %s, NULL, 0, NOW())
        """, (current_user.id, bud_name))
        new_bud_id = cursor.lastrowid
        conn.commit()
        cursor.close()

    # Create bud data for Redis
    bud_data = {
        'id': new_bud_id,
        'user_id': current_user.id,
        'name': bud_name,
        'expense_category_id': None,
        'active': 0,
        'created_at': datetime.now().isoformat()
    }

    # Add to Redis
    _update_bud_in_redis(current_user.id, bud_data)

    return jsonify({'status': 'success', 'bud_id': new_bud_id})

@app.route('/add-bud-item', methods=['POST'])
@login_required
def add_bud_item():
    data = request.get_json()
    name = data.get('name', '').strip()
    value = data.get('value', None)
    date_val = data.get('date', None)
    bud_id = int(data.get('bud_id', 0))
    active = int(data.get('active', 0))
    account = data.get('account', '').strip()

    if not name or not value or not date_val or not bud_id:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    # Validate that the bud exists before creating the item
    buds = _get_buds_from_redis(current_user.id)
    if buds is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT id FROM buds WHERE user_id = %s AND id = %s", (current_user.id, bud_id))
            bud_exists = cursor.fetchone()
            cursor.close()
            if not bud_exists:
                return jsonify({'status': 'error', 'message': 'Parent bud not found'}), 404
    else:
        bud = next((b for b in buds if int(b['id']) == int(bud_id)), None)
        if not bud:
            return jsonify({'status': 'error', 'message': 'Parent bud not found'}), 404

    # Insert to MySQL first to get real ID
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            INSERT INTO bud_items (bud_id, account, name, value, date, description)
            VALUES (%s, %s, %s, %s, %s, NULL)
        """, (bud_id, account, name, float(value), date_val))
        new_item_id = cursor.lastrowid
        conn.commit()
        cursor.close()

    # Create bud_item data for Redis
    bud_item_data = {
        'id': new_item_id,
        'bud_id': bud_id,
        'account': account,
        'name': name,
        'value': float(value),
        'date': date_val,
        'description': None
    }

    # Add to Redis
    _update_bud_item_in_redis(current_user.id, bud_item_data)

    # Only add expense entry if active flag is set
    if active:
        # Need to use MySQL for expense entry creation (existing pattern)
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            add_expense_entry_for_bud_item(cursor, bud_id, new_item_id, value, date_val)
            conn.commit()
            cursor.close()

    if active:
        save_ca_daily_balance()
    return jsonify({'status': 'success', 'item_id': new_item_id})

def add_expense_entry_for_bud_item(cursor, bud_id, bud_item_id, value, date_val):
    # Ensure date_val is a date object for comparison
    if isinstance(date_val, str):
        date_val_obj = datetime.strptime(date_val, '%Y-%m-%d').date()
    else:
        date_val_obj = date_val
    
    # Get bud_item account - try Redis first
    bud_items = _get_bud_items_from_redis(current_user.id)
    if bud_items:
        bud_item = next((item for item in bud_items if int(item['id']) == int(bud_item_id)), None)
        account = bud_item['account'] if bud_item else "Blankee"
    else:
        cursor.execute("SELECT account FROM bud_items WHERE id = %s", (bud_item_id,))
        bud_item_row = cursor.fetchone()
        account = bud_item_row['account'] if bud_item_row else "Blankee"

    # Get bud info - try Redis first
    buds = _get_buds_from_redis(current_user.id)
    if buds:
        bud = next((b for b in buds if int(b['id']) == int(bud_id)), None)
        bud_name = bud['name'] if bud else "Bud"
        expense_category_id = bud.get('expense_category_id') if bud else None
    else:
        cursor.execute("SELECT name, expense_category_id FROM buds WHERE id = %s", (bud_id,))
        bud_row = cursor.fetchone()
        bud_name = bud_row['name'] if bud_row else "Bud"
        expense_category_id = bud_row['expense_category_id'] if bud_row else None

    if account.lower() == "blankee":
        if expense_category_id:
            # Check if entry exists for this date and category, add to it if so
            expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
            existing_entry = None
            if expense_entries:
                for e in expense_entries:
                    e_date = e.get('date')
                    if isinstance(e_date, str):
                        e_date = datetime.strptime(e_date, '%Y-%m-%d').date()
                    if int(e.get('category_id', 0) or 0) == int(expense_category_id) and e_date == date_val_obj:
                        existing_entry = e
                        break
            
            if existing_entry:
                # Add to existing entry - preserve original bud_item_id
                new_amount = float(existing_entry.get('amount', 0)) + float(value)
                original_bud_item_id = existing_entry.get('bud_item_id')
                _update_entry_in_redis('expense_entries', current_user.id, expense_category_id, date_val, new_amount, bud_item_id=original_bud_item_id)
            else:
                # Create new entry
                _update_entry_in_redis('expense_entries', current_user.id, expense_category_id, date_val, float(value), bud_item_id=bud_item_id)
    else:
        cursor.execute(
            "SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s",
            (current_user.id, account)
        )
        ca_row = cursor.fetchone()
        if ca_row:
            account_id = ca_row['id']
            # Use the bud's name for the category
            cursor.execute(
                "SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s",
                (account_id, bud_name)
            )
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.execute(
                    "SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM c_expense_categories WHERE account_id = %s",
                    (account_id,)
                )
                max_order = cursor.fetchone()
                display_order = max_order['max_display_order'] + 1 if max_order and max_order['max_display_order'] is not None else 1
                cursor.execute(
                    "INSERT INTO c_expense_categories (account_id, name, display_order, is_bud) VALUES (%s, %s, %s, 1)",
                    (account_id, bud_name, display_order)
                )
                category_id = cursor.lastrowid
            else:
                category_id = cat_row['id']
            # Check if entry exists for this date and category, add to it if so
            c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
            existing_entry = None
            if c_expense_entries:
                for e in c_expense_entries:
                    e_date = e.get('date')
                    if isinstance(e_date, str):
                        e_date = datetime.strptime(e_date, '%Y-%m-%d').date()
                    if int(e.get('category_id', 0) or 0) == int(category_id) and e_date == date_val_obj:
                        existing_entry = e
                        break
            
            if existing_entry:
                # Add to existing entry - preserve original bud_item_id
                new_amount = float(existing_entry.get('amount', 0)) + float(value)
                original_bud_item_id = existing_entry.get('bud_item_id')
                _update_entry_in_redis('c_expense_entries', current_user.id, category_id, date_val, new_amount, bud_item_id=original_bud_item_id)
            else:
                # Create new entry
                _update_entry_in_redis('c_expense_entries', current_user.id, category_id, date_val, float(value), bud_item_id=bud_item_id)

@app.route('/update-bud-item', methods=['POST'])
@login_required
def update_bud_item():
    data = request.get_json()
    item_id = int(data.get('id', 0))
    field = data.get('field')
    value = data.get('value', '').strip()
    allowed_fields = {'name', 'value', 'date', 'account'}
    if not item_id or field not in allowed_fields:
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400

    # Get the bud_item from Redis first
    bud_items = _get_bud_items_from_redis(current_user.id)
    if bud_items is None:
        # Fallback to MySQL
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM bud_items WHERE id = %s", (item_id,))
            bud_item = cursor.fetchone()
            cursor.close()
    else:
        bud_item = next((item for item in bud_items if int(item['id']) == int(item_id)), None)

    if not bud_item:
        return jsonify({'status': 'error', 'message': 'Bud item not found'}), 404

    # Update the field
    bud_item[field] = value if field != 'value' else float(value)

    # Update in Redis
    _update_bud_item_in_redis(current_user.id, bud_item)

    # Get bud active status
    bud_id = bud_item['bud_id']
    buds = _get_buds_from_redis(current_user.id)
    if buds:
        bud = next((b for b in buds if int(b['id']) == int(bud_id)), None)
        bud_active = bud['active'] if bud else 0
    else:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT active FROM buds WHERE id = %s", (bud_id,))
            bud_row = cursor.fetchone()
            bud_active = bud_row['active'] if bud_row else 0
            cursor.close()

    # Only update expense entry if bud is active
    if bud_active == 1:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            update_expense_entry_for_bud_item(cursor, item_id, field, value)
            conn.commit()
            cursor.close()

    # Only run save_ca_daily_balance if account is not Blankee and bud is active
    account = bud_item.get('account', '').lower()
    if account != "blankee" and bud_active == 1:
        save_ca_daily_balance()
    return jsonify({'status': 'success'})

def update_expense_entry_for_bud_item(cursor, item_id, field, value):
    """
    Updates the linked expense entry for a bud_item.
    If the account field changes, moves the entry between expense_entries and c_expense_entries.
    Otherwise, updates the corresponding entry's field.
    """
    # Get current bud_item info
    cursor.execute("""SELECT bud_id, date, value, account FROM bud_items WHERE id = %s""", (item_id,))
    bud_item = cursor.fetchone()
    if not bud_item:
        return

    bud_id = bud_item['bud_id']
    item_date = bud_item['date']
    item_value = bud_item['value']
    current_account = bud_item['account']

    # Get bud name for category use
    cursor.execute("SELECT name FROM buds WHERE id = %s", (bud_id,))
    bud_row = cursor.fetchone()
    bud_name = bud_row['name'] if bud_row and 'name' in bud_row else "Bud"

    # If editing the account field, value is the new account
    if field == 'account':
        new_account = value
        # Always delete from both tables to avoid duplicates
        cursor.execute("DELETE FROM expense_entries WHERE bud_item_id = %s", (item_id,))
        cursor.execute("DELETE FROM c_expense_entries WHERE bud_item_id = %s", (item_id,))
        # Add entry to the new table
        if new_account.lower() == "blankee":
            cursor.execute("SELECT expense_category_id FROM buds WHERE id = %s", (bud_id,))
            bud_row = cursor.fetchone()
            if bud_row and bud_row['expense_category_id']:
                expense_category_id = bud_row['expense_category_id']
                cursor.execute(
                    "INSERT INTO expense_entries (category_id, date, amount, bud_item_id) VALUES (%s, %s, %s, %s)",
                    (expense_category_id, item_date, item_value, item_id)
                )
        else:
            cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, new_account))
            ca_row = cursor.fetchone()
            if ca_row:
                account_id = ca_row['id']
                # Use the bud's name for the category
                cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (account_id, bud_name))
                cat_row = cursor.fetchone()
                if not cat_row:
                    cursor.execute("SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM c_expense_categories WHERE account_id = %s", (account_id,))
                    max_order = cursor.fetchone()
                    display_order = max_order['max_display_order'] + 1 if max_order and max_order['max_display_order'] is not None else 1
                    cursor.execute("INSERT INTO c_expense_categories (account_id, name, display_order, is_bud) VALUES (%s, %s, %s, 1)", (account_id, bud_name, display_order))
                    category_id = cursor.lastrowid
                else:
                    category_id = cat_row['id']
                cursor.execute(
                    "INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id) VALUES (%s, %s, %s, %s)",
                    (category_id, item_date, item_value, item_id)
                )
    else:
        # Field is value or date, update the corresponding entry in the correct table
        if current_account.lower() == "blankee":
            if field == 'value':
                cursor.execute("UPDATE expense_entries SET amount = %s WHERE bud_item_id = %s", (item_value, item_id))
            elif field == 'date':
                cursor.execute("UPDATE expense_entries SET date = %s WHERE bud_item_id = %s", (item_date, item_id))
        else:
            cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, current_account))
            ca_row = cursor.fetchone()
            if not ca_row:
                return
            account_id = ca_row['id']
            # Use the bud's name for the category
            cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (account_id, bud_name))
            cat_row = cursor.fetchone()
            if not cat_row:
                cursor.execute("SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM c_expense_categories WHERE account_id = %s", (account_id,))
                max_order = cursor.fetchone()
                display_order = max_order['max_display_order'] + 1 if max_order and max_order['max_display_order'] is not None else 1
                cursor.execute("INSERT INTO c_expense_categories (account_id, name, display_order, is_bud) VALUES (%s, %s, %s, 1)", (account_id, bud_name, display_order))
                category_id = cursor.lastrowid
            else:
                category_id = cat_row['id']
            if field == 'value':
                cursor.execute("UPDATE c_expense_entries SET amount = %s WHERE bud_item_id = %s", (item_value, item_id))
            elif field == 'date':
                cursor.execute("UPDATE c_expense_entries SET date = %s WHERE bud_item_id = %s", (item_date, item_id))

@app.route('/delete-bud-item', methods=['POST'])
@login_required
def delete_bud_item():
    data = request.get_json()
    item_id = int(data.get('id', 0))
    if not item_id:
        return jsonify({'status': 'error', 'message': 'Missing item id'}), 400

    today = date.today()

    # Get bud_item info from Redis first
    all_bud_items = _get_bud_items_from_redis(current_user.id)
    if all_bud_items is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM bud_items WHERE bud_id IN (SELECT id FROM buds WHERE user_id = %s)", (current_user.id,))
            all_bud_items = cursor.fetchall()
            cursor.close()
    
    bud_item = next((item for item in all_bud_items if int(item['id']) == int(item_id)), None)
    if not bud_item:
        return jsonify({'status': 'error', 'message': 'Item not found'}), 404

    bud_id = bud_item['bud_id']
    account = bud_item.get('account', 'Blankee')

    # Get bud active status from Redis
    buds = _get_buds_from_redis(current_user.id)
    if buds is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM buds WHERE user_id = %s", (current_user.id,))
            buds = cursor.fetchall()
            cursor.close()
    
    bud = next((b for b in buds if int(b['id']) == int(bud_id)), None)
    bud_active = bud['active'] if bud else 0

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        if account.lower() == "blankee":
            # Get expense_category_id for this bud
            expense_category_id = bud.get('expense_category_id') if bud else None

            # Find Auto Adjustments category for this user
            cursor.execute("SELECT id FROM expense_categories WHERE user_id = %s AND name = %s", (current_user.id, "Auto Adjustments"))
            auto_adj = cursor.fetchone()
            if not auto_adj:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Auto Adjustments category not found'}), 404
            auto_adj_id = auto_adj['id']

            # Get all expense_entries for this bud_item from Redis
            expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
            if expense_entries:
                # Collect Auto Adjustments entries to create
                auto_adj_entries_to_create = []
                
                for e in expense_entries:
                    if int(e.get('bud_item_id', 0)) == int(item_id):
                        entry_date = e.get('date')
                        if isinstance(entry_date, str):
                            entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                        
                        if entry_date < today:
                            # Collect for Auto Adjustments
                            auto_adj_entries_to_create.append({
                                'date': entry_date,
                                'amount': float(e.get('amount', 0))
                            })
                
                # Remove all entries with this bud_item_id
                entries_to_keep = [e for e in expense_entries if int(e.get('bud_item_id', 0)) != int(item_id)]
                
                # Save filtered entries first
                _set_entries_to_redis('expense_entries', current_user.id, entries_to_keep)
                
                # Now create Auto Adjustments entries
                for auto_adj_entry in auto_adj_entries_to_create:
                    _update_entry_in_redis('expense_entries', current_user.id, 
                                         auto_adj_id, auto_adj_entry['date'], 
                                         auto_adj_entry['amount'], 
                                         processed=1, bud_item_id=None)

        else:
            # CA: Find the credit account
            cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, account))
            ca_row = cursor.fetchone()
            if not ca_row:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Credit account not found'}), 404
            account_id = ca_row['id']

            # Find Auto Adjustments CA category for this account
            cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (account_id, "Auto Adjustments"))
            auto_adj = cursor.fetchone()
            if not auto_adj:
                cursor.close()
                return jsonify({'status': 'error', 'message': 'Auto Adjustments CA category not found'}), 404
            auto_adj_id = auto_adj['id']

            # Get all c_expense_entries for this bud_item from Redis
            c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
            if c_expense_entries:
                # Collect Auto Adjustments entries to create
                ca_auto_adj_entries_to_create = []
                
                for e in c_expense_entries:
                    if int(e.get('bud_item_id', 0)) == int(item_id):
                        entry_date = e.get('date')
                        if isinstance(entry_date, str):
                            entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                        
                        if entry_date < today:
                            # Collect for Auto Adjustments
                            ca_auto_adj_entries_to_create.append({
                                'date': entry_date,
                                'amount': float(e.get('amount', 0))
                            })
                
                # Remove all entries with this bud_item_id
                ca_entries_to_keep = [e for e in c_expense_entries if int(e.get('bud_item_id', 0)) != int(item_id)]
                
                # Save filtered entries first
                _set_entries_to_redis('c_expense_entries', current_user.id, ca_entries_to_keep)
                
                # Now create Auto Adjustments entries
                for ca_auto_adj_entry in ca_auto_adj_entries_to_create:
                    _update_entry_in_redis('c_expense_entries', current_user.id, 
                                         auto_adj_id, ca_auto_adj_entry['date'], 
                                         ca_auto_adj_entry['amount'], 
                                         processed=1, bud_item_id=None)

        conn.commit()
        cursor.close()
    
    # Delete the bud_item from Redis
    _delete_bud_item_in_redis(current_user.id, item_id)
        
    # Only run save_ca_daily_balance if account is not Blankee and bud is active
    if account.lower() != "blankee" and bud_active == 1:
        save_ca_daily_balance()
    return jsonify({'status': 'success'})

@app.route('/delete-bud', methods=['POST'])
@login_required
def delete_bud():
    data = request.get_json()
    bud_id = int(data.get('bud_id', 0))
    if not bud_id:
        return jsonify({'status': 'error', 'message': 'Missing bud_id'}), 400

    today = date.today()

    # Get bud info from Redis first
    buds = _get_buds_from_redis(current_user.id)
    if buds is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM buds WHERE user_id = %s", (current_user.id,))
            buds = cursor.fetchall()
            cursor.close()
    
    bud_row = next((b for b in buds if int(b['id']) == int(bud_id)), None)
    if not bud_row:
        return jsonify({'status': 'error', 'message': 'Bud not found'}), 404
    
    bud_name = bud_row['name']
    bud_expense_category_id = bud_row.get('expense_category_id')

    # Get all bud_items for this bud from Redis
    all_bud_items = _get_bud_items_from_redis(current_user.id)
    if all_bud_items is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM bud_items WHERE bud_id IN (SELECT id FROM buds WHERE user_id = %s)", (current_user.id,))
            all_bud_items = cursor.fetchall()
            cursor.close()
    
    bud_items = [item for item in all_bud_items if int(item['bud_id']) == int(bud_id)]

    # Track CA categories to delete
    ca_category_ids_to_delete = set()
    bud_category_ids_to_delete = set()

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        for bud_item in bud_items:
            item_id = bud_item['id']
            account = bud_item.get('account', '').lower() if bud_item.get('account') else "blankee"

            if account == "blankee":
                # Find Auto Adjustments expense category for this user
                cursor.execute("""
                    SELECT id FROM expense_categories
                    WHERE user_id = %s AND name = 'Auto Adjustments' LIMIT 1
                """, (current_user.id,))
                auto_adj = cursor.fetchone()
                if not auto_adj:
                    continue
                auto_adj_id = auto_adj['id']

                # Get all expense_entries for this bud_item from Redis
                expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
                if expense_entries:
                    # Collect Auto Adjustments entries to create
                    auto_adj_entries_to_create = []
                    
                    for e in expense_entries:
                        if int(e.get('bud_item_id') or 0) == int(item_id):
                            entry_date = e.get('date')
                            if isinstance(entry_date, str):
                                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                            
                            if entry_date < today:
                                # Collect for Auto Adjustments
                                auto_adj_entries_to_create.append({
                                    'date': entry_date,
                                    'amount': float(e.get('amount', 0))
                                })
                    
                    # Remove all entries with this bud_item_id
                    entries_to_keep = [e for e in expense_entries if int(e.get('bud_item_id') or 0) != int(item_id)]
                    
                    # Save filtered entries first
                    _set_entries_to_redis('expense_entries', current_user.id, entries_to_keep)
                    
                    # Now create Auto Adjustments entries
                    for auto_adj_entry in auto_adj_entries_to_create:
                        _update_entry_in_redis('expense_entries', current_user.id, 
                                             auto_adj_id, auto_adj_entry['date'], 
                                             auto_adj_entry['amount'], 
                                             processed=1, bud_item_id=None)

                # Track bud expense category for deletion
                if bud_expense_category_id:
                    bud_category_ids_to_delete.add(bud_expense_category_id)

            else:
                # CA: Find the credit account
                cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, account))
                ca_row = cursor.fetchone()
                if not ca_row:
                    continue
                account_id = ca_row['id']

                # Find Auto Adjustments CA category for this account
                cursor.execute("""
                    SELECT id FROM c_expense_categories
                    WHERE account_id = %s AND name = 'Auto Adjustments' LIMIT 1
                """, (account_id,))
                auto_adj = cursor.fetchone()
                if not auto_adj:
                    continue
                auto_adj_id = auto_adj['id']

                # Get all c_expense_entries for this bud_item from Redis
                c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
                if c_expense_entries:
                    # Collect Auto Adjustments entries to create
                    ca_auto_adj_entries_to_create = []
                    
                    for e in c_expense_entries:
                        if int(e.get('bud_item_id') or 0) == int(item_id):
                            entry_date = e.get('date')
                            if isinstance(entry_date, str):
                                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                            
                            if entry_date < today:
                                # Collect for Auto Adjustments
                                ca_auto_adj_entries_to_create.append({
                                    'date': entry_date,
                                    'amount': float(e.get('amount', 0))
                                })
                    
                    # Remove all entries with this bud_item_id
                    ca_entries_to_keep = [e for e in c_expense_entries if int(e.get('bud_item_id') or 0) != int(item_id)]
                    
                    # Save filtered entries first
                    _set_entries_to_redis('c_expense_entries', current_user.id, ca_entries_to_keep)
                    
                    # Now create Auto Adjustments entries
                    for ca_auto_adj_entry in ca_auto_adj_entries_to_create:
                        _update_entry_in_redis('c_expense_entries', current_user.id, 
                                             auto_adj_id, ca_auto_adj_entry['date'], 
                                             ca_auto_adj_entry['amount'], 
                                             processed=1, bud_item_id=None)

                # Track CA categories created for this bud (by bud name)
                if bud_name:
                    cursor.execute("""
                        SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s
                    """, (account_id, bud_name))
                    cat_row = cursor.fetchone()
                    if cat_row:
                        ca_category_ids_to_delete.add(cat_row['id'])

        # Delete the bud's expense category if present
        for bud_cat_id in bud_category_ids_to_delete:
            cursor.execute("DELETE FROM expense_categories WHERE id = %s AND user_id = %s", (bud_cat_id, current_user.id))

        # Delete any c_expense_categories created for this bud
        for ca_cat_id in ca_category_ids_to_delete:
            cursor.execute("DELETE FROM c_expense_categories WHERE id = %s", (ca_cat_id,))

        conn.commit()
        cursor.close()
    
    # Delete all bud_items for this bud from Redis
    for bud_item in bud_items:
        _delete_bud_item_in_redis(current_user.id, bud_item['id'])

    # Delete the bud itself from Redis
    _delete_bud_in_redis(current_user.id, bud_id)
        
    save_ca_daily_balance()
    return jsonify({'status': 'success'})

@app.route('/toggle-bud-active', methods=['POST'])
@login_required
def toggle_bud_active():
    data = request.get_json()
    bud_id = int(data.get('bud_id', 0))
    active = int(data.get('active', 0))
    today = date.today()

    if not bud_id:
        return jsonify({'status': 'error', 'message': 'Missing bud_id'}), 400

    # Try Redis first for bud data
    buds = _get_buds_from_redis(current_user.id)
    if buds is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM buds WHERE user_id = %s", (current_user.id,))
            buds = cursor.fetchall()
            cursor.close()
    
    bud_row = next((b for b in buds if int(b['id']) == int(bud_id)), None)
    if not bud_row:
        return jsonify({'status': 'error', 'message': 'Bud not found'}), 404

    bud_name = bud_row['name']

    # Try Redis first for bud_items
    all_bud_items = _get_bud_items_from_redis(current_user.id)
    if all_bud_items is None:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT * FROM bud_items WHERE bud_id IN (SELECT id FROM buds WHERE user_id = %s)", (current_user.id,))
            all_bud_items = cursor.fetchall()
            cursor.close()
    
    bud_items = [item for item in all_bud_items if int(item['bud_id']) == int(bud_id)]

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT id FROM expense_categories WHERE user_id = %s AND name = 'Auto Adjustments' LIMIT 1
        """, (current_user.id,))
        auto_adj = cursor.fetchone()
        auto_adj_id = auto_adj['id'] if auto_adj else None

        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (current_user.id,))
        ca_ids = [row['id'] for row in cursor.fetchall()]

        ca_auto_adj_ids = {}
        for ca_id in ca_ids:
            cursor.execute("""
                SELECT id FROM c_expense_categories WHERE account_id = %s AND name = 'Auto Adjustments' LIMIT 1
            """, (ca_id,))
            ca_auto_adj = cursor.fetchone()
            if ca_auto_adj:
                ca_auto_adj_ids[ca_id] = ca_auto_adj['id']

        if active == 1:
            if not bud_row.get('expense_category_id'):
                cursor.execute("""
                    SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s
                """, (current_user.id,))
                max_order = cursor.fetchone()['COALESCE(MAX(display_order), 0)']
                cursor.execute("""
                    INSERT INTO expense_categories (user_id, name, display_order, is_bud)
                    VALUES (%s, %s, %s, 1)
                """, (current_user.id, bud_name, max_order + 1))
                expense_category_id = cursor.lastrowid
                
                # Update bud in Redis with new expense_category_id
                bud_row['expense_category_id'] = expense_category_id
                bud_row['active'] = active
                _update_bud_in_redis(current_user.id, bud_row)
            else:
                # Update bud active status in Redis
                bud_row['active'] = active
                _update_bud_in_redis(current_user.id, bud_row)

            # Get list of bud_item_ids for this bud to check against auto adjustments
            bud_item_ids = [int(item['id']) for item in bud_items]
            
            for item in bud_items:
                item_id = item['id']
                account = item['account']
                value = item['value']
                item_date = item['date']
                if account and account.lower() == "blankee":
                    # Transfer any auto adjustment entries for this item to bud category
                    # Only transfer if bud_item_id matches one of this bud's items
                    if auto_adj_id:
                        expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
                        if expense_entries:
                            # Find auto adjustment entries to transfer
                            auto_adj_entries_to_transfer = [e for e in expense_entries if (
                                int(e.get('category_id', 0) or 0) == int(auto_adj_id) and 
                                int(e.get('bud_item_id') or 0) == int(item_id) and
                                int(e.get('bud_item_id') or 0) in bud_item_ids
                            )]
                            
                            # Remove auto adjustment entries
                            entries_to_keep = [e for e in expense_entries if not (
                                int(e.get('category_id', 0) or 0) == int(auto_adj_id) and 
                                int(e.get('bud_item_id') or 0) == int(item_id) and
                                int(e.get('bud_item_id') or 0) in bud_item_ids
                            )]
                            _set_entries_to_redis('expense_entries', current_user.id, entries_to_keep)
                            
                            # Transfer to bud category
                            for auto_entry in auto_adj_entries_to_transfer:
                                entry_date = auto_entry.get('date')
                                if isinstance(entry_date, str):
                                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                                _update_entry_in_redis('expense_entries', current_user.id, 
                                                     bud_row['expense_category_id'], entry_date, 
                                                     float(auto_entry.get('amount', 0)), bud_item_id=item_id)
                    
                    # Create expense entry for new items (with item_date)
                    # Check if entry exists for this date and category, add to it if so
                    expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
                    existing_entry = None
                    
                    # Ensure item_date is a date object for comparison
                    if isinstance(item_date, str):
                        item_date_obj = datetime.strptime(item_date, '%Y-%m-%d').date()
                    else:
                        item_date_obj = item_date
                    
                    if expense_entries:
                        for e in expense_entries:
                            e_date = e.get('date')
                            if isinstance(e_date, str):
                                e_date = datetime.strptime(e_date, '%Y-%m-%d').date()
                            if int(e.get('category_id', 0) or 0) == int(bud_row['expense_category_id']) and e_date == item_date_obj:
                                existing_entry = e
                                break
                    
                    if existing_entry:
                        # Add to existing entry - preserve original bud_item_id
                        new_amount = float(existing_entry.get('amount', 0)) + float(value)
                        original_bud_item_id = existing_entry.get('bud_item_id')
                        _update_entry_in_redis('expense_entries', current_user.id, 
                                             bud_row['expense_category_id'], item_date, 
                                             new_amount, bud_item_id=original_bud_item_id)
                    else:
                        # Create new entry
                        _update_entry_in_redis('expense_entries', current_user.id, 
                                             bud_row['expense_category_id'], item_date, 
                                             float(value), bud_item_id=item_id)
                    
                elif account and account.lower() != "blankee":
                    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, account))
                    ca_row = cursor.fetchone()
                    if not ca_row:
                        continue
                    ca_id = ca_row['id']
                    cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (ca_id, bud_name))
                    cat_row = cursor.fetchone()
                    if not cat_row:
                        cursor.execute("SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM c_expense_categories WHERE account_id = %s", (ca_id,))
                        max_order = cursor.fetchone()
                        display_order = max_order['max_display_order'] + 1 if max_order and max_order['max_display_order'] is not None else 1
                        cursor.execute("INSERT INTO c_expense_categories (account_id, name, display_order, is_bud) VALUES (%s, %s, %s, 1)", (ca_id, bud_name, display_order))
                        bud_cat_id = cursor.lastrowid
                    else:
                        bud_cat_id = cat_row['id']
                    
                    # Transfer any auto adjustment entries for this item to bud category
                    # Only transfer if bud_item_id matches one of this bud's items
                    ca_auto_adj_id = ca_auto_adj_ids.get(ca_id)
                    if ca_auto_adj_id:
                        c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
                        if c_expense_entries:
                            # Find auto adjustment entries to transfer
                            ca_auto_adj_entries_to_transfer = [e for e in c_expense_entries if (
                                int(e.get('category_id', 0) or 0) == int(ca_auto_adj_id) and 
                                int(e.get('bud_item_id') or 0) == int(item_id) and
                                int(e.get('bud_item_id') or 0) in bud_item_ids
                            )]
                            
                            # Remove auto adjustment entries
                            entries_to_keep = [e for e in c_expense_entries if not (
                                int(e.get('category_id', 0) or 0) == int(ca_auto_adj_id) and 
                                int(e.get('bud_item_id') or 0) == int(item_id) and
                                int(e.get('bud_item_id') or 0) in bud_item_ids
                            )]
                            _set_entries_to_redis('c_expense_entries', current_user.id, entries_to_keep)
                            
                            # Transfer to bud category
                            for ca_auto_entry in ca_auto_adj_entries_to_transfer:
                                entry_date = ca_auto_entry.get('date')
                                if isinstance(entry_date, str):
                                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                                _update_entry_in_redis('c_expense_entries', current_user.id, 
                                                     bud_cat_id, entry_date, 
                                                     float(ca_auto_entry.get('amount', 0)), bud_item_id=item_id)
                    
                    # Create c_expense entry for new items (with item_date)
                    # Check if entry exists for this date and category, add to it if so
                    c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
                    existing_entry = None
                    
                    # Ensure item_date is a date object for comparison
                    if isinstance(item_date, str):
                        item_date_obj = datetime.strptime(item_date, '%Y-%m-%d').date()
                    else:
                        item_date_obj = item_date
                    
                    if c_expense_entries:
                        for e in c_expense_entries:
                            e_date = e.get('date')
                            if isinstance(e_date, str):
                                e_date = datetime.strptime(e_date, '%Y-%m-%d').date()
                            if int(e.get('category_id', 0) or 0) == int(bud_cat_id) and e_date == item_date_obj:
                                existing_entry = e
                                break
                    
                    if existing_entry:
                        # Add to existing entry - preserve original bud_item_id
                        new_amount = float(existing_entry.get('amount', 0)) + float(value)
                        original_bud_item_id = existing_entry.get('bud_item_id')
                        _update_entry_in_redis('c_expense_entries', current_user.id, 
                                             bud_cat_id, item_date, 
                                             new_amount, bud_item_id=original_bud_item_id)
                    else:
                        # Create new entry
                        _update_entry_in_redis('c_expense_entries', current_user.id, 
                                             bud_cat_id, item_date, 
                                             float(value), bud_item_id=item_id)

        elif active == 0:
            if bud_row.get('expense_category_id'):
                for item in bud_items:
                    item_id = item['id']
                    # Get and filter entries from Redis
                    expense_entries = _get_entries_from_redis('expense_entries', current_user.id)
                    if expense_entries:
                        # Collect Auto Adjustments entries to create
                        auto_adj_entries_to_create = []
                        
                        for e in expense_entries:
                            if int(e.get('category_id', 0) or 0) == int(bud_row['expense_category_id']) and \
                               int(e.get('bud_item_id') or 0) == int(item_id):
                                entry_date = e.get('date')
                                if isinstance(entry_date, str):
                                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                                
                                if entry_date < today and auto_adj_id:
                                    # Collect for Auto Adjustments (preserve bud_item_id)
                                    auto_adj_entries_to_create.append({
                                        'date': entry_date,
                                        'amount': float(e.get('amount', 0)),
                                        'bud_item_id': int(e.get('bud_item_id', 0))
                                    })
                        
                        # Remove entries with this bud category and bud_item_id
                        entries_to_keep = [e for e in expense_entries if not (
                            int(e.get('category_id', 0) or 0) == int(bud_row['expense_category_id']) and 
                            int(e.get('bud_item_id') or 0) == int(item_id)
                        )]
                        
                        # Save filtered entries first
                        _set_entries_to_redis('expense_entries', current_user.id, entries_to_keep)
                        
                        # Now create Auto Adjustments entries (preserve bud_item_id for reactivation)
                        for auto_adj_entry in auto_adj_entries_to_create:
                            _update_entry_in_redis('expense_entries', current_user.id, 
                                                 auto_adj_id, auto_adj_entry['date'], 
                                                 auto_adj_entry['amount'], 
                                                 processed=1, bud_item_id=auto_adj_entry['bud_item_id'])

            for item in bud_items:
                item_id = item['id']
                account = item['account']
                if account and account.lower() != "blankee":
                    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, account))
                    ca_row = cursor.fetchone()
                    if not ca_row:
                        continue
                    ca_id = ca_row['id']
                    ca_auto_adj_id = ca_auto_adj_ids.get(ca_id)
                    cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (ca_id, bud_name))
                    cat_row = cursor.fetchone()
                    if not cat_row:
                        continue
                    bud_cat_id = cat_row['id']
                    
                    # Get and filter c_expense entries from Redis
                    c_expense_entries = _get_entries_from_redis('c_expense_entries', current_user.id)
                    if c_expense_entries:
                        # Collect Auto Adjustments entries to create
                        ca_auto_adj_entries_to_create = []
                        
                        for e in c_expense_entries:
                            if int(e.get('category_id', 0) or 0) == int(bud_cat_id) and \
                               int(e.get('bud_item_id') or 0) == int(item_id):
                                entry_date = e.get('date')
                                if isinstance(entry_date, str):
                                    entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                                
                                if entry_date < today and ca_auto_adj_id:
                                    # Collect for Auto Adjustments (preserve bud_item_id)
                                    ca_auto_adj_entries_to_create.append({
                                        'date': entry_date,
                                        'amount': float(e.get('amount', 0)),
                                        'bud_item_id': int(e.get('bud_item_id', 0))
                                    })
                        
                        # Remove entries with this bud category and bud_item_id
                        ca_entries_to_keep = [e for e in c_expense_entries if not (
                            int(e.get('category_id', 0) or 0) == int(bud_cat_id) and 
                            int(e.get('bud_item_id') or 0) == int(item_id)
                        )]
                        
                        # Save filtered entries first
                        _set_entries_to_redis('c_expense_entries', current_user.id, ca_entries_to_keep)
                        
                        # Now create Auto Adjustments entries (preserve bud_item_id for reactivation)
                        for ca_auto_adj_entry in ca_auto_adj_entries_to_create:
                            _update_entry_in_redis('c_expense_entries', current_user.id, 
                                                 ca_auto_adj_id, ca_auto_adj_entry['date'], 
                                                 ca_auto_adj_entry['amount'], 
                                                 processed=1, bud_item_id=ca_auto_adj_entry['bud_item_id'])

            if bud_row.get('expense_category_id'):
                cursor.execute("""
                    DELETE FROM expense_categories WHERE id = %s AND user_id = %s
                """, (bud_row['expense_category_id'], current_user.id))
                
                # Update bud in Redis - set inactive and remove expense_category_id
                bud_row['active'] = active
                bud_row['expense_category_id'] = None
                _update_bud_in_redis(current_user.id, bud_row)
            else:
                # Update bud active status in Redis
                bud_row['active'] = active
                _update_bud_in_redis(current_user.id, bud_row)
        else:
            # Update bud active status in Redis
            bud_row['active'] = active
            _update_bud_in_redis(current_user.id, bud_row)

        conn.commit()
        cursor.close()
        
    save_ca_daily_balance()
    return jsonify({'status': 'success'})

#############################################################################################
##################################### CREDIT ACCOUNTS #######################################
#############################################################################################

@app.route('/credit_accounts')
@login_required
def credit_accounts():
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Fetch all credit accounts for the current user
        cursor.execute("""
            SELECT * FROM credit_accounts
            WHERE user_id = %s
            ORDER BY id ASC
        """, (current_user.id,))
        credit_accounts = cursor.fetchall()

        # Fetch all c_expense_categories for the user's credit accounts
        cursor.execute("""
            SELECT cec.*, ca.name AS account_name
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cec.display_order ASC, cec.id ASC
        """, (current_user.id,))
        c_expense_categories = cursor.fetchall()

        # Fetch all c_expense_entries for the user's categories
        cursor.execute("""
            SELECT cee.*, cec.name AS category_name
            FROM c_expense_entries cee
            JOIN c_expense_categories cec ON cee.category_id = cec.id
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
            ORDER BY cee.date DESC, cee.id ASC
        """, (current_user.id,))
        c_expense_entries = cursor.fetchall()

        # Fetch all c_a_balances for the user
        cursor.execute("""
            SELECT * FROM c_a_balances
            WHERE account_id IN (
                SELECT id FROM credit_accounts WHERE user_id = %s
            )
            ORDER BY date DESC
        """, (current_user.id,))
        c_a_balances = cursor.fetchall()

        # Fetch all c_a_balances_d for the user's credit accounts
        cursor.execute("""
            SELECT * FROM c_a_balances_d
            WHERE account_id IN (
                SELECT id FROM credit_accounts WHERE user_id = %s
            )
            ORDER BY date DESC
        """, (current_user.id,))
        c_a_balances_d = cursor.fetchall()

        # Fetch all c_a_balances_m for the user
        cursor.execute("""
            SELECT * FROM c_a_balances_m
            WHERE account_id IN (
                SELECT id FROM credit_accounts WHERE user_id = %s
            )
            ORDER BY date DESC
        """, (current_user.id,))
        c_a_balances_m = cursor.fetchall()

        # Fetch landing_page and currency_type from users table
        cursor.execute("""
            SELECT landing_page, currency_type FROM users WHERE id = %s
        """, (current_user.id,))
        user_row = cursor.fetchone()
        landing_page = user_row['landing_page'] if user_row and 'landing_page' in user_row else 'dashboard_3m'
        currency_type = user_row['currency_type'] if user_row and 'currency_type' in user_row else 'USD'

        today_str = date.today().strftime('%Y-%m-%d')
        ca_balances_today = {}
        for row in c_a_balances_d:
            if str(row['date']) == today_str:
                ca_balances_today[row['account_id']] = row['balance']

        cursor.close()

    return render_template(
        'credit_accounts.html',
        credit_accounts=credit_accounts,
        c_expense_categories=c_expense_categories,
        c_expense_entries=c_expense_entries,
        c_a_balances=c_a_balances,
        ca_balances_today=ca_balances_today,
        c_a_balances_m=c_a_balances_m,
        landing_page=landing_page,
        currency_type=currency_type
    )

@app.route('/add-credit-account', methods=['POST'])
@login_required
def add_credit_account():
    data = request.get_json()
    name = data.get('name')
    interest_rate = data.get('interest_rate')
    account_type = data.get('type')
    starting_balance = data.get('starting_balance', None)

    if not name or not interest_rate or not account_type:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        is_card = 1 if account_type == 'card' else 0
        is_line = 1 if account_type == 'line' else 0

        cursor.execute("""
            INSERT INTO credit_accounts (user_id, name, interest_rate, is_card, is_line)
            VALUES (%s, %s, %s, %s, %s)
        """, (current_user.id, name, interest_rate, is_card, is_line))
        conn.commit()
        account_id = cursor.lastrowid

        # Create "Interest Charge" category
        cursor.execute("""
            INSERT INTO c_expense_categories (account_id, name, display_order, is_interest)
            VALUES (%s, %s, %s, %s)
        """, (account_id, "Interest Charge", -1, 1))

        # Create "Auto Adjustments" category
        cursor.execute("""
            INSERT INTO c_expense_categories (account_id, name, display_order, is_auto_adjustment)
            VALUES (%s, %s, %s, %s)
        """, (account_id, "Auto Adjustments", 0, 1))

        # Create "Starting Balance" category
        cursor.execute("""
            INSERT INTO c_expense_categories (account_id, name, display_order)
            VALUES (%s, %s, %s)
        """, (account_id, "Starting Balance", 1))
        starting_balance_category_id = cursor.lastrowid

        # Create matching expense_categories record
        payment_category_name = f"{name} payment"
        cursor.execute("""
            SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM expense_categories WHERE user_id = %s
        """, (current_user.id,))
        row = cursor.fetchone()
        max_display_order = row['max_display_order'] if row and row['max_display_order'] is not None else 0
        new_display_order = max_display_order + 1

        cursor.execute("""
            INSERT INTO expense_categories (user_id, name, display_order, is_credit_account)
            VALUES (%s, %s, %s, 1)
        """, (current_user.id, payment_category_name, new_display_order))

        # Insert c_expense_entries for starting balance if provided
        if starting_balance and float(starting_balance) != 0.0:
            today_str = date.today().strftime('%Y-%m-%d')
            cursor.execute("""
                INSERT INTO c_expense_entries (category_id, date, amount)
                VALUES (%s, %s, %s)
            """, (starting_balance_category_id, today_str, starting_balance))

        conn.commit()
        cursor.close()

    initialize_ca_balances_for_account(account_id)
    save_ca_daily_balance()
    save_totals_remainders_d()

    return jsonify({'status': 'success', 'account_id': account_id})

def initialize_ca_balances_for_account(account_id):
    today = date.today()
    start_year = today.year - 1
    end_year = today.year + 3

    # Daily: every day from Jan 1 of previous year to Dec 31 of year+3
    start_date = date(start_year, 1, 1)
    end_date = date(end_year, 12, 31)

    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Insert daily balances
        current = start_date
        while current <= end_date:
            cursor.execute("""
                INSERT INTO c_a_balances_d (account_id, date, total_expenses, balance)
                VALUES (%s, %s, 0.00, 0.00)
                ON DUPLICATE KEY UPDATE account_id = account_id
            """, (account_id, current))
            current += timedelta(days=1)

        # Insert weekly (Fridays) balances
        # Find the first Friday on or after start_date
        first_friday = start_date + timedelta((4 - start_date.weekday()) % 7)
        current = first_friday
        while current <= end_date:
            cursor.execute("""
                INSERT INTO c_a_balances (account_id, date, total_expenses, balance)
                VALUES (%s, %s, 0.00, 0.00)
                ON DUPLICATE KEY UPDATE account_id = account_id
            """, (account_id, current))
            current += timedelta(days=7)

        # Insert monthly (last day of month) balances
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                last_day = calendar.monthrange(year, month)[1]
                last_date = date(year, month, last_day)
                if start_date <= last_date <= end_date:
                    cursor.execute("""
                        INSERT INTO c_a_balances_m (account_id, date, total_expenses, balance)
                        VALUES (%s, %s, 0.00, 0.00)
                        ON DUPLICATE KEY UPDATE account_id = account_id
                    """, (account_id, last_date))

        cursor.close()
        conn.commit()

@app.route('/delete-credit-account', methods=['POST'])
@login_required
def delete_credit_account():
    data = request.get_json()
    account_id = data.get('id')
    if not account_id:
        return jsonify({'status': 'error', 'message': 'Missing account id'}), 400

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Get the credit account name before deleting
            cursor.execute(
                "SELECT name FROM credit_accounts WHERE id = %s AND user_id = %s",
                (account_id, current_user.id)
            )
            account_row = cursor.fetchone()
            account_name = account_row['name'] if account_row else None

            # Update bud_items that reference this account
            if account_name:
                cursor.execute(
                    "UPDATE bud_items SET account = %s WHERE account = %s",
                    ("deleted account", account_name)
                )

            # Delete the credit account
            cursor.execute(
                "DELETE FROM credit_accounts WHERE id = %s AND user_id = %s",
                (account_id, current_user.id)
            )
            conn.commit()
            cursor.close()

        # After deleting the credit account, delete the associated expense category
        if account_name:
            payment_category_name = f"{account_name} Payment"
            # Open a new connection for the next operation
            with get_db_pool().get_connection() as conn2:
                cursor2 = conn2.cursor()
                cursor2.execute("""
                    SELECT id FROM expense_categories
                    WHERE user_id = %s AND name = %s AND is_credit_account = 1
                    LIMIT 1
                """, (current_user.id, payment_category_name))
                row = cursor2.fetchone()
                cursor2.close()
                
                if row:
                    from flask import Request
                    with app.test_request_context(
                        '/delete_expense_category',
                        method='POST',
                        data={'id': row[0]}
                    ):
                        delete_expense_category()

        save_ca_daily_balance()
        save_totals_remainders_d()

        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/update-credit-account', methods=['POST'])
@login_required
def update_credit_account():
    data = request.get_json()
    account_id = data.get('id')
    field = data.get('field')
    value = data.get('value')

    if field not in ['name', 'interest_rate', 'type']:
        return jsonify({'status': 'error', 'message': 'Invalid field.'})

    try:
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            if field == 'type':
                is_card = 1 if value == 'card' else 0
                is_line = 1 if value == 'line' else 0
                cursor.execute(
                    "UPDATE credit_accounts SET is_card = %s, is_line = %s WHERE id = %s AND user_id = %s",
                    (is_card, is_line, account_id, current_user.id)
                )
            else:
                cursor.execute(
                    f"UPDATE credit_accounts SET {field} = %s WHERE id = %s AND user_id = %s",
                    (value, account_id, current_user.id)
                )
            conn.commit()
            cursor.close()
        
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


#################################################################################
########################## APPLICATION LIFECYCLE ################################
#################################################################################

@app.teardown_appcontext
def shutdown_redis_on_teardown(exception=None):
    """Clean up Redis connections on app context teardown"""
    pass  # Redis client handles its own cleanup


def cleanup_on_exit():
    """Cleanup function to run on application exit"""
    try:
        app.logger.info("Application shutting down...")
        shutdown_redis_manager()
        dispose_db_pool()
        app.logger.info("Cleanup complete")
    except Exception as e:
        app.logger.error(f"Error during cleanup: {e}")


# Register cleanup handler
import atexit
atexit.register(cleanup_on_exit)


if __name__ == '__main__':
    # Run development server
    # For production, use WSGI server (gunicorn, uWSGI, etc.)
    app.run(host='0.0.0.0', port=5000, debug=True)