import mysql.connector
import re  # Import the regular expression module
import os
import calendar
import pyotp
import qrcode
import io
import base64
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

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@login_manager.unauthorized_handler
def unauthorized():
    # Redirect unauthorized users to the login page
    return redirect(url_for('login'))

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
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        conn.close()

        if user:
            return User(id=user[0], username=user[1], password=user[2])
        return None

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

### Register ###
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
    landing_page = request.form.get('landing_page', 'dashboard')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET landing_page = %s WHERE id = %s", (landing_page, current_user.id))
    conn.commit()
    conn.close()
    flash('Landing page updated.')
    return redirect(url_for('profile'))

def create_totals_remainders_for_new_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    today = date.today()

    # Create entries for weekly totals (totals_remainders) for 1 year back and up to 5 years forward
    one_year_back = date(today.year - 1, 1, 1)  # Start from January 1st of last year
    five_years_forward = date(today.year + 5, 12, 31)  # End at December 31st, 5 years from now

    # Find the nearest Friday for the start date
    start_date = find_nearest_friday(one_year_back, round_up=False)
    
    # For the end date, we need to check if it falls beyond December 31st and restrict it to that date
    end_date = find_nearest_friday(five_years_forward, round_up=True)
    if end_date > five_years_forward:
        end_date = five_years_forward

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
    while current_date <= five_years_forward:
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
    # For each month from one_year_back to five_years_forward, find the last day of the month
    current_month = one_year_back.replace(day=1)
    last_month = five_years_forward.replace(day=1)
    while current_month <= last_month:
        # Find the last day of the current month
        year = current_month.year
        month = current_month.month
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)
        if last_day > five_years_forward:
            last_day = five_years_forward
        cursor.execute("""
            INSERT INTO totals_remainders_m (user_id, date, total_income, total_expenses, remainder, last_month_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, last_day, 0.00, 0.00, 0.00, 0.00))
        # Move to the first day of the next month
        if month == 12:
            current_month = date(year + 1, 1, 1)
        else:
            current_month = date(year, month + 1, 1)

    conn.commit()
    conn.close()

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

        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if the username already exists
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        existing_user = cursor.fetchone()

        if existing_user:
            flash('Username already exists. Please choose a different one.')
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
        conn = get_db_connection()
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

        conn.commit()
        conn.close()

        # Return success response
        return jsonify({'status': 'success'})

    return redirect(url_for('dashboard'))

@app.route('/check_username', methods=['POST'])
def check_username():
    username = request.form['username']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    existing_user = cursor.fetchone()

    conn.close()

    if existing_user:
        return jsonify({'status': 'taken'})
    else:
        return jsonify({'status': 'available'})

### Login ###
@app.route('/login', methods=['GET', 'POST'])
def login():
    # If the user is already authenticated, redirect to their preferred landing page
    if current_user.is_authenticated:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT landing_page FROM users WHERE id = %s", (current_user.id,))
        landing_page = cursor.fetchone()
        conn.close()
        if landing_page and landing_page[0]:
            return redirect(url_for(landing_page[0]))
        else:
            return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember = 'remember' in request.form  # Get the value of the Remember Me checkbox

        # Establish database connection
        conn = get_db_connection()
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
                conn.close()
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

                cutoff_date = date.today().replace(month=12, day=31, year=date.today().year + 5)
                year_5 = date.today().year + 5

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
                    """, (category_id, year_5))
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
                                'end_date': date(year_5, 12, 31).strftime('%Y-%m-%d'),
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
                    """, (category_id, year_5))
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
                                'end_date': date(year_5, 12, 31).strftime('%Y-%m-%d'),
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
                    """, (category_id, year_5))
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
                                'end_date': date(year_5, 12, 31).strftime('%Y-%m-%d'),
                                'weekdays': rec[5].split(',') if rec[5] else [],
                                'monthly_days': [int(x) for x in rec[6].split(',')] if rec[6] else [],
                                'yearly_day': rec[7],
                                'yearly_month': rec[8],
                                'no_end_date': 1
                            }
                            with app.test_request_context():
                                update_recurring_ca_expense_inner(data, user_obj.id)

                # If the last recorded year is older than the current year + 5, add a new year of Fridays
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
                conn.close()
                if landing_page:
                    return redirect(url_for(landing_page))
                else:
                    return redirect(url_for('dashboard'))

        else:
            conn.close()
            # Invalid username or password, redirect back to login with an error message
            flash("Invalid username or password")
            return render_template('login.html')

    # If it's a GET request, render the login page
    return render_template('login.html')

@app.route('/login_mfa', methods=['POST'])
def login_mfa():
    code = request.form.get('mfa_code')
    user_id = session.get('pre_mfa_user_id')
    remember = session.get('pre_mfa_remember', False)
    if not user_id:
        flash('Session expired. Please log in again.')
        return redirect(url_for('login'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, mfa_secret, landing_page FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    conn.close()
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

def add_one_year_of_fridays(user_id):
    conn = get_db_connection()
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

    # Do not go beyond Dec 31st, 5 years from the current year
    max_end_date = date(date.today().year + 5, 12, 31)
    end_date = min(end_date, max_end_date)

    current_date = start_date
    while current_date <= end_date:
        cursor.execute("""
            INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))

        current_date += timedelta(weeks=1)  # Move to the next Friday

    save_totals_remainders_d()

    conn.commit()
    conn.close()

def add_one_year_of_days(user_id):
    conn = get_db_connection()
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

    # Do not go beyond Dec 31st, 5 years from the current year
    max_end_date = date(date.today().year + 5, 12, 31)
    end_date = min(end_date, max_end_date)

    current_date = start_date
    while current_date <= end_date:
        cursor.execute("""
            INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder, last_day_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, current_date, 0.00, 0.00, 0.00, 0.00))
        current_date += timedelta(days=1)  # Move to the next day

    save_totals_remainders_d()

    conn.commit()
    conn.close()

def add_one_year_of_months(user_id):
    conn = get_db_connection()
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

    # Do not go beyond Dec 31st, 5 years from the current year
    max_end_date = date(date.today().year + 5, 12, 31)
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

    conn.commit()
    conn.close()

def add_one_year_of_savings(user_id):
    conn = get_db_connection()
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
    max_end_date = date(date.today().year + 5, 12, 31)
    end_date = min(end_date, max_end_date)

    current_date = start_date
    while current_date <= end_date:
        cursor.execute("""
            INSERT INTO savings_entries (user_id, date, amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = amount
        """, (user_id, current_date, 0.00))
        current_date += timedelta(days=1)

    conn.commit()
    conn.close()

def add_one_year_of_ca_fridays(user_id):
    conn = get_db_connection()
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
        max_end_date = date(date.today().year + 5, 12, 31)
        end_date = min(end_date, max_end_date)
        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO c_a_balances (account_id, date, total_expenses, balance)
                VALUES (%s, %s, 0.00, 0.00)
            """, (account_id, current_date))
            current_date += timedelta(weeks=1)
    conn.commit()
    conn.close()

def add_one_year_of_ca_days(user_id):
    conn = get_db_connection()
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
        max_end_date = date(date.today().year + 5, 12, 31)
        end_date = min(end_date, max_end_date)
        current_date = start_date
        while current_date <= end_date:
            cursor.execute("""
                INSERT INTO c_a_balances_d (account_id, date, total_expenses, balance)
                VALUES (%s, %s, 0.00, 0.00)
            """, (account_id, current_date))
            current_date += timedelta(days=1)
    conn.commit()
    conn.close()

def add_one_year_of_ca_months(user_id):
    conn = get_db_connection()
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
        max_end_date = date(date.today().year + 5, 12, 31)
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
    conn.commit()
    conn.close()

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

################################## Dashboard Day ############################################

@app.route('/dashboard_d')
@login_required
def dashboard_d():
    selected_date = request.args.get('date')  # Get the selected date from query parameters
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')  # Default to current UTC date as a string

    # Establish the database connection
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch user data including the desired fields (profile_picture, first_name, last_name, balance_threshold, goofy_week_mode)
    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
        FROM users
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

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

    # Fetch all income entries within the user's account, along with associated category information and processed status, ordered by category display_order DESC, then date
    cursor.execute("""
        SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s
        ORDER BY ic.display_order DESC, ie.date ASC
    """, (current_user.id,))
    income_entries = cursor.fetchall()

    # Fetch all expense entries with associated category information and processed status for the user, ordered by category display_order DESC, then date
    cursor.execute("""
        SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s
        ORDER BY ec.display_order DESC, ee.date ASC
    """, (current_user.id,))
    expense_entries = cursor.fetchall()

    # Fetch the updated totals and remainders after processing
    cursor.execute("""
        SELECT * FROM totals_remainders_d
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    totals_remainders_d = cursor.fetchall()

    # Fetch all savings entries for the user
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    savings_entries = cursor.fetchall()

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

    # Fetch c_expense_entries for the user's categories
    cursor.execute("""
        SELECT cee.*, cec.name AS category_name
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
        ORDER BY cee.date DESC, cee.id ASC
    """, (current_user.id,))
    c_expense_entries = cursor.fetchall()

    # Fetch all c_a_balances_d for the user's credit accounts
    cursor.execute("""
        SELECT * FROM c_a_balances_d
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY date DESC
    """, (current_user.id,))
    c_a_balances_d = cursor.fetchall()

    # Close the database connection
    conn.close()

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

def update_daily_totals(user_id, start_date, goofy_week_mode, date_to_remainder):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)
    cursor.execute("""
        SELECT date FROM totals_remainders_d
        WHERE user_id = %s AND date >= %s
        ORDER BY date ASC
    """, (user_id, start_date))
    all_dates = [row[0] for row in cursor.fetchall()]

    prev_date = start_date - timedelta(days=1)
    cursor.execute("""
        SELECT remainder FROM totals_remainders_d
        WHERE user_id = %s AND date = %s
    """, (user_id, prev_date))
    prev_remainder_row = cursor.fetchone()
    last_day_remainder = float(prev_remainder_row[0]) if prev_remainder_row else 0.0

    # Fetch all income for the range
    cursor.execute("""
        SELECT ie.date, SUM(ie.amount)
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s AND ie.date >= %s
        GROUP BY ie.date
    """, (user_id, start_date))
    income_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

    # Fetch all expenses for the range
    cursor.execute("""
        SELECT ee.date, SUM(ee.amount)
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s AND ee.date >= %s
        GROUP BY ee.date
    """, (user_id, start_date))
    expense_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

    for current_date in all_dates:
        total_income = income_by_date.get(current_date, 0) + last_day_remainder
        total_expenses = expense_by_date.get(current_date, 0)

        remainder = total_income - total_expenses

        cursor.execute("""
            INSERT INTO totals_remainders_d (user_id, date, total_income, total_expenses, remainder, last_day_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_income = VALUES(total_income),
                total_expenses = VALUES(total_expenses),
                remainder = VALUES(remainder),
                last_day_remainder = VALUES(last_day_remainder)
        """, (user_id, current_date, total_income, total_expenses, remainder, last_day_remainder))

        last_day_remainder = remainder
        date_to_remainder[current_date] = remainder

    conn.commit()
    conn.close()

def update_daily_ca_totals(user_id, start_date):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
    account_ids = [row[0] for row in cursor.fetchall()]
    if not account_ids:
        conn.close()
        return

    for account_id in account_ids:
        cursor.execute("""
            SELECT date FROM c_a_balances_d
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        all_dates = [row[0] for row in cursor.fetchall()]

        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT balance FROM c_a_balances_d
            WHERE account_id = %s AND date = %s
        """, (account_id, prev_date))
        prev_balance_row = cursor.fetchone()
        last_day_balance = float(prev_balance_row[0]) if prev_balance_row and prev_balance_row[0] is not None else 0.0

        # Get all c_expense_entries for this account, grouped by date
        cursor.execute("""
            SELECT cee.date, SUM(cee.amount)
            FROM c_expense_entries cee
            JOIN c_expense_categories cec ON cee.category_id = cec.id
            WHERE cec.account_id = %s AND cee.date >= %s
            GROUP BY cee.date
        """, (account_id, start_date))
        expense_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

        # Get all payments for this account, grouped by date
        cursor.execute("""
            SELECT date, SUM(amount) FROM c_payment_entries
            WHERE account_id = %s AND date >= %s
            GROUP BY date
        """, (account_id, start_date))
        payments_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

        for current_date in all_dates:
            total_expenses = expense_by_date.get(current_date, 0.0)
            total_payments = payments_by_date.get(current_date, 0.0)
            balance = last_day_balance + total_expenses - total_payments

            cursor.execute("""
                INSERT INTO c_a_balances_d (account_id, date, total_expenses, total_payments, balance)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_expenses = VALUES(total_expenses),
                    total_payments = VALUES(total_payments),
                    balance = VALUES(balance)
            """, (account_id, current_date, total_expenses, total_payments, balance))

            last_day_balance = balance

    conn.commit()
    conn.close()

def update_weekly_ca_totals(user_id, start_date, goofy_week_mode=False):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
    account_ids = [row[0] for row in cursor.fetchall()]
    if not account_ids:
        conn.close()
        return

    for account_id in account_ids:
        cursor.execute("""
            SELECT date FROM c_a_balances
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        all_week_dates = [row[0] for row in cursor.fetchall()]

        # Build a mapping of week date to all dates in that week
        week_map = {}
        for week_date in all_week_dates:
            if goofy_week_mode:
                # Goofy: week starts Friday, ends Thursday
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                # Normal: week ends Friday, starts Saturday before
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            week_map[week_date] = (week_start, week_end)

        # Sum all expenses for each week
        week_expenses = {}
        for week_date, (week_start, week_end) in week_map.items():
            cursor.execute("""
                SELECT COALESCE(SUM(cee.amount), 0)
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date BETWEEN %s AND %s
            """, (account_id, week_start, week_end))
            week_expenses[week_date] = float(cursor.fetchone()[0])

        # Sum all payments for each week
        week_payments = {}
        for week_date, (week_start, week_end) in week_map.items():
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
            """, (account_id, week_start, week_end))
            week_payments[week_date] = float(cursor.fetchone()[0])

        for week_date, (week_start, week_end) in week_map.items():
            prev_week_date = week_date - timedelta(days=7)
            cursor.execute("""
                SELECT balance FROM c_a_balances
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_week_date))
            prev_balance_row = cursor.fetchone()
            last_week_balance = float(prev_balance_row[0]) if prev_balance_row and prev_balance_row[0] is not None else 0.0

            total_expenses = week_expenses.get(week_date, 0.0)
            total_payments = week_payments.get(week_date, 0.0)
            balance = last_week_balance + total_expenses - total_payments
            cursor.execute("""
                INSERT INTO c_a_balances (account_id, date, total_expenses, total_payments, balance)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_expenses = VALUES(total_expenses),
                    total_payments = VALUES(total_payments),
                    balance = VALUES(balance)
            """, (account_id, week_date, total_expenses, total_payments, balance))

    conn.commit()
    conn.close()

def update_monthly_ca_totals(user_id, start_date):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
    account_ids = [row[0] for row in cursor.fetchall()]
    if not account_ids:
        conn.close()
        return

    for account_id in account_ids:
        cursor.execute("""
            SELECT date FROM c_a_balances_m
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        all_months = [row[0] for row in cursor.fetchall()]

        # Build a mapping of month-end date to all dates in that month
        month_map = {}
        for last_day in all_months:
            month_start = last_day.replace(day=1)
            month_map[last_day] = (month_start, last_day)

        # Sum all expenses for each month
        month_expenses = {}
        for last_day, (month_start, month_end) in month_map.items():
            cursor.execute("""
                SELECT COALESCE(SUM(cee.amount), 0)
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date BETWEEN %s AND %s
            """, (account_id, month_start, month_end))
            month_expenses[last_day] = float(cursor.fetchone()[0])

        # Sum all payments for each month
        month_payments = {}
        for last_day, (month_start, month_end) in month_map.items():
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
            """, (account_id, month_start, month_end))
            month_payments[last_day] = float(cursor.fetchone()[0])

        for last_day in all_months:
            # Always fetch previous month's last day balance for each month
            if last_day.month == 1:
                prev_year = last_day.year - 1
                prev_month = 12
            else:
                prev_year = last_day.year
                prev_month = last_day.month - 1
            prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
            cursor.execute("""
                SELECT balance FROM c_a_balances_m
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_last_day))
            prev_balance_row = cursor.fetchone()
            last_month_balance = float(prev_balance_row[0]) if prev_balance_row and prev_balance_row[0] is not None else 0.0

            total_expenses = month_expenses.get(last_day, 0.0)
            total_payments = month_payments.get(last_day, 0.0)
            balance = last_month_balance + total_expenses - total_payments
            cursor.execute("""
                INSERT INTO c_a_balances_m (account_id, date, total_expenses, total_payments, balance)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_expenses = VALUES(total_expenses),
                    total_payments = VALUES(total_payments),
                    balance = VALUES(balance)
            """, (account_id, last_day, total_expenses, total_payments, balance))

    conn.commit()
    conn.close()

def update_daily_savings_for_savings_category(user_id, start_date):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    # Get member_since and starting_savings for this user
    cursor.execute("SELECT member_since, starting_savings FROM users WHERE id = %s", (user_id,))
    user_row = cursor.fetchone()
    if user_row and user_row[0]:
        member_since = user_row[0]
        starting_savings = float(user_row[1]) if user_row[1] is not None else 0.0
    else:
        member_since = start_date
        starting_savings = 0.0

    # Get the "Savings" income and expense category IDs for this user
    cursor.execute("""
        SELECT id FROM income_categories WHERE user_id = %s AND name = 'Savings'
    """, (user_id,))
    income_savings_row = cursor.fetchone()
    income_savings_id = income_savings_row[0] if income_savings_row else None

    cursor.execute("""
        SELECT id FROM expense_categories WHERE user_id = %s AND name = 'Savings'
    """, (user_id,))
    expense_savings_row = cursor.fetchone()
    expense_savings_id = expense_savings_row[0] if expense_savings_row else None

    if not income_savings_id and not expense_savings_id:
        conn.close()
        return  # No savings categories found

    # Get all dates to update, in order
    cursor.execute("""
        SELECT date FROM totals_remainders_d
        WHERE user_id = %s AND date >= %s
        ORDER BY date ASC
    """, (user_id, start_date))
    all_dates = [row[0] for row in cursor.fetchall()]

    # Get previous day's savings
    prev_date = start_date - timedelta(days=1)
    cursor.execute("""
        SELECT amount FROM savings_entries
        WHERE user_id = %s AND date = %s
    """, (user_id, prev_date))
    prev_savings_row = cursor.fetchone()
    last_savings = float(prev_savings_row[0]) if prev_savings_row else 0.0

    for current_date in all_dates:
        # Sum income for the date (Savings category only)
        total_income = 0.0
        if income_savings_id:
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM income_entries
                WHERE category_id = %s AND date = %s
            """, (income_savings_id, current_date))
            total_income = float(cursor.fetchone()[0])

        # Sum expenses for the date (Savings category only)
        total_expenses = 0.0
        if expense_savings_id:
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM expense_entries
                WHERE category_id = %s AND date = %s
            """, (expense_savings_id, current_date))
            total_expenses = float(cursor.fetchone()[0])

        # Only add starting_savings on the member_since date
        if current_date == member_since:
            savings = last_savings + total_expenses - total_income + starting_savings
        else:
            savings = last_savings + total_expenses - total_income

        # Insert or update savings_entries for this date
        cursor.execute("""
            INSERT INTO savings_entries (user_id, date, amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = VALUES(amount)
        """, (user_id, current_date, savings))

        last_savings = savings

    conn.commit()
    conn.close()

def update_weekly_totals(user_id, start_date, goofy_week_mode, date_to_remainder):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    # Get all relevant Fridays (or week starts) from totals_remainders
    cursor.execute("""
        SELECT date FROM totals_remainders
        WHERE user_id = %s AND date >= %s
        ORDER BY date ASC
    """, (user_id, start_date))
    all_week_dates = [row[0] for row in cursor.fetchall()]

    # Fetch all income and expense entries in one query each
    cursor.execute("""
        SELECT ie.date, ie.amount
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s AND ie.date >= %s
    """, (user_id, start_date))
    income_entries = cursor.fetchall()

    cursor.execute("""
        SELECT ee.date, ee.amount
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s AND ee.date >= %s
    """, (user_id, start_date))
    expense_entries = cursor.fetchall()

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

    for week_date in all_week_dates:
        week_start, week_end = get_week_range(week_date)

        # Sum income for this week
        total_income = sum(
            float(amount)
            for entry_date, amount in income_entries
            if week_start <= entry_date <= week_end
        )

        # Sum expenses for this week
        total_expenses = sum(
            float(amount)
            for entry_date, amount in expense_entries
            if week_start <= entry_date <= week_end
        )

        # Get last week's remainder
        prev_week_date = week_date - timedelta(days=7)
        last_week_remainder = date_to_remainder.get(prev_week_date, 0)

        # Add last week's remainder to income
        total_income_with_remainder = total_income + float(last_week_remainder)
        week_remainder = total_income_with_remainder - total_expenses

        cursor.execute("""
            INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_income = VALUES(total_income),
                total_expenses = VALUES(total_expenses),
                remainder = VALUES(remainder),
                last_week_remainder = VALUES(last_week_remainder)
        """, (user_id, week_date, total_income_with_remainder, total_expenses, week_remainder, last_week_remainder))

        # Update date_to_remainder for next week
        date_to_remainder[week_date] = week_remainder

    conn.commit()
    conn.close()

def update_monthly_totals(user_id, start_date, date_to_remainder):
    conn = get_db_connection()
    cursor = conn.cursor(buffered=True)

    # Get all relevant dates from totals_remainders_d
    cursor.execute("""
        SELECT date FROM totals_remainders_d
        WHERE user_id = %s AND date >= %s
        ORDER BY date ASC
    """, (user_id, start_date))
    all_dates = [row[0] for row in cursor.fetchall()]

    if not all_dates:
        conn.close()
        return

    # Group dates by month
    months = set((d.year, d.month) for d in all_dates)
    for year, month in sorted(months):
        # Get all dates in this month
        month_dates = [d for d in all_dates if d.year == year and d.month == month]
        if not month_dates:
            continue
        first_day = min(month_dates)
        last_day = max(month_dates)

        # Sum income and expenses for the month from entries
        cursor.execute("""
            SELECT COALESCE(SUM(ie.amount), 0)
            FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s AND ie.date BETWEEN %s AND %s
        """, (user_id, first_day, last_day))
        total_income = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COALESCE(SUM(ee.amount), 0)
            FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s AND ee.date BETWEEN %s AND %s
        """, (user_id, first_day, last_day))
        total_expenses = cursor.fetchone()[0]

        # Get last month's remainder
        prev_month = (month - 1) or 12
        prev_year = year if month > 1 else year - 1
        cursor.execute("""
            SELECT remainder FROM totals_remainders_m
            WHERE user_id = %s AND YEAR(date) = %s AND MONTH(date) = %s
            ORDER BY date DESC LIMIT 1
        """, (user_id, prev_year, prev_month))
        prev_remainder_row = cursor.fetchone()
        last_month_remainder = float(prev_remainder_row[0]) if prev_remainder_row else 0.0

        # Include last month's remainder in total_income (like weekly logic)
        total_income_with_remainder = float(total_income) + last_month_remainder
        remainder = total_income_with_remainder - float(total_expenses)

        # Find the last day of the month
        import calendar
        last_day_of_month = date(year, month, calendar.monthrange(year, month)[1])

        cursor.execute("""
            INSERT INTO totals_remainders_m (user_id, date, total_income, total_expenses, remainder, last_month_remainder)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_income = VALUES(total_income),
                total_expenses = VALUES(total_expenses),
                remainder = VALUES(remainder),
                last_month_remainder = VALUES(last_month_remainder)
        """, (user_id, last_day_of_month, total_income_with_remainder, total_expenses, remainder, last_month_remainder))

    conn.commit()
    conn.close()

@app.route('/save_totals_remainders_d', methods=['POST'])
@login_required
def save_totals_remainders_d():
    try:
        data = request.get_json(silent=True) or {}
        start_date_str = data.get('start_date')
        user_id = current_user.id

        # Fetch goofy_week_mode for the current user
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT goofy_week_mode FROM users WHERE id = %s", (user_id,))
        goofy_week_mode = bool(cursor.fetchone()[0])
        conn.close()

        # Determine the starting date for incremental update
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except Exception:
                return jsonify({"status": "error", "message": "Invalid start_date format"}), 400
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(date) FROM totals_remainders_d WHERE user_id = %s", (user_id,))
            min_date_row = cursor.fetchone()
            start_date = min_date_row[0] if min_date_row and min_date_row[0] else date.today()
            conn.close()

        date_to_remainder = {}

        # Run daily, weekly, and monthly updates
        update_daily_totals(user_id, start_date, goofy_week_mode, date_to_remainder)
        update_daily_savings_for_savings_category(user_id, start_date)
        update_weekly_totals(user_id, start_date, goofy_week_mode, date_to_remainder)
        update_monthly_totals(user_id, start_date, date_to_remainder)

        # Prepare the response: for each date, fetch last_week_remainder from totals_remainders table
        conn = get_db_connection()
        cursor = conn.cursor(buffered=True)
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

        conn.close()
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
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT goofy_week_mode FROM users WHERE id = %s", (user_id,))
        goofy_week_mode = bool(cursor.fetchone()[0])
        conn.close()

        # Determine the starting date for incremental update
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except Exception:
                return jsonify({"status": "error", "message": "Invalid start_date format"}), 400
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MIN(date) FROM c_a_balances_d
                WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
            """, (user_id,))
            min_date_row = cursor.fetchone()
            start_date = min_date_row[0] if min_date_row and min_date_row[0] else date.today()
            conn.close()

        # Update CA balances (daily, weekly, monthly)
        update_daily_ca_totals(user_id, start_date)
        update_weekly_ca_totals(user_id, start_date, goofy_week_mode)  # <-- Pass goofy_week_mode here
        update_monthly_ca_totals(user_id, start_date)

        # Fetch updated daily CA balances
        conn = get_db_connection()
        cursor = conn.cursor(buffered=True)
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

        conn.close()
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

@app.route('/dashboard-d/get_categories', methods=['GET'])
@login_required
def get_dashboard_d_categories():
    entry_type = request.args.get('type')  # 'income' or 'expense'

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    if entry_type == 'income':
        cursor.execute("""
            SELECT id, name, is_auto_adjustment FROM income_categories
            WHERE user_id = %s AND name != 'Starting Balance'
            ORDER BY display_order DESC
        """, (current_user.id,))
    elif entry_type == 'expense':
        cursor.execute("""
            SELECT id, name, is_auto_adjustment FROM expense_categories
            WHERE user_id = %s
            ORDER BY display_order DESC
        """, (current_user.id,))
    else:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

    categories = cursor.fetchall()
    conn.close()

    return jsonify({'status': 'success', 'categories': categories}), 200



@app.route('/dashboard-d/add_entry', methods=['POST'])
@login_required
def dashboard_d_add_entry():
    data = request.get_json()
    entry_type = data.get('entryType')
    category_id = data.get('category')
    amount = data.get('amount')
    entry_date = data.get('date')

    if not all([entry_type, category_id, amount, entry_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
        conn.close()
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
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
    else:
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
        cat_row = cursor.fetchone()
        if not cat_row:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400

    # Check if an entry already exists for the given category and date
    cursor.execute(f"SELECT id, amount FROM {table_name} WHERE category_id = %s AND date = %s", (category_id, entry_date))
    existing_entry = cursor.fetchone()

    if existing_entry:
        new_amount = existing_entry['amount'] + Decimal(amount)
        cursor.execute(f"UPDATE {table_name} SET amount = %s WHERE id = %s", (new_amount, existing_entry['id']))
    else:
        cursor.execute(f"INSERT INTO {table_name} (category_id, date, amount) VALUES (%s, %s, %s)", (category_id, entry_date, amount))

    # If this is an expense category and is_credit_account=1, add payment record to c_payment_entries and run save_ca_daily_balance()
    if entry_type == 'expense' and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date = %s
            """, (account_id, entry_date))
            cursor.execute("""
                INSERT INTO c_payment_entries (account_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (account_id, entry_date, amount))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

    if entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()

    return jsonify({"status": "success"})



@app.route('/dashboard-d/get_totals_for_day', methods=['GET'])
@login_required
def get_totals_for_day():
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify({'status': 'error', 'message': 'No date provided.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
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

        conn.close()

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

        conn = get_db_connection()
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
        conn.close()

        return jsonify({'status': 'success', 'remainder': remainder})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/update-processed-status-d-entry', methods=['POST'])
@login_required
def update_processed_status_d_entry():
    data = request.json
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income', 'expense', or 'ca'
    entry_date = data.get('entry_date')
    processed = data.get('processed')

    if not category_id or not entry_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if category_type == 'income':
            # Update income entries
            cursor.execute("""
                UPDATE income_entries
                SET processed = %s
                WHERE category_id = %s AND date = %s
            """, (processed, category_id, entry_date))
        elif category_type == 'expense':
            # Update expense entries
            cursor.execute("""
                UPDATE expense_entries
                SET processed = %s
                WHERE category_id = %s AND date = %s
            """, (processed, category_id, entry_date))
        elif category_type == 'ca':
            # Update credit account expense entries
            cursor.execute("""
                UPDATE c_expense_entries
                SET processed = %s
                WHERE category_id = %s AND date = %s
            """, (processed, category_id, entry_date))
        else:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        conn.commit()
        conn.close()

        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Failed to update processed status'}), 500

@app.route('/get_dashboard_d_data')
@login_required
def get_dashboard_d_data():
    user_id = current_user.id
    date = request.args.get('date')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch daily totals/remainders for user (all dates)
    cursor.execute("""
        SELECT *
        FROM totals_remainders_d
        WHERE user_id = %s
        ORDER BY date ASC
    """, (user_id,))
    totals_remainders_d = cursor.fetchall()

    # Fetch daily CA balances for user's credit accounts
    cursor.execute("""
        SELECT *
        FROM c_a_balances_d
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY account_id ASC, date ASC
    """, (user_id,))
    c_a_balances_d = cursor.fetchall()

    # Fetch all savings entries for the user
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (user_id,))
    savings_entries = cursor.fetchall()

    # Fetch all income entries for the user
    cursor.execute("""
        SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s
        ORDER BY ic.display_order DESC, ie.date ASC
    """, (user_id,))
    income_entries = cursor.fetchall()

    # Fetch all expense entries for the user
    cursor.execute("""
        SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s
        ORDER BY ec.display_order DESC, ee.date ASC
    """, (user_id,))
    expense_entries = cursor.fetchall()

    # Fetch all c_expense_entries for the user's credit accounts
    cursor.execute("""
        SELECT cee.*, cec.name AS category_name
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
        ORDER BY cee.date DESC, cee.id ASC
    """, (user_id,))
    c_expense_entries = cursor.fetchall()

    conn.close()

    return jsonify({
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries,
        "income_entries": income_entries,
        "expense_entries": expense_entries,
        "c_expense_entries": c_expense_entries
    })

################################## Dashboard #############################################
@app.route('/delete_income_category', methods=['POST'])
@login_required
def delete_income_category():
    category_id = request.form['id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Find the Auto Adjustments category for this user
        cursor.execute("""
            SELECT id FROM income_categories
            WHERE user_id = %s AND name = 'Auto Adjustments'
            LIMIT 1
        """, (current_user.id,))
        auto_adj_row = cursor.fetchone()
        if not auto_adj_row:
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
        return jsonify({'status': 'success'})

    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})

    finally:
        conn.close()

@app.route('/delete_expense_category', methods=['POST'])
@login_required
def delete_expense_category():
    category_id = request.form['id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Find the Auto Adjustments expense category for this user
        cursor.execute("""
            SELECT id FROM expense_categories
            WHERE user_id = %s AND name = 'Auto Adjustments'
            LIMIT 1
        """, (current_user.id,))
        auto_adj_row = cursor.fetchone()
        if not auto_adj_row:
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
        return jsonify({'status': 'success'})

    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})

    finally:
        conn.close()
        
@app.route('/delete_ca_category', methods=['POST'])
@login_required
def delete_ca_category():
    category_id = request.form['id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Find the account_id for this CA category
        cursor.execute("""
            SELECT account_id FROM c_expense_categories
            WHERE id = %s
        """, (category_id,))
        row = cursor.fetchone()
        if not row:
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
        return jsonify({'status': 'success'})

    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})

    finally:
        cursor.close()
        conn.close()

@app.route('/add_income_category', methods=['POST'])
@login_required
def add_income_category():
    category_name = request.form['name']
    conn = get_db_connection()
    cursor = conn.cursor()

    # Find the current max display order for income categories
    cursor.execute("SELECT COALESCE(MAX(display_order), 0) FROM income_categories WHERE user_id = %s", (current_user.id,))
    max_order = cursor.fetchone()[0]

    # Insert the new category with the next display order
    cursor.execute("INSERT INTO income_categories (user_id, name, display_order) VALUES (%s, %s, %s)", (current_user.id, category_name, max_order + 1))
    new_category_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': category_name})

@app.route('/add_expense_category', methods=['POST'])
@login_required
def add_expense_category():
    category_name = request.form['name']
    conn = get_db_connection()
    cursor = conn.cursor()

    # Find the current max display order for expense categories
    cursor.execute("SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s", (current_user.id,))
    max_order = cursor.fetchone()[0]

    # Insert the new category with the next display order
    cursor.execute("INSERT INTO expense_categories (user_id, name, display_order) VALUES (%s, %s, %s)", (current_user.id, category_name, max_order + 1))
    new_category_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': category_name})

@app.route('/add_ca_category', methods=['POST'])
@login_required
def add_ca_category():
    name = request.form.get('name')
    account_id = request.form.get('account_id')
    if not name or not account_id:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
    conn.close()
    return jsonify({'status': 'success', 'new_category_id': new_category_id, 'category_name': name})


@app.route('/update_income_category', methods=['POST'])
@login_required
def update_income_category():
    category_id = request.form['category_id']  # Use the category_id
    new_name = request.form['new_name']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE income_categories
        SET name = %s
        WHERE id = %s AND user_id = %s
    """, (new_name, category_id, current_user.id))

    conn.commit()
    conn.close()

    return jsonify({'status': 'success'})


@app.route('/update_expense_category', methods=['POST'])
@login_required
def update_expense_category():
    category_id = request.form['category_id']  # Use the category_id
    new_name = request.form['new_name']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE expense_categories
        SET name = %s
        WHERE id = %s AND user_id = %s
    """, (new_name, category_id, current_user.id))

    conn.commit()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/update_ca_category', methods=['POST'])
@login_required
def update_ca_category():
    category_id = request.form['category_id']
    new_name = request.form['new_name']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE c_expense_categories
        SET name = %s
        WHERE id = %s
    """, (new_name, category_id))

    conn.commit()
    conn.close()

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
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
        FROM users
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

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
    cursor.execute("""
        SELECT category_id, date, amount, processed
        FROM income_entries
        WHERE category_id IN (SELECT id FROM income_categories WHERE user_id = %s)
    """, (current_user.id,))
    raw_income_entries = cursor.fetchall()

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
    cursor.execute("""
        SELECT category_id, date, amount, processed
        FROM expense_entries
        WHERE category_id IN (SELECT id FROM expense_categories WHERE user_id = %s)
    """, (current_user.id,))
    raw_expense_entries = cursor.fetchall()

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
    cursor.execute("""
        SELECT cee.category_id, cee.date, cee.amount, cee.processed
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
    """, (current_user.id,))
    raw_c_expense_entries = cursor.fetchall()

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
    cursor.execute("""
        SELECT date, total_income, total_expenses, remainder, last_week_remainder
        FROM totals_remainders
        WHERE user_id = %s
    """, (current_user.id,))
    totals_remainders = cursor.fetchall()

    # Fetch all savings entries for the user
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    savings_entries = cursor.fetchall()

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

    cursor.execute("""
        SELECT * FROM c_a_balances
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY date DESC
    """, (current_user.id,))
    c_a_balances = cursor.fetchall()

    conn.close()

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
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch total_income from totals_remainders table
        cursor.execute("""
            SELECT total_income
            FROM totals_remainders
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        
        result = cursor.fetchone()
        total_income = result[0] if result else 0  # Safely handle the case where no result is found
        
        conn.close()
        
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
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch total_expenses from totals_remainders table
        cursor.execute("""
            SELECT total_expenses
            FROM totals_remainders
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        
        result = cursor.fetchone()
        total_expenses = result[0] if result else 0  # Safely handle the case where no result is found
        
        conn.close()
        
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT balance
        FROM c_a_balances
        WHERE account_id = %s AND date = %s
        LIMIT 1
    """, (account_id, date))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    balance = result['balance'] if result and result['balance'] is not None else 0
    return jsonify({'status': 'success', 'balance': balance})

@app.route('/get_last_remainder', methods=['GET'])
@login_required
def get_last_remainder():
    date = request.args.get('date')

    if not date:
        return jsonify({"status": "error", "message": "Date parameter is missing"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Query to get the last week's remainder
        cursor.execute("""
            SELECT last_week_remainder
            FROM totals_remainders
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        last_remainder_data = cursor.fetchone()

        conn.close()

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
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Query to get the remainder for the specified date
        cursor.execute("""
            SELECT remainder
            FROM totals_remainders
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        remainder_data = cursor.fetchone()

        conn.close()

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

    conn = get_db_connection()
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
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/update_expense_order', methods=['POST'])
@login_required
def update_expense_order():
    order_data = request.json['order']
    user_id = current_user.id

    conn = get_db_connection()
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
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/update_ca_order', methods=['POST'])
@login_required
def update_ca_order():
    order_data = request.json['order']
    account_id = request.json.get('account_id')

    if not account_id or not order_data:
        return jsonify({'status': 'error', 'message': 'Missing account_id or order data'}), 400

    conn = get_db_connection()
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
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/update-entry', methods=['POST'])
@login_required
def update_entry():
    data = request.json
    category_id = data.get('category_id')
    date = data.get('date')
    amount = data.get('amount')
    entry_type = data.get('type')

    if not category_id or not date or amount is None:
        return jsonify({"status": "error", "message": "Missing required parameters"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
        conn.close()
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
            conn.close()
            return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400
    else:
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
        cat_row = cursor.fetchone()
        if not cat_row:
            conn.close()
            return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400

    # Check if an entry already exists for the given category and date
    cursor.execute(f"SELECT id FROM {table_name} WHERE category_id = %s AND date = %s", (category_id, date))
    existing_entry = cursor.fetchone()

    if existing_entry:
        cursor.execute(f"UPDATE {table_name} SET amount = %s WHERE id = %s", (amount, existing_entry['id']))
    else:
        cursor.execute(f"INSERT INTO {table_name} (category_id, date, amount) VALUES (%s, %s, %s)", (category_id, date, amount))

    # If this is an expense category and is_credit_account=1, add payment record to c_payment_entries and run save_ca_daily_balance()
    if entry_type == 'expense' and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            # Remove any previous payment entries for this account and date
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date = %s
            """, (account_id, date))
            # Add payment record to c_payment_entries with the new value only
            cursor.execute("""
                INSERT INTO c_payment_entries (account_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (account_id, date, amount))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
        conn.close()
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
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        cursor.execute(
            f"DELETE FROM {table_name} WHERE category_id = %s AND date BETWEEN %s AND %s",
            (category_id, start_date, end_date)
        )
    else:
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
        cat_row = cursor.fetchone()
        if not cat_row:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
        cursor.execute(
            f"DELETE FROM {table_name} WHERE category_id = %s AND date BETWEEN %s AND %s",
            (category_id, start_date, end_date)
        )

    # If this is an expense category and is_credit_account=1, delete payment in c_payment_entries
    if entry_type == 'expense' and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            # Delete payment entry from c_payment_entries for this account and date range
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
            """, (account_id, start_date, end_date))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

    if entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()

    return jsonify({'status': 'success'})

@app.route('/check_and_initialize_totals', methods=['POST'])
@login_required
def check_and_initialize_totals():
    try:
        data = request.get_json()
        fridays = data.get('fridays', [])

        if not fridays:
            return jsonify({"status": "error", "message": "No Fridays provided"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Iterate over each provided Friday
        for friday in fridays:
            friday_date = friday['date']

            # Check if a record for this Friday already exists in totals_remainders
            cursor.execute("""
                SELECT COUNT(*) FROM totals_remainders
                WHERE user_id = %s AND date = %s
            """, (current_user.id, friday_date))
            record_exists = cursor.fetchone()[0]

            # If no record exists, create it with default values (0 for totals)
            if record_exists == 0:
                cursor.execute("""
                    INSERT INTO totals_remainders (user_id, date, total_income, total_expenses, remainder, last_week_remainder)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (current_user.id, friday_date, 0.00, 0.00, 0.00, 0.00))

        # Commit the changes to the database
        conn.commit()
        conn.close()

        return jsonify({"status": "success", "message": "Missing records initialized."})
    except mysql.connector.Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/update-processed-status-week-range', methods=['POST'])
@login_required
def update_processed_status_week_range():
    data = request.get_json()
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income', 'expense', or 'ca'
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    processed = data.get('processed')

    if not category_id or not category_type or not start_date or not end_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Determine the appropriate table and category table based on category_type
        if category_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
            user_field = 'user_id'
            user_value = current_user.id
        elif category_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
            user_field = 'user_id'
            user_value = current_user.id
        elif category_type == 'ca':
            table_name = 'c_expense_entries'
            category_table = 'c_expense_categories'
            user_field = 'ca.user_id'
            user_value = current_user.id
        else:
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        # SQL Query to update all entries within the date range
        if category_type == 'ca':
            # For CA, join through credit_accounts to get user_id
            query = f"""
                UPDATE {table_name} AS entries
                JOIN {category_table} AS categories ON entries.category_id = categories.id
                JOIN credit_accounts ca ON categories.account_id = ca.id
                SET entries.processed = %s
                WHERE ca.user_id = %s AND entries.category_id = %s AND entries.date BETWEEN %s AND %s
            """
            cursor.execute(query, (processed, user_value, category_id, start_date, end_date))
        else:
            query = f"""
                UPDATE {table_name} AS entries
                JOIN {category_table} AS categories ON entries.category_id = categories.id
                SET entries.processed = %s
                WHERE categories.user_id = %s AND entries.category_id = %s AND entries.date BETWEEN %s AND %s
            """
            cursor.execute(query, (processed, user_value, category_id, start_date, end_date))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/hide_income_category', methods=['POST'])
@login_required
def hide_income_category():
    category_id = request.form.get('category_id')
    hidden = request.form.get('hidden', 1)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE income_categories SET hidden = %s WHERE id = %s AND user_id = %s", (hidden, category_id, current_user.id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/hide_expense_category', methods=['POST'])
@login_required
def hide_expense_category():
    category_id = request.form.get('category_id')
    hidden = request.form.get('hidden', 1)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE expense_categories SET hidden = %s WHERE id = %s AND user_id = %s", (hidden, category_id, current_user.id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/hide_ca_category', methods=['POST'])
@login_required
def hide_ca_category():
    category_id = request.form['category_id']
    hidden = int(request.form['hidden'])
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE c_expense_categories SET hidden = %s WHERE id = %s",
        (hidden, category_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


################################## Dashboard 3 Month ##########################################

@app.route('/dashboard_3m')
@login_required
def dashboard_3m():
    now = datetime.now()
    fridays_by_month = {}

    # Fetch user data
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
        FROM users
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()
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
    cursor.execute("""
        SELECT category_id, date, amount, processed
        FROM income_entries
        WHERE category_id IN (SELECT id FROM income_categories WHERE user_id = %s)
    """, (current_user.id,))
    raw_income_entries = cursor.fetchall()
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
    cursor.execute("""
        SELECT category_id, date, amount, processed
        FROM expense_entries
        WHERE category_id IN (SELECT id FROM expense_categories WHERE user_id = %s)
    """, (current_user.id,))
    raw_expense_entries = cursor.fetchall()
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
    cursor.execute("""
        SELECT cee.category_id, cee.date, cee.amount, cee.processed
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
    """, (current_user.id,))
    raw_c_expense_entries = cursor.fetchall()
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
    cursor.execute("""
        SELECT date, total_income, total_expenses, remainder, last_month_remainder
        FROM totals_remainders_m
        WHERE user_id = %s
    """, (current_user.id,))
    totals_remainders = cursor.fetchall()

    # Fetch all savings entries for the user
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    savings_entries = cursor.fetchall()

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

    cursor.execute("""
        SELECT * FROM c_a_balances_m
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY date DESC
    """, (current_user.id,))
    c_a_balances_m = cursor.fetchall()

    conn.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = user_data['balance_threshold'] if user_data else 0
    member_since = user_data['member_since'] if user_data else None
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard'

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT balance
        FROM c_a_balances_m
        WHERE account_id = %s AND date = %s
        LIMIT 1
    """, (account_id, date))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    balance = result['balance'] if result and result['balance'] is not None else 0
    return jsonify({'status': 'success', 'balance': balance})

@app.route('/get_total_income_3m', methods=['GET'])
@login_required
def get_total_income_3m():
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': 'Missing date parameter'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch total_income from totals_remainders_m table
        cursor.execute("""
            SELECT total_income
            FROM totals_remainders_m
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        
        result = cursor.fetchone()
        total_income = result[0] if result else 0
        
        conn.close()
        
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
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch total_expenses from totals_remainders_m table
        cursor.execute("""
            SELECT total_expenses
            FROM totals_remainders_m
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        
        result = cursor.fetchone()
        total_expenses = result[0] if result else 0
        
        conn.close()
        
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
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Query to get the last month's remainder
        cursor.execute("""
            SELECT last_month_remainder
            FROM totals_remainders_m
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        last_remainder_data = cursor.fetchone()

        conn.close()

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
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Query to get the remainder for the specified date
        cursor.execute("""
            SELECT remainder
            FROM totals_remainders_m
            WHERE user_id = %s AND date = %s
        """, (current_user.id, date))
        remainder_data = cursor.fetchone()

        conn.close()

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
    data = request.get_json()
    category_id = data.get('category_id')
    category_type = data.get('category_type')  # 'income' or 'expense'
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    processed = data.get('processed')

    if not category_id or not category_type or not start_date or not end_date or processed is None:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Determine the appropriate table and category table based on category_type
        if category_type == 'income':
            table_name = 'income_entries'
            category_table = 'income_categories'
        elif category_type == 'expense':
            table_name = 'expense_entries'
            category_table = 'expense_categories'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid category type'}), 400

        # SQL Query to update all entries within the date range
        query = f"""
            UPDATE {table_name} AS entries
            JOIN {category_table} AS categories ON entries.category_id = categories.id
            SET entries.processed = %s
            WHERE categories.user_id = %s AND entries.category_id = %s AND entries.date BETWEEN %s AND %s
        """

        # Execute the query
        cursor.execute(query, (processed, current_user.id, category_id, start_date, end_date))

        # Commit the transaction
        conn.commit()

        cursor.close()
        conn.close()

        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

################################## Dashboard Month ############################################

@app.route('/dashboard_m')
@login_required
def dashboard_m():
    selected_date = request.args.get('date')
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
        FROM users
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False
    balance_threshold = float(user_data.get('balance_threshold', 0)) if user_data else 0
    member_since = user_data['member_since'] if user_data else None
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard'

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
    cursor.execute("""
        SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s
        ORDER BY ic.display_order DESC, ie.date ASC
    """, (current_user.id,))
    income_entries = cursor.fetchall()

    cursor.execute("""
        SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s
        ORDER BY ec.display_order DESC, ee.date ASC
    """, (current_user.id,))
    expense_entries = cursor.fetchall()

    # Totals/remainders
    cursor.execute("""
        SELECT * FROM totals_remainders_d
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    totals_remainders_d = cursor.fetchall()

    # Savings
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    savings_entries = cursor.fetchall()

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
    cursor.execute("""
        SELECT cee.*, cec.name AS category_name
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
        ORDER BY cee.date DESC, cee.id ASC
    """, (current_user.id,))
    c_expense_entries = cursor.fetchall()

    # CA balances (daily)
    cursor.execute("""
        SELECT * FROM c_a_balances_d
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY account_id ASC, date ASC
    """, (current_user.id,))
    c_a_balances_d = cursor.fetchall()

    conn.close()

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
        from_date = datetime.date(year, month_num, 1)
        to_date = datetime.date(year, month_num, monthrange(year, month_num)[1])

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Daily totals/remainders for the range
    cursor.execute("""
        SELECT * FROM totals_remainders_d
        WHERE user_id = %s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (user_id, from_date, to_date))
    totals_remainders_d = cursor.fetchall()

    # Credit account daily balances for the range
    cursor.execute("""
        SELECT * FROM c_a_balances_d
        WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
        AND date BETWEEN %s AND %s
        ORDER BY account_id ASC, date ASC
    """, (user_id, from_date, to_date))
    c_a_balances_d = cursor.fetchall()

    # Savings entries for the range
    cursor.execute("""
        SELECT * FROM savings_entries
        WHERE user_id = %s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (user_id, from_date, to_date))
    savings_entries = cursor.fetchall()

    conn.close()

    return {
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries
    }

################################## Dashboard Year ############################################

@app.route('/dashboard_y')
@login_required
def dashboard_y():
    selected_date = request.args.get('date')
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, member_since, currency_type, landing_page
        FROM users
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    goofy_week_mode = bool(user_data.get('goofy_week_mode', False)) if user_data else False
    balance_threshold = float(user_data.get('balance_threshold', 0)) if user_data else 0
    member_since = user_data['member_since'] if user_data else None
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'
    landing_page = user_data['landing_page'] if user_data and 'landing_page' in user_data else 'dashboard'

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
    cursor.execute("""
        SELECT ie.id, ie.date, ie.amount, ie.processed, ic.id AS category_id, ic.name AS category_name, ic.display_order
        FROM income_entries ie
        JOIN income_categories ic ON ie.category_id = ic.id
        WHERE ic.user_id = %s
        ORDER BY ic.display_order DESC, ie.date ASC
    """, (current_user.id,))
    income_entries = cursor.fetchall()

    cursor.execute("""
        SELECT ee.id, ee.date, ee.amount, ee.processed, ec.id AS category_id, ec.name AS category_name, ec.display_order
        FROM expense_entries ee
        JOIN expense_categories ec ON ee.category_id = ec.id
        WHERE ec.user_id = %s
        ORDER BY ec.display_order DESC, ee.date ASC
    """, (current_user.id,))
    expense_entries = cursor.fetchall()

    # Totals/remainders
    cursor.execute("""
        SELECT * FROM totals_remainders_d
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    totals_remainders_d = cursor.fetchall()

    # Savings
    cursor.execute("""
        SELECT date, amount FROM savings_entries
        WHERE user_id = %s
        ORDER BY date ASC
    """, (current_user.id,))
    savings_entries = cursor.fetchall()

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
    cursor.execute("""
        SELECT cee.*, cec.name AS category_name
        FROM c_expense_entries cee
        JOIN c_expense_categories cec ON cee.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE ca.user_id = %s
        ORDER BY cee.date DESC, cee.id ASC
    """, (current_user.id,))
    c_expense_entries = cursor.fetchall()

    # CA balances (daily)
    cursor.execute("""
        SELECT * FROM c_a_balances_d
        WHERE account_id IN (
            SELECT id FROM credit_accounts WHERE user_id = %s
        )
        ORDER BY account_id ASC, date ASC
    """, (current_user.id,))
    c_a_balances_d = cursor.fetchall()

    conn.close()

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
    first_day = f"{year}-01-01"
    last_day = f"{year}-12-31"

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT * FROM totals_remainders_d
        WHERE user_id = %s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (user_id, first_day, last_day))
    totals_remainders_d = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM c_a_balances_d
        WHERE account_id IN (SELECT id FROM credit_accounts WHERE user_id = %s)
        AND date BETWEEN %s AND %s
        ORDER BY account_id ASC, date ASC
    """, (user_id, first_day, last_day))
    c_a_balances_d = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM savings_entries
        WHERE user_id = %s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (user_id, first_day, last_day))
    savings_entries = cursor.fetchall()

    conn.close()

    return {
        "status": "success",
        "totals_remainders_d": totals_remainders_d,
        "c_a_balances_d": c_a_balances_d,
        "savings_entries": savings_entries
    }

################################## Profile #############################################
@app.route('/profile', methods=['GET'])
@login_required
def profile():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch profile picture, first name, last name, balance threshold, goofy_week_mode, landing_page, currency_type, and mfa_secret
    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, landing_page, currency_type, mfa_secret
        FROM users 
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

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

    conn.close()

    # Extract values from the query result
    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = int(user_data['balance_threshold']) if user_data and user_data['balance_threshold'] is not None else 0
    goofy_week_mode = user_data['goofy_week_mode'] if user_data else 0
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard'
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
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch profile picture, first name, last name, balance threshold, goofy_week_mode, landing_page, currency_type, and mfa_secret
    cursor.execute("""
        SELECT profile_picture, first_name, last_name, balance_threshold, goofy_week_mode, landing_page, currency_type, mfa_secret
        FROM users 
        WHERE id = %s
    """, (current_user.id,))
    user_data = cursor.fetchone()

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

    conn.close()

    # Extract values from the query result
    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    balance_threshold = int(user_data['balance_threshold']) if user_data and user_data['balance_threshold'] is not None else 0
    goofy_week_mode = user_data['goofy_week_mode'] if user_data else 0
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard'
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
    
    conn = get_db_connection()  # Get the database connection
    cursor = conn.cursor(dictionary=True)  # Create a cursor with dictionary=True for dict-like results

    try:
        # Update the goofy_week_mode column for the current user
        query = "UPDATE users SET goofy_week_mode = %s WHERE id = %s"
        cursor.execute(query, (goofy_week_mode, current_user.id))
        
        # Commit the changes
        conn.commit()
        
        return jsonify({'status': 'success'})
    except Exception as e:
        # Rollback in case of error
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        # Close the cursor and connection
        cursor.close()
        conn.close()

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

        # Get the current user's profile picture
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT profile_picture FROM users WHERE id = %s", (current_user.id,))
        user_data = cursor.fetchone()
        conn.close()

        # Delete the old profile picture file if it exists
        if user_data and user_data['profile_picture']:
            old_filepath = os.path.join(app.config['UPLOAD_FOLDER'], user_data['profile_picture'])
            if os.path.exists(old_filepath):
                os.remove(old_filepath)

        # Save the new profile picture file
        file.save(filepath)

        # Update the user's profile picture in the database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users 
            SET profile_picture = %s 
            WHERE id = %s
        """, (filename, current_user.id))
        conn.commit()
        conn.close()

        flash('Profile picture updated successfully.')
        return redirect(url_for('profile'))
    else:
        flash('Invalid file type')
        return redirect(url_for('profile'))

@app.route('/update_first_name', methods=['POST'])
@login_required
def update_first_name():
    new_first_name = request.form['first_name']
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET first_name = %s 
        WHERE id = %s
    """, (new_first_name, current_user.id))
    conn.commit()
    conn.close()

    flash('First name updated successfully.')
    return redirect(url_for('profile', success='first_name'))

@app.route('/update_last_name', methods=['POST'])
@login_required
def update_last_name():
    new_last_name = request.form['last_name']
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET last_name = %s 
        WHERE id = %s
    """, (new_last_name, current_user.id))
    conn.commit()
    conn.close()

    flash('Last name updated successfully.')
    return redirect(url_for('profile', success='last_name'))

@app.route('/update_username', methods=['POST'])
@login_required
def update_username():
    new_username = request.form['username']

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET username = %s 
        WHERE id = %s
    """, (new_username, current_user.id))
    conn.commit()
    conn.close()

    flash('Username updated successfully.')
    return redirect(url_for('profile', success='email'))

@app.route('/update_password', methods=['POST'])
@login_required
def update_password():
    new_password = request.form['password']

    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET password = %s 
        WHERE id = %s
    """, (hashed_password, current_user.id))
    conn.commit()
    conn.close()

    flash('Password updated successfully.')
    return redirect(url_for('profile', success='password'))

@app.route('/enable_mfa', methods=['POST'])
@login_required
def enable_mfa():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Generate a new secret
        secret = pyotp.random_base32()
        # Save it to the user
        cursor.execute("UPDATE users SET mfa_secret = %s WHERE id = %s", (secret, current_user.id))
        conn.commit()

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
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()
    
@app.route('/verify_mfa', methods=['POST'])
@login_required
def verify_mfa():
    code = request.form.get('code')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT mfa_secret FROM users WHERE id = %s", (current_user.id,))
    result = cursor.fetchone()
    conn.close()
    if not result or not result[0]:
        return jsonify({'status': 'error', 'message': 'No MFA secret set'}), 400
    secret = result[0]
    totp = pyotp.TOTP(secret)
    if totp.verify(code):
        # Optionally set a session flag for MFA
        return jsonify({'status': 'success'})
    else:
        return jsonify({'status': 'error', 'message': 'Invalid code'}), 400

@app.route('/disable_mfa', methods=['POST'])
@login_required
def disable_mfa():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET mfa_secret = NULL WHERE id = %s", (current_user.id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/update_starting_balance', methods=['POST'])
@login_required
def update_starting_balance():
    # Retrieve the new starting balance from the form
    new_balance = request.form.get('starting_balance')

    if new_balance:
        conn = get_db_connection()
        cursor = conn.cursor()

        # First, find the 'Starting Balance' category for the current user
        cursor.execute("""
            SELECT id FROM income_categories 
            WHERE name = 'Starting Balance' AND user_id = %s LIMIT 1
        """, (current_user.id,))
        
        result = cursor.fetchone()

        if result:
            STARTING_BALANCE_CATEGORY_ID = result[0]  # Access the first element of the tuple

            # Check if there is already an entry in the income_entries table for this category
            cursor.execute("""
                SELECT id FROM income_entries 
                WHERE category_id = %s LIMIT 1
            """, (STARTING_BALANCE_CATEGORY_ID,))
            
            entry_result = cursor.fetchone()

            if entry_result:
                # Update the existing starting balance entry, only the amount, not the date
                cursor.execute("""
                    UPDATE income_entries
                    SET amount = %s  -- Use the correct column name here for the amount
                    WHERE id = %s
                """, (new_balance, entry_result[0]))  # Access the first element of the tuple
            else:
                # Insert a new starting balance entry if it doesn't exist
                cursor.execute("""
                    INSERT INTO income_entries (category_id, amount, date)  -- Use the correct column name here
                    VALUES (%s, %s, CURDATE())
                """, (STARTING_BALANCE_CATEGORY_ID, new_balance))

            # Commit the transaction and close the connection
            conn.commit()

        conn.close()

    return redirect(url_for('profile'))

@app.route('/update_balance_threshold', methods=['POST'])
@login_required
def update_balance_threshold():
    # Retrieve the new balance threshold from the form
    new_threshold = request.form.get('balance_threshold')
    
    if new_threshold:
        conn = get_db_connection()  # Establish the database connection
        cursor = conn.cursor()  # Create the cursor
        
        # Update the user's balance threshold
        cursor.execute("""
            UPDATE users 
            SET balance_threshold = %s 
            WHERE id = %s
        """, (new_threshold, current_user.id))
        
        # Commit the transaction and close the connection
        conn.commit()
        conn.close()

    return redirect(url_for('profile'))



@app.route('/remove_profile_picture', methods=['POST'])
@login_required
def remove_profile_picture():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Fetch the current user's profile picture
    cursor.execute("SELECT profile_picture FROM users WHERE id = %s", (current_user.id,))
    user_data = cursor.fetchone()

    if user_data and user_data[0]:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], user_data[0])
        if os.path.exists(filepath) and user_data[0] != 'DefaultProfilePicture.svg':
            os.remove(filepath)  # Delete the file from the server

        # Update the database to set profile_picture to None or to the default picture
        cursor.execute("UPDATE users SET profile_picture = NULL WHERE id = %s", (current_user.id,))
        conn.commit()

    conn.close()
    return jsonify({'status': 'success'})  # Return a JSON response indicating success

@app.route('/delete_user/<username>', methods=['POST'])
@login_required
def delete_user(username):
    if username != current_user.username:
        flash('You can only delete your own account.')
        return jsonify({'status': 'error', 'message': 'You can only delete your own account.'})

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT profile_picture FROM users WHERE username = %s", (username,))
    user_data = cursor.fetchone()

    if user_data and user_data[0]:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], user_data[0])
        if os.path.exists(filepath) and user_data[0] != 'DefaultProfilePicture.svg':
            os.remove(filepath)

    cursor.execute("DELETE FROM users WHERE username = %s", (username,))
    conn.commit()
    conn.close()

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

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET currency_type = %s WHERE id = %s", (currency_type, current_user.id))
        conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()

############################# Recurring Income ############################
@app.route('/recurring-income')
@login_required
def recurring_income():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Fetch recurring income records, including no_end_date from income_categories
    cursor.execute("""
        SELECT ri.id, ri.category_id, ic.name as category_name, ri.amount, 
               ri.cadence_interval, ri.cadence_unit, ri.weekdays, ri.monthly_days, 
               ri.start_date, ri.end_date, ri.yearly_day, ri.yearly_month,
               ic.no_end_date
        FROM recurring_income ri
        JOIN income_categories ic ON ri.category_id = ic.id
        WHERE ri.user_id = %s
    """, (current_user.id,))

    recurring_income_records = cursor.fetchall()

    # Fetch user profile data (profile picture, first name, last name, landing_page, currency_type)
    cursor.execute("SELECT profile_picture, first_name, last_name, landing_page, currency_type FROM users WHERE id = %s", (current_user.id,))
    user_data = cursor.fetchone()
    conn.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    # Calculate December 31st, 5 years from now
    current_date = date.today()
    no_end_date = date(current_date.year + 5, 12, 31)

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
        # Get database connection
        conn = get_db_connection()
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

        # Step 3: Insert the recurring income record
        cursor.execute("""
            INSERT INTO recurring_income 
            (user_id, category_id, amount, cadence_interval, cadence_unit, weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            current_user.id, category_id, amount, cadence_interval, cadence_unit, 
            ','.join(weekdays) if weekdays else None, 
            ','.join(map(str, monthly_days)) if monthly_days else None,  # Store multiple monthly days as a comma-separated string
            yearly_day if yearly_day else None, 
            yearly_month if yearly_month else None, 
            start_date, 
            end_date
        ))

        # Get the ID of the newly inserted recurring income record
        recurring_id = cursor.lastrowid
        
        # Commit the transaction
        conn.commit()

        # Generate income entries based on the cadence
        generate_income_entries(
            recurring_id, category_id, amount, cadence_interval, cadence_unit, 
            start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
        )

        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring income added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring income: {str(e)}'}), 500

    finally:
        cursor.close()
        conn.close()


def generate_income_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            # Insert entry for the current date
            cursor.execute("""
                INSERT INTO income_entries (recurring_id, category_id, date, amount)
                VALUES (%s, %s, %s, %s)
            """, (recurring_id, category_id, current_date, amount))
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    cursor.execute("""
                        INSERT INTO income_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, weekday_date, amount))
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
                        cursor.execute("""
                            INSERT INTO income_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                        cursor.execute("""
                            INSERT INTO income_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                    cursor.execute("""
                        INSERT INTO income_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
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
                    cursor.execute("""
                        INSERT INTO income_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
                    year += interval

        # Increment current_date
        if delta:
            current_date += delta
        else:
            break

    conn.commit()
    cursor.close()
    conn.close()

@app.route('/delete-recurring-income', methods=['POST'])
@login_required
def delete_recurring_income():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get the category_id for this recurring income
        cursor.execute("""
            SELECT category_id FROM recurring_income WHERE id = %s AND user_id = %s
        """, (recurring_id, current_user.id))
        category = cursor.fetchone()
        if not category:
            return jsonify({'status': 'error', 'message': 'Recurring income not found.'}), 404
        category_id = category[0]

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

        # Delete all future entries and the recurring record/category
        cursor.execute("DELETE FROM income_entries WHERE recurring_id = %s", (recurring_id,))
        cursor.execute("DELETE FROM recurring_income WHERE id = %s AND user_id = %s", (recurring_id, current_user.id))
        cursor.execute("DELETE FROM income_categories WHERE id = %s AND user_id = %s", (category_id, current_user.id))

        conn.commit()
        return jsonify({'status': 'success', 'message': 'Recurring income and associated category deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring income.'}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/update-recurring-income', methods=['POST'])
@login_required
def update_recurring_income():
    data = request.get_json()
    return update_recurring_income_inner(data, current_user.id)

def update_recurring_income_inner(data, user_id):
    conn = None
    cursor = None
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

        # Connect to the database
        conn = get_db_connection()
        cursor = conn.cursor()

        # Step 1: Fetch the category_id from recurring_income
        cursor.execute("""
            SELECT category_id FROM recurring_income WHERE id = %s AND user_id = %s
        """, (recurring_id, user_id))
        result = cursor.fetchone()
        if not result:
            return jsonify({'status': 'error', 'message': 'Recurring income not found'}), 404
        category_id = result[0]

        # Step 2: Update the category name in the income_categories table
        cursor.execute("""
            UPDATE income_categories
            SET name = %s, is_recurring = 1, no_end_date = %s
            WHERE id = %s
        """, (category_name, no_end_date, category_id))

        # Convert the monthly_days array into a comma-separated string for storage, if it exists
        monthly_days_str = ','.join(map(str, monthly_days)) if monthly_days else None

        # Step 2: Update the recurring income record in the database
        cursor.execute("""
            UPDATE recurring_income
            SET category_id = %s, amount = %s, cadence_interval = %s, cadence_unit = %s, weekdays = %s, monthly_days = %s, yearly_day = %s, yearly_month = %s, start_date = %s, end_date = %s
            WHERE id = %s AND user_id = %s
        """, (category_id, amount, cadence_interval, cadence_unit,
              ','.join(weekdays) if weekdays else None,  # Store weekdays as a comma-separated string
              monthly_days_str,  # Store monthly days as a comma-separated string
              yearly_day if yearly_day else None,
              yearly_month if yearly_month else None,
              start_date, end_date, recurring_id, current_user.id))

        # Step 3: Delete old income entries for today and the future related to this recurring record
        cursor.execute("""
            DELETE FROM income_entries
            WHERE recurring_id = %s AND date >= %s
        """, (recurring_id, today))

        # Commit the deletion before generating new entries
        conn.commit()

        # Step 4: Recreate the income entries with the updated details (only for today and future)
        generate_income_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit,
                                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month)

        # Commit after generating new entries
        conn.commit()

        return jsonify({'status': 'success', 'message': 'Recurring income updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


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

################################# Recurring Expense ############################
@app.route('/recurring-expense')
@login_required
def recurring_expense():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Fetch recurring expense records, including no_end_date from expense_categories
    cursor.execute("""
        SELECT ri.id, ri.category_id, ic.name as category_name, ri.amount, 
               ri.cadence_interval, ri.cadence_unit, ri.weekdays, ri.monthly_days, 
               ri.start_date, ri.end_date, ri.yearly_day, ri.yearly_month,
               ic.no_end_date
        FROM recurring_expense ri
        JOIN expense_categories ic ON ri.category_id = ic.id
        WHERE ri.user_id = %s
    """, (current_user.id,))

    recurring_expense_records = cursor.fetchall()

    # Fetch user profile data (profile picture, first name, last name, landing_page, currency_type)
    cursor.execute("SELECT profile_picture, first_name, last_name, landing_page, currency_type FROM users WHERE id = %s", (current_user.id,))
    user_data = cursor.fetchone()
    conn.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    # Calculate December 31st, 5 years from now
    current_date = date.today()
    no_end_date = date(current_date.year + 5, 12, 31)

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
        # Get database connection
        conn = get_db_connection()
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

        # Step 3: Insert the recurring expense record
        cursor.execute("""
            INSERT INTO recurring_expense 
            (user_id, category_id, amount, cadence_interval, cadence_unit, weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            current_user.id, category_id, amount, cadence_interval, cadence_unit, 
            ','.join(weekdays) if weekdays else None, 
            ','.join(map(str, monthly_days)) if monthly_days else None,  # Store multiple monthly days as a comma-separated string
            yearly_day if yearly_day else None, 
            yearly_month if yearly_month else None, 
            start_date, 
            end_date
        ))

        # Get the ID of the newly inserted recurring expense record
        recurring_id = cursor.lastrowid
        
        # Commit the transaction
        conn.commit()

        # Generate expense entries based on the cadence
        generate_expense_entries(
            recurring_id, category_id, amount, cadence_interval, cadence_unit, 
            start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
        )

        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring expense added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring expense: {str(e)}'}), 500

    finally:
        cursor.close()
        conn.close()


def generate_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            # Insert entry for the current date
            cursor.execute("""
                INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                VALUES (%s, %s, %s, %s)
            """, (recurring_id, category_id, current_date, amount))
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    cursor.execute("""
                        INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, weekday_date, amount))
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
                        cursor.execute("""
                            INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                        cursor.execute("""
                            INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                year = current_date.year
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
                    cursor.execute("""
                        INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
                    year += interval
            else:
                interval = int(cadence_interval)
                year = current_date.year
                while True:
                    yearly_entry_date = date(year=year, month=1, day=1)
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    cursor.execute("""
                        INSERT INTO expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
                    year += interval

        # Increment current_date
        if delta:
            current_date += delta
        else:
            break

    conn.commit()
    cursor.close()
    conn.close()

@app.route('/delete-recurring-expense', methods=['POST'])
@login_required
def delete_recurring_expense():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get the category_id for this recurring expense
        cursor.execute("""
            SELECT category_id FROM recurring_expense WHERE id = %s AND user_id = %s
        """, (recurring_id, current_user.id))
        category = cursor.fetchone()
        if not category:
            return jsonify({'status': 'error', 'message': 'Recurring expense not found.'}), 404
        category_id = category[0]

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

        # Delete all future entries and the recurring record/category
        cursor.execute("DELETE FROM expense_entries WHERE recurring_id = %s", (recurring_id,))
        cursor.execute("DELETE FROM recurring_expense WHERE id = %s AND user_id = %s", (recurring_id, current_user.id))
        cursor.execute("DELETE FROM expense_categories WHERE id = %s AND user_id = %s", (category_id, current_user.id))

        conn.commit()
        return jsonify({'status': 'success', 'message': 'Recurring expense and associated category deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring expense.'}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/update-recurring-expense', methods=['POST'])
@login_required
def update_recurring_expense():
    data = request.get_json()
    return update_recurring_expense_inner(data, current_user.id)

def update_recurring_expense_inner(data, user_id):
    conn = None
    cursor = None
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

        # Connect to the database
        conn = get_db_connection()
        cursor = conn.cursor()

        # Step 1: Fetch the category_id from recurring_expense
        cursor.execute("""
            SELECT category_id FROM recurring_expense WHERE id = %s AND user_id = %s
        """, (recurring_id, user_id))
        result = cursor.fetchone()
        if not result:
            return jsonify({'status': 'error', 'message': 'Recurring expense not found'}), 404
        category_id = result[0]

        # Step 2: Update the category name in the expense_categories table
        cursor.execute("""
            UPDATE expense_categories
            SET name = %s, is_recurring = 1, no_end_date = %s
            WHERE id = %s
        """, (category_name, no_end_date, category_id))

        # Convert the monthly_days array into a comma-separated string for storage, if it exists
        monthly_days_str = ','.join(map(str, monthly_days)) if monthly_days else None

        # Step 2: Update the recurring expense record in the database
        cursor.execute("""
            UPDATE recurring_expense
            SET category_id = %s, amount = %s, cadence_interval = %s, cadence_unit = %s, weekdays = %s, monthly_days = %s, yearly_day = %s, yearly_month = %s, start_date = %s, end_date = %s
            WHERE id = %s AND user_id = %s
        """, (category_id, amount, cadence_interval, cadence_unit, 
              ','.join(weekdays) if weekdays else None,  # Store weekdays as a comma-separated string
              monthly_days_str,  # Store monthly days as a comma-separated string
              yearly_day if yearly_day else None, 
              yearly_month if yearly_month else None, 
              start_date, end_date, recurring_id, current_user.id))

        # Step 3: Delete old expense entries for today and the future related to this recurring record
        cursor.execute("""
            DELETE FROM expense_entries
            WHERE recurring_id = %s AND date >= %s
        """, (recurring_id, today))

        # Commit the deletion before generating new entries
        conn.commit()
        
        # Step 4: Recreate the expense entries with the updated details (only for today and future)
        generate_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, 
                                start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month)

        # Commit after generating new entries
        conn.commit()
        
        return jsonify({'status': 'success', 'message': 'Recurring expense updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

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

################################### Footer ###########################################

@app.route('/footer_add_entry', methods=['POST'])
@login_required
def footer_add_entry():
    data = request.get_json()
    entry_type = data.get('entryType')
    category_id = data.get('category')
    amount = data.get('amount')
    entry_date = data.get('date')

    if not all([entry_type, category_id, amount, entry_date]):
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    # Establish a database connection
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
        conn.close()
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
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400
    else:
        cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s AND user_id = %s", (category_id, current_user.id))
        cat_row = cursor.fetchone()
        if not cat_row:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid category_id for this entry type'}), 400

    # Check if an entry already exists for the given category and date
    cursor.execute(f"SELECT id, amount, processed FROM {table_name} WHERE category_id = %s AND date = %s", (category_id, entry_date))
    existing_entry = cursor.fetchone()

    if existing_entry:
        new_amount = existing_entry['amount'] + Decimal(amount)
        new_processed = existing_entry['processed'] + 1  # Increment the processed value by 1
        cursor.execute(f"UPDATE {table_name} SET amount = %s, processed = %s WHERE id = %s", (new_amount, new_processed, existing_entry['id']))
    else:
        cursor.execute(f"INSERT INTO {table_name} (category_id, date, amount, processed) VALUES (%s, %s, %s, %s)", (category_id, entry_date, amount, 1))

    # If this is an expense category and is_credit_account=1, add payment record to c_payment_entries and run save_ca_daily_balance()
    ca_triggered = False
    if entry_type == 'expense' and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date = %s
            """, (account_id, entry_date))
            cursor.execute("""
                INSERT INTO c_payment_entries (account_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (account_id, entry_date, amount))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

    # For CA, update balances
    if entry_type.startswith('ca_') or entry_type == 'ca' or ca_triggered:
        save_ca_daily_balance()

    return jsonify({"status": "success"})

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Optionally, check that the category_id exists in the correct category table
    cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s", (category_id,))
    cat_row = cursor.fetchone()
    if not cat_row:
        return jsonify({"status": "error", "message": "Invalid category_id for this entry type"}), 400

    # Delete all entries for this category between start_date and end_date
    cursor.execute(
        f"DELETE FROM {table_name} WHERE category_id = %s AND date BETWEEN %s AND %s",
        (category_id, start_date, end_date)
    )

    # Insert the new entry for the friday_date
    cursor.execute(
        f"INSERT INTO {table_name} (category_id, date, amount) VALUES (%s, %s, %s)",
        (category_id, friday_date, amount)
    )

    # If this is an expense category and is_credit_account=1, add payment record to c_payment_entries and run save_ca_daily_balance_inner
    ca_triggered = False
    if entry_type == 'expense' and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            # --- Remove any previous payment entries for this account and date range ---
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
            """, (account_id, start_date, end_date))
            # --- Add payment record to c_payment_entries with the new value only ---
            cursor.execute("""
                INSERT INTO c_payment_entries (account_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (account_id, friday_date, amount))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

    # If a CA payment was updated, trigger CA balance recalculation
    if ca_triggered:
        save_ca_daily_balance()

    return jsonify({"status": "success"})

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
        conn.close()
        return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

    # Optionally, check that the category_id exists in the correct category table
    cursor.execute(f"SELECT * FROM {category_table} WHERE id = %s", (category_id,))
    cat_row = cursor.fetchone()

    # Delete all entries for this category between start_date and end_date
    cursor.execute(
        f"DELETE FROM {table_name} WHERE category_id = %s AND date BETWEEN %s AND %s",
        (category_id, start_date, end_date)
    )

    ca_triggered = False
    # If this is an expense category and is_credit_account=1, delete payment in c_payment_entries for the full range
    if entry_type == 'expense' and cat_row and cat_row.get('is_credit_account', 0) == 1:
        payment_category_name = cat_row['name']
        cursor.execute("""
            SELECT ca.id AS account_id
            FROM credit_accounts ca
            WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
            LIMIT 1
        """, (current_user.id, payment_category_name))
        account_row = cursor.fetchone()
        if account_row:
            account_id = account_row['account_id']
            # Delete payment entries from c_payment_entries for this account and date range
            cursor.execute("""
                DELETE FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
            """, (account_id, start_date, end_date))
            ca_triggered = True

    conn.commit()
    cursor.close()
    conn.close()

    # If a CA payment was updated, trigger CA balance recalculation
    if ca_triggered:
        save_ca_daily_balance()

    return jsonify({'status': 'success'})

@app.route('/move_entry_d', methods=['POST'])
@login_required
def move_entry_d():
    data = request.get_json()
    entry_id = data.get('entry_id')
    new_date = data.get('new_date')
    entry_type = data.get('type')  # 'income', 'expense', or 'ca'

    if not entry_id or not new_date or not entry_type:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if entry_type == 'income':
            cursor.execute("""
                SELECT ie.category_id
                FROM income_entries ie
                JOIN income_categories ic ON ie.category_id = ic.id
                WHERE ie.id = %s AND ic.user_id = %s
            """, (entry_id, current_user.id))
            row = cursor.fetchone()
            if not row:
                return jsonify({'status': 'error', 'message': 'Entry not found or not authorized'}), 404
            category_id = row['category_id']

            cursor.execute("SELECT amount FROM income_entries WHERE id = %s", (entry_id,))
            amount_row = cursor.fetchone()
            amount = amount_row['amount'] if amount_row else 0

            cursor.execute("""
                SELECT id, amount FROM income_entries
                WHERE category_id = %s AND date = %s
            """, (category_id, new_date))
            existing = cursor.fetchone()

            if existing:
                new_amount = existing['amount'] + amount
                cursor.execute("UPDATE income_entries SET amount = %s WHERE id = %s", (new_amount, existing['id']))
                cursor.execute("DELETE FROM income_entries WHERE id = %s", (entry_id,))
            else:
                cursor.execute("UPDATE income_entries SET date = %s WHERE id = %s", (new_date, entry_id))

        elif entry_type == 'expense':
            cursor.execute("""
                SELECT ee.category_id, ec.is_credit_account, ec.name
                FROM expense_entries ee
                JOIN expense_categories ec ON ee.category_id = ec.id
                WHERE ee.id = %s AND ec.user_id = %s
            """, (entry_id, current_user.id))
            row = cursor.fetchone()
            if not row:
                return jsonify({'status': 'error', 'message': 'Entry not found or not authorized'}), 404
            category_id = row['category_id']
            is_credit = row['is_credit_account']
            payment_category_name = row['name']

            cursor.execute("SELECT amount, date FROM expense_entries WHERE id = %s", (entry_id,))
            amount_row = cursor.fetchone()
            amount = amount_row['amount'] if amount_row else 0
            old_date = amount_row['date'] if amount_row else None

            cursor.execute("""
                SELECT id, amount FROM expense_entries
                WHERE category_id = %s AND date = %s
            """, (category_id, new_date))
            existing = cursor.fetchone()

            if existing:
                new_amount = existing['amount'] + amount
                cursor.execute("UPDATE expense_entries SET amount = %s WHERE id = %s", (new_amount, existing['id']))
                cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry_id,))
            else:
                cursor.execute("UPDATE expense_entries SET date = %s WHERE id = %s", (new_date, entry_id))

            # If is_credit_account, update c_payment_entries and CA balances
            if is_credit == 1:
                cursor.execute("""
                    SELECT ca.id AS account_id
                    FROM credit_accounts ca
                    WHERE ca.user_id = %s AND %s LIKE CONCAT(ca.name, ' payment')
                    LIMIT 1
                """, (current_user.id, payment_category_name))
                account_row = cursor.fetchone()
                if account_row:
                    account_id = account_row['account_id']
                    # Find the payment entry for the old date
                    cursor.execute("""
                        SELECT id FROM c_payment_entries
                        WHERE account_id = %s AND date = %s
                    """, (account_id, old_date))
                    payment_entry = cursor.fetchone()
                    if payment_entry:
                        # Just update the date to the new date
                        cursor.execute("""
                            UPDATE c_payment_entries SET date = %s WHERE id = %s
                        """, (new_date, payment_entry['id']))
                    else:
                        # If not found, insert a new payment entry at the new date
                        cursor.execute("""
                            INSERT INTO c_payment_entries (account_id, date, amount, processed)
                            VALUES (%s, %s, %s, 1)
                        """, (account_id, new_date, amount))

        elif entry_type == 'ca':
            cursor.execute("""
                SELECT cee.category_id
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE cee.id = %s AND ca.user_id = %s
            """, (entry_id, current_user.id))
            row = cursor.fetchone()
            if not row:
                return jsonify({'status': 'error', 'message': 'Entry not found or not authorized'}), 404
            category_id = row['category_id']

            cursor.execute("SELECT amount FROM c_expense_entries WHERE id = %s", (entry_id,))
            amount_row = cursor.fetchone()
            amount = amount_row['amount'] if amount_row else 0

            cursor.execute("""
                SELECT id, amount FROM c_expense_entries
                WHERE category_id = %s AND date = %s
            """, (category_id, new_date))
            existing = cursor.fetchone()

            if existing:
                new_amount = existing['amount'] + amount
                cursor.execute("UPDATE c_expense_entries SET amount = %s WHERE id = %s", (new_amount, existing['id']))
                cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry_id,))
            else:
                cursor.execute("UPDATE c_expense_entries SET date = %s WHERE id = %s", (new_date, entry_id))

        else:
            return jsonify({'status': 'error', 'message': 'Invalid entry type'}), 400

        conn.commit()
        save_ca_daily_balance()
        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

############################################## Buds ######################################################

@app.route('/buds')
@login_required
def buds():
    bud_id = request.args.get('bud_id', type=int)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Use LEFT JOIN to include buds with no expense_category_id
    cursor.execute("""
        SELECT b.id, b.name, b.expense_category_id, b.created_at, b.active, ec.name AS category_name
        FROM buds b
        LEFT JOIN expense_categories ec ON b.expense_category_id = ec.id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
    """, (current_user.id,))
    buds = cursor.fetchall()

    # Determine selected bud
    selected_bud = None
    if buds:
        if bud_id:
            selected_bud = next((bud for bud in buds if bud['id'] == bud_id), buds[0])
        else:
            selected_bud = buds[0]
    else:
        selected_bud = None

    # Get all bud items for the selected bud
    bud_items_by_bud = {}
    if selected_bud:
        cursor.execute("""
            SELECT * FROM bud_items
            WHERE bud_id = %s
            ORDER BY id DESC
        """, (selected_bud['id'],))
        bud_items = cursor.fetchall()
        bud_items_by_bud[selected_bud['id']] = bud_items
    else:
        bud_items = []

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
    landing_page = user_row['landing_page'] if user_row and 'landing_page' in user_row else 'dashboard'
    currency_type = user_row['currency_type'] if user_row and 'currency_type' in user_row else 'USD'

    conn.close()

    return render_template(
        'buds.html',
        buds=buds,
        selected_bud=selected_bud,
        bud_items_by_bud=bud_items_by_bud,
        expense_categories=expense_categories,
        credit_accounts=credit_accounts,  # <-- Pass credit accounts to the template
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Only create the buds record, do NOT create the expense category here
    cursor.execute("""
        INSERT INTO buds (user_id, name, active)
        VALUES (%s, %s, 0)
    """, (current_user.id, bud_name))

    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})

@app.route('/add-bud-item', methods=['POST'])
@login_required
def add_bud_item():
    data = request.get_json()
    name = data.get('name', '').strip()
    value = data.get('value', None)
    date_val = data.get('date', None)
    bud_id = int(data.get('bud_id', 0))
    active = int(data.get('active', 0))  # Default to 0 if not provided
    account = data.get('account', '').strip()  # <-- Get the selected account

    if not name or not value or not date_val or not bud_id:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    # Insert bud_item with account value
    cursor.execute(
        "INSERT INTO bud_items (bud_id, name, value, date, account) VALUES (%s, %s, %s, %s, %s)",
        (bud_id, name, value, date_val, account)
    )
    bud_item_id = cursor.lastrowid  # Get the new bud_item's id

    # Only add expense entry if active flag is set
    if active:
        add_expense_entry_for_bud_item(cursor, bud_id, bud_item_id, value, date_val)

    conn.commit()
    cursor.close()
    conn.close()

    if active:
        save_ca_daily_balance()
    return jsonify({'status': 'success'})

def add_expense_entry_for_bud_item(cursor, bud_id, bud_item_id, value, date_val):
    cursor.execute("SELECT account FROM bud_items WHERE id = %s", (bud_item_id,))
    bud_item_row = cursor.fetchone()
    account = bud_item_row['account'] if bud_item_row else "Blankee"

    # Always fetch bud_row for bud name and expense_category_id
    cursor.execute("SELECT name, expense_category_id FROM buds WHERE id = %s", (bud_id,))
    bud_row = cursor.fetchone()
    bud_name = bud_row['name'] if bud_row else "Bud"
    expense_category_id = bud_row['expense_category_id'] if bud_row else None

    if account.lower() == "blankee":
        if expense_category_id:
            cursor.execute(
                "INSERT INTO expense_entries (category_id, date, amount, bud_item_id) VALUES (%s, %s, %s, %s)",
                (expense_category_id, date_val, value, bud_item_id)
            )
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
            cursor.execute(
                "INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id) VALUES (%s, %s, %s, %s)",
                (category_id, date_val, value, bud_item_id)
            )

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Update bud_items table
    query = f"UPDATE bud_items SET {field} = %s WHERE id = %s"
    cursor.execute(query, (value, item_id))

    # Get the account and bud active status after update
    cursor.execute("SELECT account, bud_id FROM bud_items WHERE id = %s", (item_id,))
    bud_item = cursor.fetchone()
    account = bud_item['account'].lower() if bud_item and 'account' in bud_item else "blankee"
    bud_id = bud_item['bud_id'] if bud_item and 'bud_id' in bud_item else None

    bud_active = 0
    if bud_id:
        cursor.execute("SELECT active FROM buds WHERE id = %s", (bud_id,))
        bud_row = cursor.fetchone()
        bud_active = bud_row['active'] if bud_row and 'active' in bud_row else 0

    # Only update expense entry if bud is active
    if bud_active == 1:
        update_expense_entry_for_bud_item(cursor, item_id, field, value)

    conn.commit()
    cursor.close()
    conn.close()

    # Only run save_ca_daily_balance if account is not Blankee and bud is active
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get bud_item info
    cursor.execute("SELECT bud_id, account FROM bud_items WHERE id = %s", (item_id,))
    bud_item = cursor.fetchone()
    if not bud_item:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': 'Item not found'}), 404

    bud_id = bud_item['bud_id']
    account = bud_item['account']
    today = date.today()

    bud_active = 0
    if bud_id:
        cursor.execute("SELECT active FROM buds WHERE id = %s", (bud_id,))
        bud_row = cursor.fetchone()
        bud_active = bud_row['active'] if bud_row and 'active' in bud_row else 0

    if account.lower() == "blankee":
        # Get expense_category_id for this bud
        cursor.execute("SELECT expense_category_id FROM buds WHERE id = %s", (bud_id,))
        bud_row = cursor.fetchone()
        if not bud_row:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Bud not found'}), 404
        expense_category_id = bud_row['expense_category_id']

        # Find Auto Adjustments category for this user
        cursor.execute("SELECT id FROM expense_categories WHERE user_id = %s AND name = %s", (current_user.id, "Auto Adjustments"))
        auto_adj = cursor.fetchone()
        if not auto_adj:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Auto Adjustments category not found'}), 404
        auto_adj_id = auto_adj['id']

        # Get all expense_entries for this bud_item
        cursor.execute("SELECT * FROM expense_entries WHERE bud_item_id = %s", (item_id,))
        entries = cursor.fetchall()

        # For entries before today, move to Auto Adjustments
        for entry in entries:
            if entry['date'] < today:
                cursor.execute("""
                    INSERT INTO expense_entries (category_id, date, amount, bud_item_id, processed)
                    VALUES (%s, %s, %s, NULL, 1)
                """, (auto_adj_id, entry['date'], entry['amount']))
            # Delete the entry
            cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry['id'],))

    else:
        # CA: Find the credit account
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s AND name = %s", (current_user.id, account))
        ca_row = cursor.fetchone()
        if not ca_row:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Credit account not found'}), 404
        account_id = ca_row['id']

        # Find Auto Adjustments CA category for this account
        cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (account_id, "Auto Adjustments"))
        auto_adj = cursor.fetchone()
        if not auto_adj:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Auto Adjustments CA category not found'}), 404
        auto_adj_id = auto_adj['id']

        # Get all c_expense_entries for this bud_item
        cursor.execute("SELECT * FROM c_expense_entries WHERE bud_item_id = %s", (item_id,))
        entries = cursor.fetchall()

        # For entries before today, move to Auto Adjustments CA
        for entry in entries:
            if entry['date'] < today:
                cursor.execute("""
                    INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id, processed)
                    VALUES (%s, %s, %s, NULL, 1)
                """, (auto_adj_id, entry['date'], entry['amount']))
            # Delete the entry
            cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry['id'],))

    # Delete the bud_item itself (will also delete future expense_entries/c_expense_entries if ON DELETE CASCADE)
    cursor.execute("DELETE FROM bud_items WHERE id = %s", (item_id,))

    conn.commit()
    cursor.close()
    conn.close()
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get all bud_items for this bud
    cursor.execute("SELECT id, account FROM bud_items WHERE bud_id = %s", (bud_id,))
    bud_items = cursor.fetchall()
    today = date.today()

    # Track CA categories to delete
    ca_category_ids_to_delete = set()
    bud_category_ids_to_delete = set()

    # Get bud name and expense_category_id
    cursor.execute("SELECT name, expense_category_id FROM buds WHERE id = %s", (bud_id,))
    bud_row = cursor.fetchone()
    bud_name = bud_row['name'] if bud_row else None
    bud_expense_category_id = bud_row['expense_category_id'] if bud_row else None

    for bud_item in bud_items:
        item_id = bud_item['id']
        account = bud_item['account'].lower() if bud_item['account'] else "blankee"

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

            # Get all expense_entries for this bud_item
            cursor.execute("SELECT * FROM expense_entries WHERE bud_item_id = %s", (item_id,))
            entries = cursor.fetchall()

            # For entries before today, move to Auto Adjustments
            for entry in entries:
                if entry['date'] < today:
                    cursor.execute("""
                        INSERT INTO expense_entries (category_id, date, amount, bud_item_id, processed)
                        VALUES (%s, %s, %s, NULL, 1)
                    """, (auto_adj_id, entry['date'], entry['amount']))
                cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry['id'],))

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

            # Get all c_expense_entries for this bud_item
            cursor.execute("SELECT * FROM c_expense_entries WHERE bud_item_id = %s", (item_id,))
            entries = cursor.fetchall()

            # For entries before today, move to Auto Adjustments CA
            for entry in entries:
                if entry['date'] < today:
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id, processed)
                        VALUES (%s, %s, %s, NULL, 1)
                    """, (auto_adj_id, entry['date'], entry['amount']))
                cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry['id'],))

            # Track CA categories created for this bud (by bud name)
            if bud_name:
                cursor.execute("""
                    SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s
                """, (account_id, bud_name))
                cat_row = cursor.fetchone()
                if cat_row:
                    ca_category_ids_to_delete.add(cat_row['id'])

    # Delete all bud_items for this bud (will also delete future entries via ON DELETE CASCADE)
    cursor.execute("DELETE FROM bud_items WHERE bud_id = %s", (bud_id,))

    # Delete the bud itself
    cursor.execute("DELETE FROM buds WHERE id = %s", (bud_id,))

    # Delete the bud's expense category if present
    for bud_cat_id in bud_category_ids_to_delete:
        cursor.execute("DELETE FROM expense_categories WHERE id = %s AND user_id = %s", (bud_cat_id, current_user.id))

    # Delete any c_expense_categories created for this bud
    for ca_cat_id in ca_category_ids_to_delete:
        cursor.execute("DELETE FROM c_expense_categories WHERE id = %s", (ca_cat_id,))

    conn.commit()
    cursor.close()
    conn.close()
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM buds WHERE id = %s AND user_id = %s", (bud_id, current_user.id))
    bud_row = cursor.fetchone()
    if not bud_row:
        cursor.close()
        conn.close()
        return jsonify({'status': 'error', 'message': 'Bud not found'}), 404

    bud_name = bud_row['name']

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

    cursor.execute("SELECT id, account, value, date FROM bud_items WHERE bud_id = %s", (bud_id,))
    bud_items = cursor.fetchall()

    if active == 1:
        if not bud_row['expense_category_id']:
            cursor.execute("""
                SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s
            """, (current_user.id,))
            max_order = cursor.fetchone()['COALESCE(MAX(display_order), 0)']
            cursor.execute("""
                INSERT INTO expense_categories (user_id, name, display_order, is_bud)
                VALUES (%s, %s, %s, 1)
            """, (current_user.id, bud_name, max_order + 1))
            expense_category_id = cursor.lastrowid
            cursor.execute("""
                UPDATE buds SET active = %s, expense_category_id = %s WHERE id = %s AND user_id = %s
            """, (active, expense_category_id, bud_id, current_user.id))
            cursor.execute("SELECT * FROM buds WHERE id = %s AND user_id = %s", (bud_id, current_user.id))
            bud_row = cursor.fetchone()
        else:
            cursor.execute("""
                UPDATE buds SET active = %s WHERE id = %s AND user_id = %s
            """, (active, bud_id, current_user.id))

        for item in bud_items:
            item_id = item['id']
            account = item['account']
            value = item['value']
            item_date = item['date']
            if account and account.lower() == "blankee":
                cursor.execute("""
                    SELECT id FROM expense_entries WHERE category_id = %s AND bud_item_id = %s
                """, (bud_row['expense_category_id'], item_id))
                exists = cursor.fetchone()
                if not exists:
                    cursor.execute("""
                        INSERT INTO expense_entries (category_id, date, amount, bud_item_id, processed)
                        VALUES (%s, %s, %s, %s, 0)
                    """, (bud_row['expense_category_id'], item_date, value, item_id))
                cursor.execute("""
                    DELETE FROM expense_entries WHERE category_id = %s AND bud_item_id = %s
                """, (auto_adj_id, item_id))
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
                cursor.execute("""
                    SELECT id FROM c_expense_entries WHERE category_id = %s AND bud_item_id = %s
                """, (bud_cat_id, item_id))
                exists = cursor.fetchone()
                if not exists:
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id, processed)
                        VALUES (%s, %s, %s, %s, 0)
                    """, (bud_cat_id, item_date, value, item_id))
                ca_auto_adj_id = ca_auto_adj_ids.get(ca_id)
                if ca_auto_adj_id:
                    cursor.execute("""
                        DELETE FROM c_expense_entries WHERE category_id = %s AND bud_item_id = %s
                    """, (ca_auto_adj_id, item_id))

        if not bud_row['expense_category_id']:
            cursor.execute("""
                SELECT COALESCE(MAX(display_order), 0) FROM expense_categories WHERE user_id = %s
            """, (current_user.id,))
            max_order = cursor.fetchone()['COALESCE(MAX(display_order), 0)']
            cursor.execute("""
                INSERT INTO expense_categories (user_id, name, display_order, is_bud)
                VALUES (%s, %s, %s, 1)
            """, (current_user.id, bud_name, max_order + 1))
            expense_category_id = cursor.lastrowid
            cursor.execute("""
                UPDATE buds SET active = %s, expense_category_id = %s WHERE id = %s AND user_id = %s
            """, (active, expense_category_id, bud_id, current_user.id))
        else:
            cursor.execute("""
                UPDATE buds SET active = %s WHERE id = %s AND user_id = %s
            """, (active, bud_id, current_user.id))

    elif active == 0:
        if bud_row['expense_category_id']:
            for item in bud_items:
                item_id = item['id']
                cursor.execute("""
                    SELECT * FROM expense_entries
                    WHERE category_id = %s AND bud_item_id = %s
                """, (bud_row['expense_category_id'], item_id))
                entries = cursor.fetchall()
                for entry in entries:
                    if entry['date'] < today:
                        cursor.execute("""
                            INSERT INTO expense_entries (category_id, date, amount, bud_item_id, processed)
                            VALUES (%s, %s, %s, %s, 1)
                        """, (auto_adj_id, entry['date'], entry['amount'], item_id))
                    cursor.execute("DELETE FROM expense_entries WHERE id = %s", (entry['id'],))

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
                if not ca_auto_adj_id:
                    continue
                cursor.execute("SELECT id FROM c_expense_categories WHERE account_id = %s AND name = %s", (ca_id, bud_name))
                cat_row = cursor.fetchone()
                if not cat_row:
                    continue
                bud_cat_id = cat_row['id']
                cursor.execute("""
                    SELECT * FROM c_expense_entries
                    WHERE category_id = %s AND bud_item_id = %s
                """, (bud_cat_id, item_id))
                ca_entries = cursor.fetchall()
                for entry in ca_entries:
                    if entry['date'] < today:
                        cursor.execute("""
                            INSERT INTO c_expense_entries (category_id, date, amount, bud_item_id, processed)
                            VALUES (%s, %s, %s, %s, 1)
                        """, (ca_auto_adj_id, entry['date'], entry['amount'], item_id))
                    cursor.execute("DELETE FROM c_expense_entries WHERE id = %s", (entry['id'],))

        if bud_row['expense_category_id']:
            cursor.execute("""
                DELETE FROM expense_categories WHERE id = %s AND user_id = %s
            """, (bud_row['expense_category_id'], current_user.id))
            cursor.execute("""
                UPDATE buds SET active = %s, expense_category_id = NULL WHERE id = %s AND user_id = %s
            """, (active, bud_id, current_user.id))
        else:
            cursor.execute("""
                UPDATE buds SET active = %s WHERE id = %s AND user_id = %s
            """, (active, bud_id, current_user.id))
    else:
        cursor.execute("""
            UPDATE buds SET active = %s WHERE id = %s AND user_id = %s
        """, (active, bud_id, current_user.id))

    conn.commit()
    cursor.close()
    conn.close()
    save_ca_daily_balance()
    return jsonify({'status': 'success'})

############################################## Credit Accounts ######################################################

@app.route('/credit_accounts')
@login_required
def credit_accounts():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
    landing_page = user_row['landing_page'] if user_row and 'landing_page' in user_row else 'dashboard'
    currency_type = user_row['currency_type'] if user_row and 'currency_type' in user_row else 'USD'

    today_str = date.today().strftime('%Y-%m-%d')
    ca_balances_today = {}
    for row in c_a_balances_d:
        if str(row['date']) == today_str:
            ca_balances_today[row['account_id']] = row['balance']

    conn.close()

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

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
    conn.close()

    initialize_ca_balances_for_account(account_id)
    save_ca_daily_balance()
    save_totals_remainders_d()

    return jsonify({'status': 'success', 'account_id': account_id})

def initialize_ca_balances_for_account(account_id):
    today = date.today()
    start_year = today.year - 1
    end_year = today.year + 5

    # Daily: every day from Jan 1 of previous year to Dec 31 of year+5
    start_date = date(start_year, 1, 1)
    end_date = date(end_year, 12, 31)

    conn = get_db_connection()
    cursor = conn.cursor()

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

    conn.commit()
    conn.close()

@app.route('/delete-credit-account', methods=['POST'])
@login_required
def delete_credit_account():
    data = request.get_json()
    account_id = data.get('id')
    if not account_id:
        return jsonify({'status': 'error', 'message': 'Missing account id'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
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
        conn.close()

        # After deleting the credit account, delete the associated expense category
        if account_name:
            payment_category_name = f"{account_name} Payment"
            # Open a new connection for the next operation
            conn2 = get_db_connection()
            cursor2 = conn2.cursor()
            cursor2.execute("""
                SELECT id FROM expense_categories
                WHERE user_id = %s AND name = %s AND is_credit_account = 1
                LIMIT 1
            """, (current_user.id, payment_category_name))
            row = cursor2.fetchone()
            if row:
                from flask import Request
                with app.test_request_context(
                    '/delete_expense_category',
                    method='POST',
                    data={'id': row[0]}
                ):
                    delete_expense_category()
            cursor2.close()
            conn2.close()

        save_ca_daily_balance()
        save_totals_remainders_d()

        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

@app.route('/update-credit-account', methods=['POST'])
@login_required
def update_credit_account():
    data = request.get_json()
    account_id = data.get('id')
    field = data.get('field')
    value = data.get('value')

    if field not in ['name', 'interest_rate', 'type']:
        return jsonify({'status': 'error', 'message': 'Invalid field.'})

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
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
        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

################################################## Recurring CA Expense #################################################

@app.route('/recurring-ca-expense')
@login_required
def recurring_ca_expense():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch recurring CA expense records, including no_end_date from c_expense_categories
    cursor.execute("""
        SELECT rce.id, rce.category_id, cec.account_id, cec.name as category_name, rce.amount, 
               rce.cadence_interval, rce.cadence_unit, rce.weekdays, rce.monthly_days, 
               rce.start_date, rce.end_date, rce.yearly_day, rce.yearly_month,
               cec.no_end_date
        FROM recurring_c_expense rce
        JOIN c_expense_categories cec ON rce.category_id = cec.id
        JOIN credit_accounts ca ON cec.account_id = ca.id
        WHERE rce.user_id = %s
    """, (current_user.id,))
    recurring_ca_expense_records = cursor.fetchall()

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
    conn.close()

    profile_picture = user_data['profile_picture'] if user_data else None
    first_name = user_data['first_name'] if user_data else ''
    last_name = user_data['last_name'] if user_data else ''
    landing_page = user_data['landing_page'] if user_data and user_data['landing_page'] else 'dashboard'
    currency_type = user_data['currency_type'] if user_data and 'currency_type' in user_data else 'USD'

    current_date = date.today()
    no_end_date = date(current_date.year + 5, 12, 31)

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
        conn = get_db_connection()
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

        # Step 3: Insert the recurring CA expense record
        cursor.execute("""
            INSERT INTO recurring_c_expense 
            (user_id, category_id, amount, cadence_interval, cadence_unit, weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            current_user.id, category_id, amount, cadence_interval, cadence_unit,
            ','.join(weekdays) if weekdays else None,
            ','.join(map(str, monthly_days)) if monthly_days else None,
            yearly_day if yearly_day else None,
            yearly_month if yearly_month else None,
            start_date,
            end_date
        ))

        recurring_id = cursor.lastrowid
        conn.commit()

        # Generate CA expense entries based on the cadence
        generate_ca_expense_entries(
            recurring_id, category_id, amount, cadence_interval, cadence_unit,
            start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
        )

        return jsonify({'status': 'success', 'recurring_id': recurring_id, 'message': 'Recurring CA expense added successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred while adding the recurring CA expense: {str(e)}'}), 500

    finally:
        cursor.close()
        conn.close()

def generate_ca_expense_entries(recurring_id, category_id, amount, cadence_interval, cadence_unit, start_date_str, end_date_str, weekdays=None, monthly_days=None, yearly_day=None, yearly_month=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    current_date = start_date
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    while current_date <= end_date:
        delta = None

        if cadence_unit == 'days':
            cursor.execute("""
                INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                VALUES (%s, %s, %s, %s)
            """, (recurring_id, category_id, current_date, amount))
            delta = timedelta(days=int(cadence_interval))

        elif cadence_unit == 'weeks':
            for weekday in weekdays:
                weekday_num = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].index(weekday)
                weekday_date = current_date + timedelta(days=(weekday_num - current_date.weekday()) % 7)
                if start_date <= weekday_date <= end_date:
                    cursor.execute("""
                        INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, weekday_date, amount))
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
                        cursor.execute("""
                            INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                        cursor.execute("""
                            INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                            VALUES (%s, %s, %s, %s)
                        """, (recurring_id, category_id, entry_date, amount))
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
                year = current_date.year
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
                    cursor.execute("""
                        INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
                    year += interval
            else:
                interval = int(cadence_interval)
                year = current_date.year
                while True:
                    yearly_entry_date = date(year=year, month=1, day=1)
                    if yearly_entry_date < start_date:
                        year += interval
                        continue
                    if yearly_entry_date > end_date:
                        break
                    cursor.execute("""
                        INSERT INTO c_expense_entries (recurring_id, category_id, date, amount)
                        VALUES (%s, %s, %s, %s)
                    """, (recurring_id, category_id, yearly_entry_date, amount))
                    year += interval

        if delta:
            current_date += delta
        else:
            break

    conn.commit()
    cursor.close()
    conn.close()

@app.route('/delete-recurring-ca-expense', methods=['POST'])
@login_required
def delete_recurring_ca_expense():
    try:
        data = request.get_json()
        recurring_id = data.get('recurring_id')
        if not recurring_id:
            return jsonify({'status': 'error', 'message': 'Recurring ID not provided.'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get the category_id for this recurring CA expense
        cursor.execute("""
            SELECT category_id FROM recurring_c_expense WHERE id = %s AND user_id = %s
        """, (recurring_id, current_user.id))
        category = cursor.fetchone()
        if not category:
            return jsonify({'status': 'error', 'message': 'Recurring CA expense not found.'}), 404
        category_id = category[0]

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

        # Delete all future entries and the recurring record/category
        cursor.execute("DELETE FROM c_expense_entries WHERE recurring_id = %s", (recurring_id,))
        cursor.execute("DELETE FROM recurring_c_expense WHERE id = %s AND user_id = %s", (recurring_id, current_user.id))
        cursor.execute("DELETE FROM c_expense_categories WHERE id = %s", (category_id,))

        conn.commit()
        return jsonify({'status': 'success', 'message': 'Recurring CA expense deleted successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': 'An error occurred while deleting the recurring CA expense.'}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/update-recurring-ca-expense', methods=['POST'])
@login_required
def update_recurring_ca_expense():
    data = request.get_json()
    return update_recurring_ca_expense_inner(data, current_user.id)

def update_recurring_ca_expense_inner(data, user_id):
    conn = None
    cursor = None
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

        conn = get_db_connection()
        cursor = conn.cursor()

        # Step 1: Fetch the category_id from recurring_c_expense
        cursor.execute("""
            SELECT category_id FROM recurring_c_expense WHERE id = %s AND user_id = %s
        """, (recurring_id, user_id))
        result = cursor.fetchone()
        if not result:
            return jsonify({'status': 'error', 'message': 'Recurring CA expense not found'}), 404
        category_id = result[0]

        # Step 2: Update the recurring CA expense record in the database
        cursor.execute("""
            UPDATE recurring_c_expense
            SET amount = %s, cadence_interval = %s, cadence_unit = %s, weekdays = %s, monthly_days = %s, yearly_day = %s, yearly_month = %s, start_date = %s, end_date = %s
            WHERE id = %s AND user_id = %s
        """, (
            amount, cadence_interval, cadence_unit,
            ','.join(weekdays) if weekdays else None,
            ','.join(map(str, monthly_days)) if monthly_days else None,
            yearly_day if yearly_day else None,
            yearly_month if yearly_month else None,
            start_date, end_date, recurring_id, user_id
        ))

        # Step 3: Delete old CA expense entries for today and the future related to this recurring record
        cursor.execute("""
            DELETE FROM c_expense_entries
            WHERE recurring_id = %s AND date >= %s
        """, (recurring_id, today))

        conn.commit()

        # Step 4: Recreate the CA expense entries with the updated details (only for today and future)
        generate_ca_expense_entries(
            recurring_id, category_id, amount, cadence_interval, cadence_unit,
            start_date, end_date, weekdays, monthly_days, yearly_day, yearly_month
        )

        conn.commit()

        return jsonify({'status': 'success', 'message': 'Recurring CA expense updated successfully!'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {str(e)}'}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()